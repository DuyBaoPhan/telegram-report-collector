# Dashboard giám sát cục bộ

Dashboard tiếng Việt, sáng/tối, responsive; đọc dữ liệu SQLite thật, không có dữ liệu minh họa tích hợp và không gửi lệnh Telegram. Khởi chạy độc lập với bot để việc mở/đóng trình duyệt không ảnh hưởng thu thập.

## Khởi chạy trên Windows PowerShell

Mở **terminal thứ hai**, giữ nguyên terminal bot đang chạy:

```powershell
cd D:\telegram-report-collector
.\venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DASHBOARD_USERNAME = 'admin'
$secure = Read-Host 'Mat khau dashboard (it nhat 12 ky tu)' -AsSecureString
$env:DASHBOARD_PASSWORD = [System.Net.NetworkCredential]::new('', $secure).Password
# Chỉ đặt nếu bot không dùng data/collector.db. Phải trỏ cùng DB với bot:
# $env:DATABASE_PATH = 'D:\duong-dan\collector.db'
$env:DASHBOARD_PORT = '8000'
.\venv\Scripts\python.exe -m app.dashboard
```

Mở http://127.0.0.1:8000 trong trình duyệt và nhập tài khoản/mật khẩu vừa đặt trong hộp thoại HTTP Basic. Mật khẩu không hiện trong terminal, URL hoặc mã nguồn frontend; không dùng BOT_TOKEN làm mật khẩu dashboard. Biến môi trường chỉ tồn tại trong terminal này và tiến trình con. Sau khi dừng bằng Ctrl+C, có thể xóa:

```powershell
Remove-Item Env:DASHBOARD_PASSWORD
```

Dashboard **không tự đọc `.env`** và không cần BOT_TOKEN/ADMIN_IDS. DATABASE_PATH cần được đặt trong terminal dashboard nếu đường dẫn của bot tùy biến. Không đặt mật khẩu thật vào tài liệu, lịch sử lệnh hoặc commit. Tài khoản mặc định là admin; mật khẩu bắt buộc ít nhất 12 ký tự. Không đủ cấu hình thì server từ chối khởi động.

## Phạm vi và cách hiểu dữ liệu

- Tiếp nhận hôm nay và biểu đồ 7 ngày tính theo UTC+7, dựa `submitted_at` (thời gian Telegram gửi), không phải thời điểm ghi DB. Tổng tin nhắn là bản ghi hiện tại, không đếm số revision chỉnh sửa.
- Nhóm đang theo dõi là nhóm đã đăng ký có `active=1`; không suy ra bot đang là thành viên/đủ quyền Telegram từ cờ này.
- Báo cáo hợp lệ, không hợp lệ, trùng lặp, cần kiểm tra lấy trạng thái phân tích hiện hành. “Cần xử lý” cộng PENDING + INVALID + NEED_REVIEW; số có lỗi xử lý có thể trùng PENDING nên không cộng thêm lần nữa.
- Tìm kiếm nhóm theo tên/Chat ID, lọc active/inactive, phân trang nhóm và metadata tin nhắn. Không trả nội dung thô, JSON Telegram, trường báo cáo, tên người gửi, lỗi nguyên văn hay đường dẫn tệp xuất.
- Lịch sử 8 lần xuất và 8 lần scheduler gần nhất chỉ để quan sát. Không tải tệp, chạy xuất, sửa nhóm, hay thay đổi cấu hình từ dashboard. Phân vùng realtime chờ xuất là `revision > completed_revision`; lịch sử realtime là các lần đã thực thi, không phải cấu hình bật/tắt hiện tại.
- Khi tab hiển thị, kiểm tra thay đổi mỗi khoảng 1 giây qua `/api/changes`; chỉ tải lại dữ liệu khi có thay đổi hoặc thao tác lọc/phân trang/làm mới. Tab ẩn tạm dừng kiểm tra, trở lại tab tải ngay. Lỗi mạng/DB tự thử lại với thời gian chờ tăng dần tối đa 15 giây; mỗi request có timeout 8 giây, không chồng vòng poll. Dữ liệu giữa các endpoint có thể chênh nhẹ nếu bot đang ingest; trong mỗi endpoint overview là một read transaction nhất quán. Khi lỗi DB, không thay số cũ bằng số 0; banner cảnh báo giữ rõ trạng thái lỗi.

## Heartbeat bot

Bot phiên bản mới ghi file cạnh DB: `<database-path>.heartbeat.json`, thay file nguyên tử, chỉ chứa thời gian và trạng thái vận hành, không chứa token/payload. Ghi thất bại không dừng thu thập. Cần **khởi động lại bot một lần vào thời điểm phù hợp** để mã heartbeat mới hoạt động; không cần restart bot chỉ để mở dashboard về sau.

