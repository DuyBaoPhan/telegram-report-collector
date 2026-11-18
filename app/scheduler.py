"""Private-admin scheduled exports with durable, at-most-once slot claims."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import math
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    timezone: ZoneInfo
    times: tuple[str, ...]
    group_times: dict[int, tuple[str, ...]]
    recipients: frozenset[int]
    realtime_enabled: bool = False
    debounce_seconds: float = 10

    @classmethod
    def load(cls, path, admin_ids):
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or set(data) - {'enabled', 'timezone', 'times', 'group_times', 'recipient_admin_ids', 'realtime'}:
            raise ValueError('Invalid schedule configuration keys')
        enabled = data.get('enabled', False)
        if not isinstance(enabled, bool):
            raise ValueError('schedule enabled must be boolean')
        try:
            timezone = ZoneInfo(data.get('timezone', 'Asia/Ho_Chi_Minh'))
        except Exception as exc:
            raise ValueError('Invalid schedule timezone') from exc
        def times(values):
            if not isinstance(values, list):
                raise ValueError('Schedule times must be a list')
            for value in values:
                if not isinstance(value, str) or len(value) != 5 or value[2] != ':' or not value.replace(':', '').isascii() or not value.replace(':', '').isdigit():
                    raise ValueError('Schedule time must be HH:MM')
                hour, minute = map(int, value.split(':'))
                if hour > 23 or minute > 59:
                    raise ValueError('Invalid schedule time')
            return tuple(sorted(set(values)))
        groups = data.get('group_times', {})
        if not isinstance(groups, dict):
            raise ValueError('group_times must be an object')
        parsed = {}
        for key, value in groups.items():
            try:
                chat_id = int(key)
            except (TypeError, ValueError):
                raise ValueError('group_times keys must be negative chat IDs') from None
            if str(chat_id) != key or chat_id >= 0:
                raise ValueError('group_times keys must be negative chat IDs')
            parsed[chat_id] = times(value)
        recipients = data.get('recipient_admin_ids', sorted(admin_ids))
        if not isinstance(recipients, list) or not recipients or any(type(x) is not int or x <= 0 or x not in admin_ids for x in recipients):
            raise ValueError('Scheduled recipients must be positive IDs in ADMIN_IDS')
        realtime = data.get('realtime', {})
        if not isinstance(realtime, dict) or set(realtime) - {'enabled', 'debounce_seconds'}:
            raise ValueError('Invalid realtime configuration')
        realtime_enabled = realtime.get('enabled', False)
        debounce = realtime.get('debounce_seconds', 10)
        if type(realtime_enabled) is not bool or type(debounce) not in (int, float) or not math.isfinite(debounce) or debounce <= 0:
            raise ValueError('Realtime enabled must be boolean and debounce_seconds positive finite number')
        return cls(enabled, timezone, times(data.get('times', ['09:00', '15:00', '17:00'])), parsed, frozenset(recipients), realtime_enabled, debounce)


class ScheduleService:
    def __init__(self, database, bot, generator, config, export_lock=None, clock=None, scheduler=None):
        self.database, self.bot, self.generator, self.config = database, bot, generator, config
        self.lock = export_lock or asyncio.Lock()
        self.clock = clock or (lambda: datetime.now(self.config.timezone))
        self.scheduler = scheduler or AsyncIOScheduler(timezone=config.timezone)
        self.tasks = set()
        self.debounce_handle = None
        self.pending_signature = None
        self.stopping = False
        self.realtime_lock = asyncio.Lock()

    def pending(self):
        with self.database.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM realtime_dirty WHERE revision>completed_revision ORDER BY report_date,chat_id')]

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self.tasks.discard(task)
        # Detached debounce/startup jobs otherwise emit unhandled-task warnings
        # (and their exception payloads may contain private dependency details).
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error('Background schedule task failed (%s); durable state retained', type(error).__name__)

    async def notify_changes(self):
        """Wake only for new durable revisions; ordinary chat cannot postpone export."""
        if not self.config.realtime_enabled or self.stopping:
            return
        rows = await asyncio.to_thread(self.pending)
        if self.stopping:
            return
        signature = tuple((r['report_date'], r['chat_id'], r['revision']) for r in rows)
        if not signature or signature == self.pending_signature:
            return
        self.pending_signature = signature
        if self.debounce_handle:
            self.debounce_handle.cancel()
        self.debounce_handle = asyncio.get_running_loop().call_later(
            self.config.debounce_seconds, lambda: self.spawn(self.flush_realtime()))

    def unresolved_snapshot(self, day):
        # A failed edit can leave a formerly valid row PENDING; never publish a
        # temporary omission as the completed snapshot for another report.
        with self.database.connect() as db:
            return db.execute("""SELECT 1 FROM report_export_state s
                JOIN raw_messages m ON m.id=s.raw_message_id
                WHERE s.report_date=? AND m.processing_status='PENDING' LIMIT 1""", (day,)).fetchone() is not None

    def attempt(self, day, rows):
        # Commit BEFORE the first Telegram call. Crash/network uncertainty never resends.
        with self.database.connect() as db:
            attempt_id = db.execute("INSERT INTO realtime_attempts(report_date,revisions_json,status) VALUES (?,?,'ATTEMPTING')",
                (day, json.dumps([(r['chat_id'],r['revision']) for r in rows]))).lastrowid
            for r in rows:
                db.execute('UPDATE realtime_dirty SET completed_revision=MAX(completed_revision,?) WHERE report_date=? AND chat_id=?',
                    (r['revision'],day,r['chat_id']))
            return attempt_id

    def finish_attempt(self, attempt_id, status, detail):
        with self.database.connect() as db:
            db.execute('UPDATE realtime_attempts SET status=?,detail=?,completed_at=CURRENT_TIMESTAMP WHERE id=?',
                       (status,detail,attempt_id))

    async def flush_realtime(self):
        """Explicit flush is also useful in deterministic tests and maintenance."""
        if not self.config.realtime_enabled or self.stopping:
            return
        if self.debounce_handle:
            self.debounce_handle.cancel()
            self.debounce_handle = None
        async with self.realtime_lock:
            rows = await asyncio.to_thread(self.pending)
            for day in sorted({r['report_date'] for r in rows}):
                batch = [r for r in rows if r['report_date'] == day]
                try:
                    async with self.lock:
                        if await asyncio.to_thread(self.unresolved_snapshot, day):
                            logger.warning('Realtime snapshot has unresolved edits; dirty revisions retained')
                            # This batch was deferred, not consumed. A successful
                            # no-op reprocess leaves the dirty revision unchanged;
                            # let its notify_changes() re-arm the debounce timer.
                            self.pending_signature = None
                            continue
                        results = await asyncio.to_thread(self.generator.export, day, None, [r['chat_id'] for r in batch])
                    if not results or any(r['status'] != 'SUCCESS' for r in results):
                        logger.warning('Realtime export incomplete; dirty revisions retained')
                        continue
                except Exception as exc:
                    logger.warning('Realtime export failed (%s); dirty revisions retained', type(exc).__name__)
                    continue
                attempt_id = await asyncio.to_thread(self.attempt, day, batch)
                failures = 0
                for recipient in sorted(self.config.recipients):
                    for result in results:
                        try:
                            await self.bot.send_document(recipient, FSInputFile(result['path']))
                        except Exception as exc:
                            failures += 1
                            logger.warning('Realtime send failed/uncertain (%s); not retried', type(exc).__name__)
                await asyncio.to_thread(self.finish_attempt, attempt_id, 'PARTIAL' if failures else 'SUCCESS', f'send_failures={failures}; no automatic resend')

    def claim(self, key, day, chat_id, trigger):
        with self.database.connect() as db:
            return db.execute('''INSERT OR IGNORE INTO scheduled_runs
                (slot_key,report_date,chat_id,trigger_type,status) VALUES (?,?,?,?,'CLAIMED')''',
                (key, day, chat_id, trigger)).rowcount == 1

    def history(self):
        with self.database.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM scheduled_runs ORDER BY id DESC LIMIT 10')]

    def finish(self, key, status, detail):
        with self.database.connect() as db:
            db.execute('UPDATE scheduled_runs SET status=?,detail=?,completed_at=CURRENT_TIMESTAMP WHERE slot_key=?', (status, detail, key))

    async def run(self, chat_id=None, manual=False, slot_time=None):
        now = self.clock().astimezone(self.config.timezone)
        if slot_time is not None and not manual:
            hour, minute = map(int, slot_time.split(":"))
            slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot > now:
                slot -= timedelta(days=1)
            now = slot
        day = now.date().isoformat()
        key = f'manual:{uuid.uuid4().hex}' if manual else f'scheduled:{chat_id}:{now:%Y-%m-%dT%H:%M}'
        if not await asyncio.to_thread(self.claim, key, day, chat_id, 'MANUAL' if manual else 'SCHEDULED'):
            return 'DUPLICATE'
        try:
            groups = await asyncio.to_thread(self.database.groups)
            active = [g['chat_id'] for g in groups if g['active'] and (chat_id is None or g['chat_id'] == chat_id)]
            if chat_id is not None and not active:
                await asyncio.to_thread(self.finish, key, 'SKIPPED', 'Group not active/registered')
                return 'SKIPPED'
            async with self.lock:
                results = await asyncio.to_thread(self.generator.export, day, None, active)
                # Ingestion/reprocess uses this same lock: keep the missing query
                # consistent with the exported reports, but release it before network I/O.
                missing = await asyncio.to_thread(self.missing, day, active)
            failures = sum(r['status'] != 'SUCCESS' for r in results)
            # Each document/message is attempted once. A network timeout may mean delivery occurred.
            for recipient in sorted(self.config.recipients):
                for result in results:
                    if result['status'] == 'SUCCESS':
                        try:
                            await self.bot.send_document(recipient, FSInputFile(result['path']))
                        except Exception as exc:
                            failures += 1
                            logger.warning('Scheduled document send failed (%s)', type(exc).__name__)
                try:
                    text = f'Scheduled report {day}: {missing}; export/send failures={failures}.'
                    for offset in range(0, len(text), 3500):
                        await self.bot.send_message(recipient, text[offset:offset+3500])
                except Exception as exc:
                    failures += 1
                    logger.warning('Scheduled notification failed (%s)', type(exc).__name__)
            status = 'PARTIAL' if failures else 'SUCCESS'
            await asyncio.to_thread(self.finish, key, status, f'failures={failures}; {missing}')
            return status
        except Exception as exc:
            await asyncio.to_thread(self.finish, key, 'FAILED', type(exc).__name__)
            logger.error('Scheduled export failed (%s)', type(exc).__name__)
            return 'FAILED'

    def missing(self, day, active):
        roster = self.generator.templates.mapping.get('roster_by_chat_id', {})
        summaries = []
        with self.database.connect() as db:
            for chat_id in active:
                if str(chat_id) not in roster:
                    summaries.append(f'{chat_id}: missing UNKNOWN (no roster)')
                    continue
                submitted = {r[0] for r in db.execute('''SELECT m.user_id FROM reports r
                    JOIN raw_messages m ON m.id=r.raw_message_id WHERE m.chat_id=? AND r.report_date=?
                    AND r.status='VALID' AND m.processing_status!='PENDING' ''', (chat_id, day))}
                missing = sorted(set(roster[str(chat_id)]) - submitted)
                summaries.append(f'{chat_id}: missing {len(missing)}' + (f' ({", ".join(map(str, missing))})' if missing else ''))
        return '; '.join(summaries) or 'missing UNKNOWN (no active groups)'

    async def scheduled(self, chat_id=None, slot_time=None):
        # APScheduler may already have queued this callback when stop() shuts
        # down its executor. Do not start new work after the drain boundary.
        if self.stopping:
            return
        task = self.spawn(self.run(chat_id, slot_time=slot_time))
        await asyncio.shield(task)

    def start(self):
        self.stopping = False
        if self.config.realtime_enabled:
            self.spawn(self.notify_changes())
        if not self.config.enabled:
            return
        for chat_id, times in [(None, self.config.times), *self.config.group_times.items()]:
            for time in times:
                hour, minute = map(int, time.split(':'))
                self.scheduler.add_job(self.scheduled, CronTrigger(hour=hour, minute=minute, timezone=self.config.timezone),
                    kwargs={'chat_id': chat_id, 'slot_time': time}, id=f'{chat_id}:{time}', coalesce=True, max_instances=1, misfire_grace_time=60)
        self.scheduler.start()

    async def stop(self):
        self.stopping = True
        if self.debounce_handle:
            self.debounce_handle.cancel()
            self.debounce_handle = None
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            await asyncio.sleep(0)  # apply AsyncIOScheduler's queued shutdown before closing HTTP session
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
