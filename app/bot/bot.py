from app.heartbeat import Heartbeat
import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

from app.excel import ExportGenerator
from app.config import Settings
from app.database.database import Database
from app.parser import ReportParser, ReportProcessor
from app.scheduler import ScheduleConfig, ScheduleService

logger = logging.getLogger(__name__)


class CommandClaimError(RuntimeError):
    """Durable claim failed: polling must stop without acknowledging this update."""


class AdminCommandAttempts(BaseMiddleware):
    """Handler middleware runs after Command filters, including direct feed_update."""
    def __init__(self, database, admin_ids):
        self.database = database
        self.admin_ids = admin_ids

    async def __call__(self, handler, event, data):
        if not event.from_user or event.from_user.id not in self.admin_ids:
            return
        update = data['event_update']
        # Edited commands are audit data, never new administrative attempts.
        if update.message is None:
            return
        if self.database is None:
            raise CommandClaimError('Command ledger unavailable')
        command = event.text.split()[0].split('@')[0].lower()
        key = self.database.event_fingerprint(update.update_id, False,
                    event.model_dump_json(exclude_none=True))
        try:
            claimed = await asyncio.to_thread(self.database.claim_command, key, command)
        except Exception:
            raise CommandClaimError('Unable to persist command claim') from None
        if not claimed:
            return
        status = 'UNCERTAIN'
        try:
            result = await handler(event, data)
            status = 'SUCCESS'
            return result
        except Exception:
            status = 'FAILED'
            raise
        finally:
            try:
                await asyncio.to_thread(self.database.finish_command, key, status)
            except Exception:
                # CLAIMED remains durable and suppresses replay even if finishing fails.
                logger.error('Unable to finalize command attempt; replay remains suppressed.')


def create_dispatcher(settings: Settings, database=None, processor=None, schedule=None, export_lock=None) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AdminCommandAttempts(database, settings.admin_ids))
    export_lock = export_lock or asyncio.Lock()

    def admin(message):
        return bool(message.from_user and message.from_user.id in settings.admin_ids)

    @dp.message(Command('start', 'help', 'status', 'groups', 'report', 'register_group', 'disable_group', 'chatid', 'report_today', 'errors', 'reprocess', 'export', 'schedule_status', 'schedule_run'))
    async def commands(message: Message):
        if not admin(message):
            return
        command = message.text.split()[0].split('@')[0]
        args = message.text.split(maxsplit=1)
        is_group = message.chat.type in ('group', 'supergroup')
        if command == '/chatid' and is_group:
            await message.answer(f'Chat ID của nhóm này: {message.chat.id}')
        elif command == '/register_group' and is_group and database:
            name = args[1].strip() if len(args) > 1 else message.chat.title or str(message.chat.id)
            await asyncio.to_thread(database.register, message.chat.id, name[:200])
            await message.answer('Đã đăng ký nhóm. Thu thập các tin nhắn mới tiếp theo.')
        elif message.chat.type == 'private':
            if command == '/schedule_status' and schedule:
                rows = await asyncio.to_thread(schedule.history)
                text = '\n'.join(f"{r['slot_key']} | {r['status']} | {r['detail'] or ''}" for r in rows) or 'No scheduled runs.'
                await message.answer(f'Scheduler enabled={schedule.config.enabled}; timezone={schedule.config.timezone}; realtime={schedule.config.realtime_enabled}; debounce={schedule.config.debounce_seconds}s\n' + text[:3400])
            elif command == '/schedule_run' and schedule:
                result = await schedule.run(manual=True)
                await message.answer(f'Schedule test: {result}; sent only to configured admin recipients.')
            elif command == '/export' and database:
                try:
                    day = args[1].strip() if len(args) > 1 else datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).date().isoformat()
                    if date.fromisoformat(day).isoformat() != day:
                        raise ValueError()
                except ValueError:
                    await message.answer('Cú pháp: /export [YYYY-MM-DD]')
                    return
                try:
                    async with export_lock:
                        exports = await asyncio.to_thread(ExportGenerator(database).export, day, message.from_user.id)
                except Exception as exc:
                    logger.error('Excel export failed (%s)', type(exc).__name__)
                    await message.answer('Không thể tạo Excel. Kiểm tra cấu hình template và quyền ghi.')
                    return
                for result in exports:
                    if result['status'] == 'SUCCESS':
                        # Only the requesting, authorized private admin receives files.
                        await message.answer_document(FSInputFile(result['path']))
                failed = sum(r['status'] == 'FAILED' for r in exports)
                await message.answer(f"Xuất Excel: {len(exports)-failed} thành công; {failed} lỗi. Lịch sử được lưu trong SQLite.")
            elif command == '/report_today' and database and processor:
                day = datetime.now(processor.parser.timezone).date().isoformat()
                rows = await asyncio.to_thread(database.report_today, day)
                text = '; '.join(f"{r['status']}: {r['count']}" for r in rows) or 'Chưa có báo cáo.'
                await message.answer(f'Ngày báo cáo {day}: {text}')
            elif command == '/errors' and database:
                rows = await asyncio.to_thread(database.errors)
                text = '\n'.join(f"raw_id={r['id']} | {r['processing_status'] if r['processing_status'] == 'PENDING' else r['status']} | {r['processing_error'] or r['issues_json'] or ''} | duplicate_of={r['duplicate_of']}" for r in rows) or 'Không có lỗi/cần duyệt.'
                for i in range(0, len(text), 3500):
                    await message.answer(text[i:i+3500])
            elif command == '/reprocess' and processor:
                try:
                    target = args[1].strip()
                    raw_id = None if target == 'all' else int(target)
                    if raw_id is not None and raw_id <= 0:
                        raise ValueError()
                except (IndexError, ValueError):
                    await message.answer('Cú pháp: /reprocess RAW_ID hoặc /reprocess all')
                    return
                async with export_lock:
                    result = await asyncio.to_thread(processor.reprocess, raw_id)
                await message.answer(f"Đã xếp: {result['queued']}; xử lý: {result['processed']}; lỗi còn PENDING: {result['failed']}.")
            elif command == '/disable_group' and database:
                try:
                    chat_id = int(args[1])
                except (IndexError, ValueError):
                    await message.answer('Cú pháp: /disable_group CHAT_ID')
                    return
                changed = await asyncio.to_thread(database.disable, chat_id)
                await message.answer('Đã vô hiệu hóa nhóm; dữ liệu cũ được giữ lại.' if changed else 'Không tìm thấy nhóm.')
            elif command == '/groups' and database:
                groups = await asyncio.to_thread(database.groups)
                text = '\n'.join(f"{g['chat_id']} | {g['display_name']} | {'ON' if g['active'] else 'OFF'}" for g in groups) or 'Chưa đăng ký nhóm.'
                for i in range(0, len(text), 3500):
                    await message.answer(text[i:i+3500])
            elif command in ('/status', '/report') and database:
                counts = await asyncio.to_thread(database.stats)
                await message.answer(f"Bot hoạt động. SQLite OK. Nhóm ON: {counts['groups']}; tin nhắn: {counts['messages']}; phiên bản nhận: {counts['revisions']}. Đây chưa phải số báo cáo đã phân tích.")
            else:
                await message.answer('Lệnh quản trị riêng: /status /groups /report /report_today /errors /export [YYYY-MM-DD] /schedule_status /schedule_run /reprocess RAW_ID|all /disable_group CHAT_ID\nTrong nhóm: /chatid và /register_group [Tên hiển thị].')

    return dp


