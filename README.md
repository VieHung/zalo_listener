# Zalo Web Listener

Listener chỉ-đọc cho Zalo Web: kết nối vào một phiên Brave đã đăng nhập, quan sát
HTTP/WebSocket, thử giải mã payload, trích xuất tin nhắn và lưu vào SQLite.

> Chỉ sử dụng với tài khoản, hội thoại và dữ liệu mà bạn có quyền thu thập và xử
> lý. Project không tự đăng nhập, không vượt CAPTCHA/MFA và không gửi tin nhắn.

## Cách hoạt động

```text
Brave đã đăng nhập Zalo Web
  -> inject.js bắt HTTP, XMLHttpRequest và WebSocket
  -> binding __ZL_SINK chuyển sự kiện về Python
  -> crypto.py bóc frame và giải mã Base64/deflate/AES-GCM
  -> extract.py nhận dạng message theo heuristic
  -> privacy.py giả danh UID và che PII
  -> store.py ghi dữ liệu vào SQLite
```

Listener chỉ nhận dữ liệu mà trang Zalo tải sau khi hook được kích hoạt. Để lấy
thêm tin nhắn, hãy mở hoặc cuộn các cuộc trò chuyện trong tab Zalo, hoặc chờ tin
nhắn mới. Đây không phải công cụ đồng bộ toàn bộ lịch sử tài khoản.

## Yêu cầu

- Python 3.10 trở lên
- Brave Browser hoặc một trình duyệt Chromium hỗ trợ CDP
- Một tài khoản đã đăng nhập thủ công tại `https://chat.zalo.me/`

## Cài đặt

Từ thư mục `zalo_listener`:

```bash
python -m venv .venv
.venv/bin/pip install PyYAML playwright pycryptodome
mkdir -p data .runtime/brave-profile
```

Không cần chạy `playwright install`: listener kết nối vào Brave có sẵn qua CDP,
thay vì khởi chạy browser do Playwright quản lý.

## Cấu hình

Tạo file `config.yaml` trong thư mục project:

```yaml
cdp:
  endpoint: "http://127.0.0.1:9222"
  target_url_contains: "chat.zalo.me"
  reload_on_attach: true

storage:
  sqlite_path: "/home/taviethung/Storage_2/Work/Company/MB/zalo_listener/data/zalo.sqlite3"

capture:
  url_filter: "zalo"
  http: true
  websocket: true
  dom: false
  dom_selector: '[class*="message"]'
  store_raw_events: false

crypto:
  # Để rỗng để listener thử tìm key từ phiên Zalo đang đăng nhập.
  secret_key_b64: ""

filter:
  group_only: false
  only_thread_ids: []
  skip_thread_ids: []

privacy:
  pseudonymize_uid: true
  store_display_name: false
  scrub_pii: true
  salt: "THAY_BANG_CHUOI_NGAU_NHIEN_DAI"

runtime:
  stats_every_seconds: 15
```

Sinh salt ngẫu nhiên:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_hex(32))"
```

Không commit `config.yaml` nếu file chứa secret key hoặc thông tin cấu hình nhạy
cảm.

### Bộ lọc hội thoại

- `group_only: true`: chỉ giữ message được nhận dạng là thuộc nhóm.
- `only_thread_ids`: nếu không rỗng, chỉ giữ các thread ID được liệt kê.
- `skip_thread_ids`: bỏ qua các thread ID được liệt kê.

Nên bắt đầu với `group_only: false`, vì schema Zalo có thể thay đổi và một số
message nhóm có thể chỉ được nhận dạng là `unknown`.

## Mở Brave với CDP

Chạy trong terminal thứ nhất:

```bash
brave-browser \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="/home/taviethung/Storage_2/Work/Company/MB/zalo_listener/.runtime/brave-profile" \
  "https://chat.zalo.me/"
```

Sau đó đăng nhập Zalo thủ công và giữ cửa sổ Brave này mở.

Không mở cổng CDP ra mạng. Bất kỳ tiến trình nào truy cập được cổng này đều có
thể có khả năng điều khiển phiên browser.

## Chạy listener

Vì thư mục project đồng thời là package `zalo_listener`, hãy chạy module từ thư
mục cha:

```bash
cd /home/taviethung/Storage_2/Work/Company/MB
zalo_listener/.venv/bin/python -m zalo_listener.main \
  -c zalo_listener/config.yaml
```

Khi kết nối thành công, log sẽ có dạng:

```text
[cdp] nối tới http://127.0.0.1:9222 ...
[hook] sẵn sàng trên https://chat.zalo.me/
[key] tìm thấy secret key từ localStorage:...
[run] Đang lắng nghe. Mở/đọc các nhóm để tin nhắn mới chảy về.
```

Nhấn `Ctrl+C` để dừng. Project dùng file lock theo đường dẫn database và sẽ từ
chối khởi động nếu đã có listener khác ghi cùng database.

## Kiểm tra dữ liệu

Mở database:

```bash
sqlite3 data/zalo.sqlite3
```

Một số truy vấn hữu ích:

```sql
.tables

