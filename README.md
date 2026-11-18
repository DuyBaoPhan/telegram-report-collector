# Public source configuration

This repository contains source, tests, documentation and a blank Excel template only. No runtime database, credentials or company reports are included. Configure `.env` from `.env.example`, then set real employee/group policies in `config/report_rules.json`. Scheduled and realtime delivery are disabled by default; enable them only after local validation. `null` policies yield NEED_REVIEW. Dashboard credentials are supplied separately as described in docs/DASHBOARD.md.

# Telegram Report Collector — Phases 1–6 (offline hardening)

Bot aiogram 3 long polling: SQLite, parser quy tắc, Excel, cron/realtime delivery và kiểm thử offline. Các mục giai đoạn cũ bên dưới giữ làm lịch sử; đọc mục mới nhất và [Windows runbook](docs/OPERATIONS.md) trước vận hành. Không AI, chưa tải attachment, chưa có FastAPI/admin UI/danh bạ nhân viên.

## Chuẩn bị trên Windows PowerShell

Yêu cầu Python 3.11 trở lên, kết nối Telegram Bot API và bot tạo qua BotFather. Không chia sẻ token.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env`, nhập `BOT_TOKEN`, `ADMIN_IDS` (Telegram **user ID số** của admin, phân cách dấu phẩy), và `TEST_GROUP_ID` (chat ID âm). Lấy user ID qua một ứng dụng Telegram đáng tin cậy hoặc thông tin quản trị sẵn có; không nhầm username với user ID.

Để lấy ID nhóm lần đầu, đặt tạm `TEST_GROUP_ID=-1`, chạy bot, thêm bot vào nhóm rồi admin gửi `/chatid@TenBot` trong nhóm. Bot chỉ phản hồi lệnh này cho user ID có trong ADMIN_IDS; ID nhóm sẽ hiển thị công khai trong nhóm. Dừng bot bằng Ctrl+C, điền ID thực vào `.env`, khởi động lại. Lệnh này không lưu tin nhắn từ nhóm chưa cấu hình.

```powershell
.\.venv\Scripts\python -m app.main
```

Trong chat riêng với bot, admin gửi `/start`, `/status`. Trong nhóm thử nghiệm, gửi một tin nhắn mới và kiểm tra console có `chat_id`, `user_id`, `message_id`. Với tin nhắn ẩn danh hoặc gửi thay mặt kênh, `user_id` có thể không có hoặc không đại diện người thật. Bot không trả lời tin nhắn nhóm thông thường và không đọc lịch sử cũ.

Để nhận các tin nhắn thông thường, cấu hình Privacy Mode bằng BotFather (`/setprivacy` → chọn bot → Disable), rồi kiểm tra lại quyền thành viên; có thể cần xóa và thêm lại bot. Nếu privacy bật, bot có thể chỉ nhận lệnh hoặc tin nhắn được Telegram cho phép. Không thể xác nhận cấu hình này chỉ từ mã nguồn.

Kiểm thử: ` .\.venv\Scripts\python -m pytest -q `. Chạy một instance duy nhất. Long polling không chạy đồng thời với webhook; nếu đã có webhook, quản trị viên cần chủ động gỡ qua Bot API trước khi chạy. Không xóa webhook tự động.

## Giới hạn và bước tiếp theo

Giai đoạn 1 chưa chứng minh hoàn tất cho đến khi nhận được tin nhắn thật. Sau khi xác nhận: Giai đoạn 2 quản lý nhiều nhóm và lưu SQLite chống trùng; Giai đoạn 3 parser và validation; Giai đoạn 4 Excel; Giai đoạn 5 scheduler và phân phối; Giai đoạn 6 kiểm thử tải, khôi phục và bảo mật. Không triển khai các bước này trước khi kiểm thử giai đoạn hiện tại.

## Giai đoạn 2 — SQLite và nhiều nhóm

Cấu hình tùy chọn `DATABASE_PATH=data/collector.db` trong `.env`; đường dẫn tương đối tính từ thư mục dự án. Mặc định tự dùng đường dẫn trên, không cần sửa `.env` đang có. Nhóm TEST_GROUP_ID được đăng ký một lần lúc khởi động (trừ -1); nhóm đã vô hiệu hóa không tự bật lại.

1. Dừng phiên bot cũ bằng Ctrl+C; chỉ chạy MỘT phiên. Nếu đang kích hoạt venv, dùng `python.exe -m app.main` như trước.
2. Khi khởi động thành công console hiện tên bot, SQLite sẵn sàng và chờ tin nhắn.
3. Thêm bot vào từng nhóm đã được doanh nghiệp cho phép. Admin cấu hình trong ADMIN_IDS gửi `/register_group Tên hiển thị` ngay trong nhóm. Dùng `/chatid` để xem ID. Không cần sửa source hoặc TEST_GROUP_ID cho mỗi nhóm mới.
4. Gửi tin nhắn rồi sửa nội dung. Console hiện `Đã lưu: ...`. Chat riêng admin gửi `/status`, `/groups`, `/report` để xem số liệu; /report là số tin nhắn lưu trữ, chưa phải số báo cáo hợp lệ.
5. Chat riêng admin dùng `/disable_group CHAT_ID` để ngừng thu thập, giữ nguyên dữ liệu cũ. Đăng ký lại bằng lệnh trong nhóm để bật lại. Không xóa nhóm vật lý vì có dữ liệu liên quan.
6. Dừng rồi khởi động lại, gửi tin nhắn mới và kiểm tra số liệu vẫn còn. Thực hiện với ít nhất BA nhóm trước khi xác nhận hoàn thành thực tế Giai đoạn 2.

### Dữ liệu và giới hạn

- `raw_messages`: nội dung hiện tại, chat/message ID, sender, thời gian Telegram, trạng thái PENDING và JSON gốc Telegram của tin nhắn đã cho phép.
- `message_revisions`: mỗi update ID duy nhất, giữ bản gốc và các lần chỉnh sửa. Khóa UNIQUE(chat_id,message_id) ngăn tạo nhiều tin nhắn cho cùng định danh.
- Tin nhắn riêng và nhóm chưa đăng ký/đã tắt không lưu. Lệnh /register_group đầu tiên chỉ bật thu thập cho các tin nhắn tiếp theo. Các lệnh trong nhóm đã đăng ký cũng có thể được lưu như tin nhắn gốc.
- Tệp đính kèm hiện chỉ lưu metadata trong JSON Telegram, CHƯA tải nội dung file. Parser, bảng employees/reports/attachments/export_history/system_logs, API và Excel sẽ bổ sung trong các giai đoạn sau. JSON Telegram có thể chứa tên và thông tin nhạy cảm: giới hạn quyền thư mục data cho tài khoản vận hành bằng quyền Windows, không chia sẻ database.
- Không ghi nội dung tin nhắn hoặc token vào console. Console chưa phải audit log bền vững.
- Polling tuần tự: commit dữ liệu trước khi tiến offset. Mất mạng khi polling sẽ thử lại; ghi DB lỗi thì dừng, không cố tình xác nhận update bị lỗi. Khi khởi động lại có thể nhận lại update cuối nhưng được chống trùng. Telegram chỉ giữ update chưa nhận trong thời hạn giới hạn (thường không quá 24 giờ); không đảm bảo phục hồi khi tắt lâu hoặc Telegram không cấp update.
- Chưa nhận thông báo xóa tin nhắn. Không cam kết thu hồi dữ liệu khi người gửi xóa tin nhắn trên Telegram.
- Khi sao lưu thủ công, dừng bot trước rồi sao chép database; không xóa các file WAL/SHM khi bot đang chạy. SQLite chưa mã hóa dữ liệu tại rest.

Kiểm thử tự động: `python -m pytest -q`. Các test dùng database tạm, không truy cập Telegram hoặc database vận hành; bao gồm 1.000 tin nhắn qua ba nhóm, chống trùng, chỉnh sửa, kiểm soát nhóm và rollback/retry khi ghi lỗi. Đây không thay thế kiểm thử ba nhóm thật, mất mạng thật hoặc kiểm thử tải đồng thời.

## Giai đoạn 3 — parser quy tắc và validation (không AI/Excel)

Parser chạy sau khi commit bản gốc vào SQLite. Khi khởi động, bot phát lại mọi raw message `PENDING` (kể cả dữ liệu giai đoạn 2). Nếu parser hoặc transaction lỗi, raw/revisions vẫn giữ nguyên, trạng thái vẫn `PENDING`, chỉ lưu tên loại lỗi, không ghi nội dung lỗi nhạy cảm. Không khởi chạy thêm một bot song song để thử.

### Quy tắc và mẫu báo cáo

Chỉnh `config/report_rules.json` (UTF-8); tùy chọn `REPORT_RULES_PATH` trong môi trường để dùng tệp khác. Nhãn là dòng `Nhãn: giá trị`, không phân biệt hoa/thường, chuẩn hóa Unicode NFC; nội dung công việc có thể nhiều dòng. Chỉ nhận diện báo cáo nếu có tiêu đề chính xác `Báo cáo`/`Bao cao`, nhãn mã báo cáo, hoặc ít nhất hai trường được nhận diện. Chat thông thường, media không caption, lệnh bot được đánh dấu `IGNORED`, không tạo báo cáo INVALID. Không suy đoán giá trị thiếu từ tên Telegram, ngày gửi hay nội dung tự do.

```text
Báo cáo
Mã báo cáo: BC-2026-001
Ngày báo cáo: 18/11/2026
Nhân viên: Nguyễn Văn An
Nội dung: Kiểm tra thiết bị
Hoàn thành biên bản
```

- `labels`: nhãn/alias theo trường; `required_fields`: bắt buộc (mẫu yêu cầu đủ bốn trường trên). `multiline_fields`: trường nối các dòng tiếp theo. Không hỗ trợ bảng, OCR hay file đính kèm.
- `date_formats`: mẫu có đủ ngày/tháng/năm; mặc định `dd/mm/yyyy` hoặc `yyyy-mm-dd`. Ngày lịch không tồn tại là INVALID. Không áp dụng quy tắc cấm ngày tương lai/ngày cũ khi chưa có yêu cầu doanh nghiệp. Nếu hai định dạng cấu hình hiểu khác nhau, NEED_REVIEW.
- `policies.employee_ids`: danh sách **Telegram user ID số** được phép; `policies.group_ids`: danh sách chat ID âm được phép nộp báo cáo (khác danh sách nhóm được thu thập); `submission_hours`: `[giờ_đầu, giờ_cuối]`, bao gồm hai đầu, 0–23, hỗ trợ qua nửa đêm. Theo timezone cấu hình `Asia/Ho_Chi_Minh`, xét thời điểm gửi ban đầu Telegram, không phải thời điểm sửa.
- Mặc định ba policy đều `null`: **NEED_REVIEW**, KHÔNG tự cho hợp lệ và KHÔNG tạo dữ liệu nhân viên giả. Điền quy tắc thật của doanh nghiệp trước khi xét VALID; `[]` từ chối mọi ID. Không có bảng danh bạ nhân viên hay xác minh tên hiển thị trong giai đoạn này. Tên báo cáo không được xác thực là tên pháp lý.
- `VALID`: đủ trường, ngày hợp lệ và chính sách đã cấu hình đều đạt. `INVALID`: thiếu trường/ngày sai/ID không phép/ngoài giờ. `NEED_REVIEW`: thiếu chính sách, trường lặp hoặc thời gian không xác định. `DUPLICATE`: mã chuẩn hóa trùng toàn hệ thống (không phân biệt hoa/thường/khoảng trắng); báo cáo không INVALID có raw ID nhỏ nhất làm chủ. INVALID không chiếm mã. Nếu có lỗi và cần duyệt đồng thời, INVALID ưu tiên; duplicate ưu tiên trên NEED_REVIEW cho bản trùng.
- Sửa mã, sửa thành INVALID hoặc sửa thành chat tự động tính lại chủ/trùng cho báo cáo liên quan trong cùng transaction. Bảng `reports` là snapshot hiện tại; `report_history` lưu kết quả xử lý/chuyển trạng thái, fingerprint quy tắc và revision nguồn; `message_revisions` giữ JSON gốc từng update. Không xóa/sửa raw originals để reprocess. Khi PENDING, snapshot trước đó có thể còn hiển thị cho đến lúc xử lý thành công; `/errors` liệt kê cả PENDING.
- Migration SQLite additive tạo `reports`, `report_history`, indexes và `user_version=2`, không xóa dữ liệu cũ. Dừng bot và sao lưu toàn bộ thư mục data trước nâng cấp; giữ cả WAL/SHM nếu SQLite đang mở. Chạy một instance duy nhất. Thuật toán đối chiếu duplicate quét snapshots sau mỗi lần xử lý: phù hợp quy mô nhỏ, chưa tối ưu tải lớn.

### Lệnh riêng cho admin

- `/report_today`: đếm theo trạng thái cho **ngày báo cáo** hôm nay theo timezone, không phải ngày nhận tin nhắn.
- `/errors`: tối đa 50 lỗi/NEED_REVIEW/DUPLICATE/PENDING mới nhất, hiển thị raw ID và mã lý do; không gửi raw report content.
- `/reprocess RAW_ID` hoặc `/reprocess all`: đánh dấu PENDING bền vững rồi xử lý lại nội dung hiện tại; giữ revision/history. Dùng `all` sau thay đổi rule và khởi động lại bot để nạp cấu hình mới. Raw ID lấy từ `/errors`; raw ID không tồn tại trả số xếp 0. Lệnh chọn một ID cũng chạy lại các PENDING đang tồn tại.

Các lệnh mới không phản hồi trong nhóm, không cho user ngoài ADMIN_IDS. `/register_group`, `/chatid`, `/groups`, `/disable_group`, `/status`, `/report` giữ hành vi cũ. `/status` và `/report` vẫn là số tin nhắn thu thập, không phải số báo cáo VALID.

### Kiểm thử Windows, không cần token hay Telegram

Từ PowerShell tại thư mục dự án; môi trường hiện có trong repo là `venv` (không phải `.venv`):

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q app tests
```