- **polling**: lần getUpdates gần nhất thành công, kể cả trả về 0 tin. Không cam kết kết nối tương lai.
- **retrying**: nhận lỗi mạng/server hoặc Telegram RetryAfter, đang chờ thử lại.
- **starting**: chưa xác nhận polling thành công.
- **stopped**: tiến trình đã đi qua finally dừng. Dừng cưỡng bức có thể không ghi được.
- **stale**: heartbeat hơn 90 giây chưa cập nhật; cần kiểm tra tiến trình/mạng. Đợt xử lý lớn hoặc chờ RetryAfter dài cũng có thể gây trạng thái này.
- **unknown**: chưa có file, file lỗi hoặc bot phiên bản cũ. Không suy đoán ONLINE từ tin nhắn mới nhất.

## Bảo mật và đồng thời

- Chỉ bind `127.0.0.1`, không có tùy chọn host. Không dùng tunnel, reverse proxy, port-forward hoặc public hosting: HTTP Basic trên HTTP chỉ phù hợp loopback máy tin cậy. Muốn truy cập từ xa cần thiết kế TLS/xác thực riêng, ngoài phạm vi MVP này.
- Tất cả route (HTML, JS, CSS và API) yêu cầu xác thực; so sánh credentials constant-time. Chỉ GET/HEAD được phép. Không có cookie phiên/token frontend và không lưu credentials trong localStorage (chỉ lưu lựa chọn sáng/tối).
- Trình duyệt có thể giữ HTTP Basic đến khi đóng phiên/profile. Không có logout hữu hiệu mọi trình duyệt; dùng profile riêng/đóng trình duyệt nếu dùng máy chung. Đổi mật khẩu và restart server để thu hồi mật khẩu cũ.
- SQLite mở bằng `mode=ro`, `query_only=ON`, timeout ngắn, không khởi tạo/migrate DB. Bot vẫn sở hữu ghi DB theo WAL. Không sao chép DB đang chạy bằng copy đơn lẻ; dùng quy trình backup được tài liệu chính hướng dẫn.
- DB không tồn tại, hỏng, thiếu bảng, bận hoặc sai schema trả thông báo chung 503, không stack trace/đường dẫn/lỗi thô. Overview chỉ hỗ trợ schema 7. Không truy cập DB production trong test.
- Không ghi access log chứa URL/auth. Frontend dùng `textContent`, không HTML từ dữ liệu; CSP chặn script/CDN ngoài, frame embedding và form submission. Tất cả tài nguyên phục vụ offline, không gọi CDN.
- Giới hạn trang 1–100000, kích thước trang 1–50 và chuỗi tìm 100 ký tự; SQL tham số hóa, wildcard tìm kiếm được escape. Không có route mở file tùy ý.
- Đọc DB và các tổng hợp có chi phí tăng theo dữ liệu. Đây là monitor cục bộ cho một operator, chưa phải hệ thống nhiều người hoặc lưu lượng hàng triệu bản ghi; cân nhắc index/materialized aggregates và đo tải trước khi mở rộng.

## Kiểm thử

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q app tests
```

Test dashboard dùng DB tạm schema thật, xác minh xác thực mọi route, cấu hình bắt buộc, ràng buộc filter/phân trang, metadata-only, SQL injection/wildcard, query_only, DB vắng/sai schema và heartbeat. Không đọc `.env`, token hoặc DB đang vận hành.

## Cập nhật gần thời gian thực

Sau khi cập nhật mã, khởi động lại riêng tiến trình dashboard và nhấn **Ctrl+F5** trong trình duyệt để nhận JavaScript mới. Không cần restart bot chỉ vì thay đổi cơ chế dashboard này.

`GET /api/changes` yêu cầu cùng HTTP Basic như mọi route, trả duy nhất token SHA-256 mờ, không trả đường dẫn/nội dung DB. Endpoint chỉ đọc metadata filesystem của DB, WAL và heartbeat (không mở SQLite, không tạo file). Token còn xét trạng thái heartbeat quá hạn và ngày báo cáo UTC+7. Nhờ WAL, các insert, edit, đổi trạng thái xử lý/báo cáo, export, scheduler và realtime đều kích hoạt tải lại, kể cả chưa checkpoint DB chính. Token là **gợi ý invalidation filesystem**, không phải event log hay snapshot giao dịch; có thể tải lại dư khi checkpoint, và không bảo đảm phát hiện thao tác ngoài ứng dụng cố ý giữ nguyên toàn bộ metadata. Mỗi vòng poll lấy token trước khi đọc dữ liệu nên ghi xảy ra giữa các request được kiểm tra tiếp ở vòng sau. Không SSE/WebSocket, không thay schema hay thêm dependency. Độ trễ thường khoảng 1 giây cộng thời gian request, không phải cam kết thời gian thực cứng.
