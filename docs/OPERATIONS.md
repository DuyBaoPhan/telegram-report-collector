# Windows operating runbook — phase 6

## Scope and safety

Run all commands from the project root. This guide does not authorize accessing
production data, connecting a live bot, or deploying. Tests and the opt-in stress
script use generated data and temporary databases; they do not read `.env`.
A successful offline run does not prove Telegram permissions, BotFather privacy
configuration, real network recovery, or a production capacity guarantee.

## Before enabling a real bot

- Use a dedicated Windows account with restrictive NTFS permissions on `.env`,
  the database directory, exports, logs and backups. Do not put them in a public
  share or a cloud-synced directory. SQLite is not encrypted at rest.
- Keep one process per bot token. A same-database local advisory OS lock rejects
  a second instance started through `app.main`; different paths/machines/tokens
  are not coordinated. Use a local disk, not a network share. Do not delete the
  `.lock` file: OS releases its byte lock on exit/crash; the file remains normally.
- Review parser allowlists, working hours, company-approved Excel mapping/template,
  complete roster, recipients and scheduler settings. Do not assume sample values
  establish an employee directory, attendance policy or company-approved form.
- Disable automatic cron/realtime delivery until authorized recipients and tests
  are approved. Manual exports and schedule commands also send employee data.
- Do not open unknown attachments. Current code stores Telegram metadata only;
  attachment downloading, malware scanning, MIME/content validation, quarantine,
  size enforcement and OCR are **not implemented**. Telegram schema-valid metadata
  is not evidence of file safety or availability.