Nếu cài mới dùng `py -3.12 -m venv venv` trước. Gói `tzdata` là cần thiết cho timezone trên Windows. Bộ test dùng SQLite trong thư mục tạm pytest, fake Telegram message và mock lệnh; test cấu hình chặn đọc `.env`. Không kết nối bot, không cần truy cập token, không đụng database vận hành. Kiểm tra: parser tiếng Việt/alias/multiline, trường thiếu, ngày sai, chat ignored, policy, trùng, chỉnh sửa mã/trạng thái, audit/reprocess, migration legacy, rollback khi trigger DB lỗi, parser exception và phân quyền lệnh riêng. Chưa thay thế kiểm thử vận hành thật trên các nhóm được doanh nghiệp cho phép.

## Phase 4 — Excel export (sample template)

Phase 4 adds manual exports only; **there is no scheduler / automatic daily delivery**.
The included `templates/report_template.xlsx` is a clearly labelled sample, not a
customer-approved company layout. Replace it with an approved `.xlsx` and adjust
`config/excel_mapping.json` before relying on a specific business form.

### Windows setup / verification

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pytest -q
# Start only when you intend to connect the configured Telegram bot:
.\venv\Scripts\python.exe -m app.main
```

In a **private chat** with the bot, an authorized `ADMIN_IDS` user can send:

```text
/export
/export 2026-11-18
```

The default date is today in `Asia/Ho_Chi_Minh`. Dates must be `YYYY-MM-DD`.
The requesting admin receives the company workbook and one workbook for **every
registered group**, including disabled/empty groups (historical data is retained).
Unauthorized users and commands in groups receive no export/files. File names
include chat IDs, so identically named groups cannot overwrite each other.

### Mapping, output and audit

- The JSON mapping explicitly maps `work_content` to the **Task content** column.
  IDs are stored as text so leading zeroes and long Telegram IDs are preserved.
- Each export reads the latest parsed report state in one SQLite snapshot, selects
  exactly `report_date`, and puts only `VALID`, non-PENDING reports in details.
  Invalid/duplicate/review records appear in the Issues sheet and status counts.
  Reprocessing is still required after parser/rule changes. Raw text is not reparsed
  during export.
- `Summary` includes excluded status counts and missing-employee information.
  Missing is **UNKNOWN**, not zero, unless a complete roster is configured for every
  group in that workbook. `roster_by_chat_id` maps string chat IDs to lists of
  **numeric Telegram user IDs**, e.g. `{"-10": [42, 43]}`. This roster is independent
  of parser allowlists; an allowlist is not evidence of expected daily attendance.
- Undated/unparsed messages cannot be assigned to a report date and are not included
  in date-scoped counts. `/errors` remains the operational view for these records.
  Pending reports with an existing matching date are excluded and counted as PENDING.
- Exports are fresh workbooks under `data/exports/DATE/RUN_ID/`; reruns never append
  rows or overwrite a prior run. Original template files are never modified.
  Each workbook is saved to a temporary file and atomically renamed on the same
  filesystem. A set of several workbooks is not a single atomic transaction.
- SQLite `export_history` tracks STARTED / SUCCESS / FAILED per workbook with
  requesting admin, path and timestamps. SUCCESS means local file creation, **not
  Telegram delivery**. Network/send failures can be retried with `/export`; files
  remain local. Abrupt process termination can leave STARTED records / temporary
  files, requiring operator inspection. History failures fail closed.
- Untrusted workbook strings are explicitly literal text and formula prefixes are
  apostrophe-escaped. Template formulas are trusted and retained/translated from the
  first detail row. openpyxl does not calculate formulas: Excel recalculates on open.
  XML-illegal control characters are removed; text is limited to Excel's 32,767 chars.
- Template customization supports fixed headers and a first detail prototype row.
  Formulas/styles from that row extend for additional reports. Avoid fixed footers,
  macros, pivot tables, embedded unsupported objects, merged detail cells, or formulas
  requiring automatic total-range expansion; those need a custom approved mapping.
  Existing print settings, header merges and template formulas are retained. Only
  trusted operators should edit templates/mapping. No automatic retention/deletion
  policy is installed; exported files contain employee information, so restrict
  filesystem access and define an appropriate retention policy.

## Giai đoạn 5 — Xuất tự động theo lịch

Cấu hình trong `config/schedule.json`. Mặc định `enabled=false` để tránh gửi tự động trước khi quản trị viên kiểm thử. Lịch mẫu 09:00, 15:00, 17:00, múi giờ Asia/Ho_Chi_Minh. Sau khi sửa cấu hình cần dừng và khởi động lại bot. Biến môi trường tùy chọn `SCHEDULE_PATH` chọn file lịch khác.

- `enabled`: bật/tắt cron tự động; lệnh chạy thử vẫn hoạt động khi tắt.
- `times`: danh sách giờ HH:MM xuất bộ file công ty và nhóm.
- `group_times`: object ánh xạ chat ID âm (key chuỗi) đến danh sách HH:MM, lịch xuất thêm riêng cho nhóm. Các lịch này bổ sung chứ không thay thế lịch công ty.
- `recipient_admin_ids`: tùy chọn danh sách Telegram user ID nhận file. Nếu không cấu hình sẽ dùng ADMIN_IDS; chỉ cho phép tập con của ADMIN_IDS. Admin phải từng nhấn Start với bot và không chặn bot.
- `/schedule_status`: chat riêng admin xem trạng thái và tối đa 10 lượt gần nhất.
- `/schedule_run`: chat riêng admin chạy thử ngay luồng xuất/gửi; mỗi lệnh là một lượt thủ công riêng nên gửi lặp nếu gọi nhiều lần. Không cần chờ cron và không tự bật cron.

### Bật và kiểm thử trên Windows

Dừng bot bằng Ctrl+C và sao lưu thư mục data trước nâng cấp. Trong venv hiện tại:

```powershell
python.exe -m pip install -r requirements.txt
python.exe -m pytest -q
python.exe -m app.main
```

Đầu tiên gửi `/schedule_status` rồi `/schedule_run` trong chat riêng; kiểm tra file đến, trạng thái và thông báo thiếu nhân viên. Sau khi xác nhận, đặt `enabled=true` trong JSON, khởi động lại bot, kiểm tra trạng thái và chờ một mốc lịch thực tế. Để thử cron nhanh có thể thêm giờ vài phút sắp tới bằng HH:MM rồi gỡ lại sau kiểm thử; không sửa giờ hệ thống. Chỉ chạy một instance. Máy cần thức, có mạng và bot đang chạy; chưa cài Windows Service hoặc Task Scheduler khởi động cùng máy.

### Chống trùng và giới hạn vận hành

SQLite claim bền vững theo phạm vi và mốc phút trước khi xuất; cùng lượt đã claim không được tự chạy lại khi chồng lịch hoặc khởi động lại. Đây là **at-most-once attempt**, không phải cam kết exactly-once gửi Telegram. Lỗi gửi có thể khiến một số admin nhận đủ và một số không; không tự retry lượt đã claim vì không thể biết chắc Telegram đã nhận hay chưa. Crash có thể để CLAIMED chưa hoàn tất; quản trị viên kiểm tra trạng thái rồi chủ động dùng `/schedule_run` hoặc `/export` để xuất lại nếu cần.

Không có catch-up các mốc bỏ lỡ khi bot tắt rồi chạy lại. Khi tiến trình còn sống, APScheduler dùng misfire grace 60 giây, coalesce và max_instances=1. Không dùng cơ chế này thay kho lưu trữ lịch sử Telegram. File xuất thành công và gửi Telegram thành công là hai trạng thái khác nhau.

Cảnh báo thiếu báo cáo chỉ tính từ `roster_by_chat_id` trong `config/excel_mapping.json` (Telegram user IDs được kỳ vọng của từng nhóm), dựa trên báo cáo VALID trong ngày. Chưa có roster đầy đủ thì hiển thị UNKNOWN/chưa xác định, không tự coi là 0 hay suy ra từ thành viên Telegram. Đây là cấu hình thử nghiệm; cần thống nhất lịch, danh sách nhân viên và người nhận thật trước vận hành công ty.

## Realtime Excel export (report changes)

`config/schedule.json` now has a separate realtime switch (the existing cron `enabled`
value is unchanged):

```json
"realtime": { "enabled": true, "debounce_seconds": 10 }
```

After deterministic processing commits a change to a **VALID exported report**, the
bot waits for 10 quiet seconds, then exports one full-company workbook plus only
the affected groups for each affected report date. A burst resets this trailing
window. Ordinary chat, unchanged edits, and new invalid/duplicate reports do not
reset it. Date-changing edits export both old and new dates. Invalidating or
editing away a formerly valid report removes its exported row; duplicate-owner
promotion also marks the promoted report's date/group. Telegram does not deliver
normal message-deletion updates to this bot: deleting a message in Telegram alone
cannot trigger removal. Edit its report into ordinary text/invalid data, or use the
existing reprocessing workflow after a deliberate database maintenance operation.

Files are sent **only to the positive private admin IDs** selected by the existing
`recipient_admin_ids` / `ADMIN_IDS` validation; never to reporting groups. Admins
must first open/start the bot privately. Cron/manual/realtime generation shares
one in-process export lock. Group selection limits group workbooks, **not** the
company snapshot. Separate bot processes must not share this database; this is a
single-process polling service, not a distributed worker system.

### Durability and failure handling

Schema version 5 is additive: `report_export_state` retains the last committed
VALID projection and `realtime_dirty` records per-date/group revision counters in
the same transaction as parsing and duplicate reconciliation. Existing version-4
reports are baselined on migration, not mass-sent. Unfinished dirty revisions are
replayed when realtime starts, including after a restart. Turning realtime off
retains the queue. Generation failures (template, permissions, disk full, Excel
file locking) keep dirty work pending for a later change/restart; fix the cause
and restart the bot to retry. Shutdown cancels an unexpired debounce timer without
losing its durable work and drains already-running exports/sends before closing
the HTTP session.

Delivery uses **at-most-once attempts, not guaranteed delivery**. After all files
are generated successfully, the captured revisions are marked attempted durably
*before* contacting Telegram. `realtime_attempts` records `SUCCESS`, `PARTIAL`, or
`ATTEMPTING` (interrupted/uncertain). A timeout, blocked bot, or crash may mean some
or none of the documents arrived; these attempts are **not automatically resent**.
Newer revisions remain pending. Review the local attempt/export history and use a
private `/export YYYY-MM-DD` when an administrator explicitly wants another copy.
Exports remain on disk for recovery. Cron/manual scheduled-run history continues
to use the existing `scheduled_runs` table; `/schedule_status` shows realtime
configuration alongside that history.

### Windows operation

Restart the running collector after changing schedule configuration. Keep its
console/service running and Windows awake; no export occurs while the process or
machine is stopped. A restarted process replays pending revisions after the
configured quiet window; it does not replay Telegram deliveries already attempted.
Do not force-terminate during export/send if a graceful Ctrl+C/service stop is
possible. Keep writable space in the database/export folders, and close a locked
output workbook before retrying a generation failure. Each export has its own run
folder and files are replaced atomically. Test coverage uses isolated temporary
SQLite databases and fake Telegram clients; it does not contact live Telegram.

## Phase 6 — offline testing and operational hardening

See [Windows operating runbook and acceptance checklist](docs/OPERATIONS.md).
Phase 6 evidence uses generated aiogram updates, temporary SQLite files and fake
Telegram calls; it is **not** a claim of successful live Telegram load testing.

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q app tests scripts
# Opt-in, generated data only; never opens .env or production DB:
.\venv\Scripts\python.exe scripts/stress_offline.py --run --export-rounds 2
```