SELECT
    datetime(ts_ms / 1000, 'unixepoch', 'localtime') AS sent_at,
    thread_id,
    direction,
    uid_from,
    msg_type,
    text
FROM messages
ORDER BY ts_ms DESC
LIMIT 30;

SELECT thread_id, thread_type, msg_count
FROM threads
ORDER BY msg_count DESC;

SELECT COUNT(*) AS raw_event_count
FROM raw_events;
```

Các bảng chính:

| Bảng | Nội dung |
| --- | --- |
| `messages` | Tin nhắn đã trích xuất, khử trùng theo `msg_id`; `direction` là `incoming`, `outgoing` hoặc `unknown` |
| `threads` | Thống kê hội thoại/nhóm |
| `users` | UID đã giả danh và tên hiển thị nếu được cho phép |
| `raw_events` | Payload thô, chỉ được ghi khi bật lưu raw |

## Chế độ discover

Khi schema Zalo thay đổi và listener không nhận dạng được message, có thể chạy:

```bash
cd /home/taviethung/Storage_2/Work/Company/MB
zalo_listener/.venv/bin/python -m zalo_listener.main \
  -c zalo_listener/config.yaml \
  --discover
```

Chế độ này ghi toàn bộ sự kiện vào `discover/events_<timestamp>.jsonl` ở thư
mục cha của package. Chỉ bật trong thời gian ngắn để chẩn đoán.

## Quyền riêng tư và bảo mật

Mặc định nên dùng:

```yaml
capture:
  store_raw_events: false

privacy:
  pseudonymize_uid: true
  store_display_name: false
  scrub_pii: true
```

Các cột chuẩn hoá trong `messages` được áp privacy filter, nhưng khi
`store_raw_events: true`:

- `raw_events.payload` có thể chứa payload gốc chưa che PII;
- `messages.raw_json` có thể chứa UID, tên và nội dung gốc;
- phản hồi dùng để lấy key có thể chứa dữ liệu nhạy cảm.

Ngoài ra, `--discover` ghi sự kiện trước khi lọc và có thể ghi cả secret key lấy
từ `localStorage`. Không chia sẻ database, file discover hoặc browser profile.

## Khắc phục sự cố

### `No module named playwright` hoặc `No module named Crypto`

Đảm bảo đang dùng đúng virtual environment:

```bash
.venv/bin/pip install PyYAML playwright pycryptodome
```

### Không kết nối được CDP

Kiểm tra Brave có được mở với `--remote-debugging-port=9222` hay không:

```bash
curl --fail-with-body http://127.0.0.1:9222/json/version
```

Nếu không có phản hồi, đóng tiến trình Brave dùng profile listener rồi chạy lại
lệnh ở phần “Mở Brave với CDP”.

### Có sự kiện nhưng không có message

Kiểm tra dòng thống kê:

- `events > 0`, `decoded = 0`: listener chưa tìm được key hoặc cách mã hoá đã
  thay đổi.
- `decoded > 0`, `new_msgs = 0`: schema message có thể đã thay đổi hoặc bộ lọc
  đang loại message.
- `group_only: true`: thử tạm chuyển thành `false`.

Chỉ khi cần dò schema, bật `--discover` trong thời gian ngắn và bảo vệ file đầu
ra như dữ liệu nhạy cảm.

## Giới hạn hiện tại

- Framing WebSocket hiện hỗ trợ header 4 byte và các chế độ Base64, deflate,
  AES-GCM đang quan sát được; Zalo vẫn có thể thay đổi protocol bất kỳ lúc nào.
- Listener không tự lấy toàn bộ lịch sử; dữ liệu phụ thuộc vào những gì tab Zalo
  thực sự tải.
- Tên thread chưa được tự động điền vào bảng `threads`.
- Chiều tin nhắn được suy ra bằng cách so sánh `uidFrom` với `userId`; nếu Zalo
  không gửi đủ hai field thì giá trị sẽ là `unknown`.

## Cấu trúc mã nguồn

| File | Vai trò |
| --- | --- |
| `main.py` | Kết nối CDP, nhận sự kiện, lọc và điều phối lưu trữ |
| `inject.js` | Hook `fetch`, `XMLHttpRequest`, `WebSocket` và DOM tùy chọn |
| `crypto.py` | Chuẩn hoá key và thử giải mã AES-CBC/AES-ECB |
| `extract.py` | Duyệt JSON và nhận dạng message theo nhiều tên field |
| `privacy.py` | Chuẩn hoá Unicode, giả danh UID và che PII |
| `store.py` | Tạo schema và ghi SQLite |