## Verify safely (no Telegram/token needed)

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q app tests scripts
.\venv\Scripts\python.exe scripts/stress_offline.py --run --export-rounds 2
```

The benchmark is opt-in: it generates 1,000 reports for 20 groups × 50 users/group,
measures ingestion/parser/export, checks counts and repeat export output. No timing
threshold is asserted: SQLite/disk/antivirus materially affect results. The regular
suite includes simulated duplicates, edits, concurrent collection, restart/replay,
rollback, export errors, mocked network retries, authorization, malformed metadata,
cron/realtime dirty state, formula injection and safe startup/logging checks.
Do not replace temporary test paths with a production path.

## Start, stop, failure recovery

1. Start only after authorization: `.\venv\Scripts\python.exe -m app.main`.
2. Stop with Ctrl+C in its console. Wait for normal exit before backup/restart;
   scheduler shutdown drains work already in flight. Do not repeatedly terminate
   an in-flight delivery to try to force retry.
3. On failure the main entrypoint prints/logs the exception **type**, not text or
   traceback. Check credentials/configuration locally without copying secrets
   into tickets. Network/auth startup failure exits nonzero; restart deliberately
   after remediation. Polling network errors retry, but database collection errors
   stop before advancing that update's offset.
4. Raw data commits before parsing; parser failures leave PENDING data to replay
   on next startup. Inspect `/errors` as an authorized private admin. Reprocess
   only after correcting the cause; do not edit raw originals in SQL.
5. Telegram only retains undelivered updates for a limited window (typically up
   to 24 hours). Restart is not a historical backfill guarantee. No message deletion
   events or guaranteed exactly-once file delivery are available. An interrupted
   send may have succeeded remotely; inspect recipients/history before rerunning.

## Backup and restore (operator action, not performed by tests)

1. Stop **all** bot instances and confirm their consoles exited. Use a trusted
   SQLite tool to check `PRAGMA integrity_check` if necessary; do not modify data.
2. Copy the entire configured database directory to a restricted, dated backup.
   Preserve the DB and any remaining `-wal`/`-shm` sidecars together. Never delete
   sidecars from an open database. Keep rules, mappings, template and schedule
   versions with the backup. Handle credentials separately in a secret store.
3. Verify backup contents and protect retention/access; exports contain personal
   information and are not substitutes for a database backup.
4. To restore: stop all instances; preserve a dated copy of the failed directory;
   restore the backed-up directory as a coherent set. Never mix a restored DB with
   unrelated WAL/SHM files. Use the application version that supports that schema.
   Startup refuses a schema newer than version 5 instead of downgrading it.
5. Verify integrity/counts and perform an isolated export before reconnecting a live
   bot. Restoring an old backup can replay updates or repeat previous delivery;
   coordinate reconciliation with admins. Test restore on a copy periodically.

## Logs and disk management

Runtime logs live in `logs/collector.log` next to the configured SQLite database,
rotating at 1,000,000 bytes with three backups (about 4 MB maximum plus current
record overhead). Only app log records are retained; dependency logs/tracebacks
are suppressed to prevent token URLs/update payloads leaking. Known token strings
are redacted. App code must continue to log identifiers/error types, never message
contents; the filter is not a universal personal-data detector. Restrict log access:
chat/message/update IDs still constitute metadata. Do not enable raw HTTP debugging.
Exports/DB/backups have **no automatic retention or disk quota**. Monitor free disk,
PENDING count and failed/abandoned export/schedule history; a full disk can halt
collection. STARTED/CLAIMED records from a crash need operator review, not blind
resending. Define retention with business/legal owners before automated cleanup.

## Known capacity and remaining product scope

The parser currently reconciles duplicate snapshots and realtime export projections
across all reports per processed message: roughly quadratic total work in report
count. Offline 1,000-report measurements are a smoke benchmark, not evidence for
unbounded history or sustained 20-group traffic. Production polling deliberately
processes ingestion sequentially; concurrent tests exercise DB safety, not a claim
of concurrent live polling. Large history needs a separately reviewed incremental
reconciliation design/index/query benchmark. Exports serialize with parser state
changes and can increase latency. WAL does not make multiple writers parallel.

Attachment downloading/content checks, FastAPI/API/admin web UI, a managed employee
directory and employee identity verification are absent. Roster configuration is
not a directory. Customer-approved template/rules/roster, live network/auth/group
permissions, real restart recovery and deployment acceptance remain outstanding.

## Acceptance checklist (record evidence, do not auto-check)

- [ ] Full offline pytest and compileall pass; benchmark output archived with date,
      machine details and revision/version (generated data only).
- [ ] Authorized manual test in 3–5 groups receives new messages and edits, repeated
      update does not duplicate, unauthorized/disabled/private data is not stored.
- [ ] Authorized controlled network outage/restart verified within Telegram retention;
      DB failure fails closed, recovery replay and backup restore verified on copies.
- [ ] 20-group/50-user-per-group business expectations and latency approved against
      measured workload; ongoing history growth monitored.
- [ ] Repeated manual/cron/realtime exports reviewed for correct latest state, isolated
      group scope, formulas treated safely, and ambiguous send/crash recovery policy.
- [ ] Attachment download requirement explicitly accepted as unmet or implemented
      and independently tested before claiming complete attachment support.
- [ ] Managed directory/API/admin UI scope explicitly signed off as remaining.
- [ ] NTFS access, secret handling, retention, disk monitoring and incident owners set.


## Review fixes 2 and 5 — schema 7 (offline verified)

- Accepted raw-message state now stores its internal `current_revision_id`. Edit timestamp ties compare the Telegram update ID of that accepted revision, **not** the maximum ID among historical/audit events. Timestamp-newer edits still survive Telegram ID reseeding; stale audited revisions cannot block subsequent same-second edits. Exact event replay remains suppressed by the canonical event fingerprint; colliding Telegram IDs remain distinct events.
- An additive, atomic schema-7 migration backfills current revision references from existing raw JSON and introduces `admin_command_attempts`. It preserves raw payloads and history; newer database versions are refused before mutation.
- Supported authorized commands are durably claimed in the central handler middleware, before any command effect (also for private commands not collected as reports and direct dispatcher calls). Unauthorized traffic and ordinary report messages create no command ledger entries. Edited commands are never executed.
- The ledger keeps only a SHA-256 event identity, supported command name, status, and timestamps: no private command arguments, raw payloads, error strings, or recipient metadata. All existing ledger entries suppress automatic replay, including FAILED/UNCERTAIN attempts. On restart a leftover CLAIMED attempt is marked UNCERTAIN. A fresh command message is an intentional new attempt.
- This is **at-most-once attempt**, not exactly-once Telegram delivery. A crash after claiming but before acting can lose a command; a timeout may have delivered a reply even when the local attempt is FAILED. Send a **fresh command** after checking actual outcomes; do not expect replay of an old command to retry it. A handler that completes normally (including a handled error response) records SUCCESS; uncaught exceptions record FAILED; cancellation/crash is uncertain. A failure to finalize status retains the durable claim and still suppresses replay.
- If the initial claim cannot be persisted, command effects are not run and polling stops **without advancing its Telegram offset**. Repair storage and restart. No raw exception/payload is logged or replied to.

### Windows upgrade/backup procedure

Stop the bot first (`Ctrl+C`) and ensure no other instance is running. Back up the entire `data` directory while stopped, including any SQLite `-wal` and `-shm` files. Example PowerShell (from the project directory):

```powershell
$backup = "data_backup_$(Get-Date -Format yyyyMMdd_HHmmss)"
Copy-Item -LiteralPath .\data -Destination $backup -Recurse
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q app tests
# Then start the bot using your normal documented launch command.
```

The next application initialization migrates the database transactionally. Do not open production data with an older application after migration. For rollback, stop the application and restore the **whole** stopped backup with its matching application version; never mix a database with unrelated WAL files. The offline review ran only against temporary test databases: no production DB migration, live Telegram command, or `.env` read was performed.

## Template ownership and scheduled missing-count consistency (2026-11-18)

Custom templates must leave generator-owned data cells blank: mapped columns on the
configured detail sheet from `first_data_row` onward, and Issues A:F from row 3
onward. Both formulas and nonempty constants (including zero, False and whitespace)
are rejected; `None`/empty strings are blank. Source templates are never cleared or
rewritten. Remove stale example/report/issue values from a separate reviewed copy
before configuring it. Existing formulas outside mapped columns and metadata
placeholder labels remain supported. Invalid templates create FAILED export-history
rows and no documents are sent for failed scopes; scheduled runs still send their
failure notification and finish PARTIAL under the existing status contract.

Scheduled export and its subsequent missing query now share the ingestion/reprocess
lock. This prevents the supported single-process ingestion path from changing report
state between the workbook snapshot and notification query. The lock is released
before Telegram documents/messages. It is not a cross-process database snapshot:
external writers, another collector process, or direct callers ignoring the shared
lock are unsupported and can still cause divergence. Company workbook summaries
still include all registered groups, including disabled groups; scheduled missing
notifications intentionally cover only active target groups. Those totals need not
match when their scopes differ. No roster means UNKNOWN, not zero.

Restart is required to load these code changes: stop the old bot, back up the full
configured data directory while stopped, then start with the existing venv using
`venv\Scripts\python.exe -m app.main`. Do not hot-update or start a second instance.
No new dependency or schema migration was added (schema remains 7). Verify a fresh
manual export and a scheduled/manual schedule run using an approved clean template.