Runtime changes: refuse newer SQLite schemas (no silent downgrade), keep stale edit
revisions without replacing newer content, match parser history to the current raw
snapshot, sanitize startup failures, rotate app-only logs, and hold an OS lock for
one local database path. The local lock is not a distributed/token-global lock.

**Remaining scope:** downloading/validating attachments, FastAPI endpoints and admin
UI, managed employee directory, real approved template/roster/rules, deployment and
live Telegram acceptance remain unimplemented or unverified. Media metadata is
archived as untrusted JSON only: no file is fetched or executed, no OCR or content
validation is performed. No Docker or AI component is added.

Final generated-data verification and measured limitations: [Phase 6 results](docs/PHASE6_RESULTS.md).

## Dashboard giám sát cục bộ (chỉ đọc)

Có giao diện tiếng Việt sáng/tối để theo dõi heartbeat bot, nhóm Telegram, lưu lượng 7 ngày, chất lượng báo cáo, metadata tiếp nhận và lịch sử xuất/scheduler. Chạy độc lập bằng `python -m app.dashboard`, chỉ bind `127.0.0.1`; bắt buộc tài khoản/mật khẩu dashboard riêng, không cần BOT_TOKEN. Không đọc nội dung tin nhắn hoặc thực thi thao tác quản trị.

Xem [hướng dẫn Dashboard](docs/DASHBOARD.md) để cấu hình PowerShell, đăng nhập, giải thích heartbeat và giới hạn bảo mật. Dashboard không tự đọc `.env`; cần đặt DATABASE_PATH riêng nếu bot dùng đường dẫn tùy biến.