async def run_bot(settings: Settings) -> None:
    schedule_config = ScheduleConfig.load(settings.schedule_path, settings.admin_ids)
    database = Database(settings.database_path)
    if settings.test_group_id != -1:
        # Bootstrap only: never silently re-enable a disabled group on restart.
        database.register(settings.test_group_id, 'Nhóm thử nghiệm', reactivate=False)
    processor = ReportProcessor(database, ReportParser(settings.report_rules_path))
    replay = await asyncio.to_thread(processor.replay_pending)
    logger.info('Phát lại PENDING: xử lý=%s lỗi=%s', replay['processed'], replay['failed'])
    bot = Bot(settings.bot_token)
    export_lock = asyncio.Lock()
    schedule = None
    offset = None
    heartbeat = Heartbeat(settings.database_path)
    heartbeat.write('starting')
    try:
        schedule = ScheduleService(database, bot, ExportGenerator(database), schedule_config, export_lock)
        dp = create_dispatcher(settings, database, processor, schedule, export_lock)
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError('Bot đang có webhook; cần chủ động gỡ trước khi long polling.')
        me = await bot.get_me()
        logger.info('Bot @%s đã kết nối. SQLite sẵn sàng. Đang chờ tin nhắn.', me.username)
        schedule.start()
        # Sequential durable ingestion: acknowledge an update only AFTER database commit.
        # aiogram start_polling normally advances offsets even when a handler fails.
        transient_delay = 5
        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=25,
                    allowed_updates=['message', 'edited_message'])
            except TelegramRetryAfter as exc:
                heartbeat.write('retrying')
                await asyncio.sleep(exc.retry_after)
                continue
            except (TelegramNetworkError, TelegramServerError):
                heartbeat.write('retrying')
                logger.warning('Telegram temporarily unavailable; retrying in %s seconds.', transient_delay)
                await asyncio.sleep(transient_delay)
                transient_delay = min(transient_delay * 2, 60)
                continue
            heartbeat.write('polling')
            transient_delay = 5
            for update in updates:
                async with export_lock:
                    try:
                        stored = await asyncio.to_thread(database.collect, update)
                    except Exception:
                        logger.error('Không thể ghi SQLite. Dừng polling để không xác nhận mất dữ liệu.')
                        raise RuntimeError('Lỗi database; kiểm tra quyền ghi, dung lượng và khởi động lại.') from None
                    if stored:
                        message = update.message or update.edited_message
                        logger.info('Đã lưu: chat_id=%s message_id=%s update_id=%s', message.chat.id, message.message_id, update.update_id)
                    # Raw commit has completed. Processing errors must never lose the original.
                    raw_id = await asyncio.to_thread(database.raw_id_for_update, update)
                    if raw_id is not None:
                        try:
                            await asyncio.to_thread(processor.process, raw_id)
                            await schedule.notify_changes()
                        except Exception as exc:
                            logger.error('Phân tích lỗi (%s); raw_id=%s còn PENDING.', type(exc).__name__, raw_id)
                try:
                    await dp.feed_update(bot, update)
                except CommandClaimError:
                    logger.error('Unable to persist command claim; stopping polling without acknowledgement.')
                    raise
                except Exception as exc:
                    # No payload/token or raw exception text in logs.
                    logger.error('Lỗi phản hồi lệnh (%s); dữ liệu đã được ghi nếu thuộc nhóm cho phép.', type(exc).__name__)
                # A command can commit a reprocess/register change and then
                # fail sending its reply. Wake durable dirty work even then.
                # Claim failures above remain fatal and never acknowledge.
                try:
                    await schedule.notify_changes()
                except Exception as exc:
                    logger.error('Realtime notification failed (%s); durable work retained.', type(exc).__name__)
                offset = update.update_id + 1
    finally:
        heartbeat.write('stopped')
        try:
            if schedule is not None:
                await schedule.stop()
        finally:
            await bot.session.close()
