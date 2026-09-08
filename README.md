# Zalo Web Listener

Listener chỉ-đọc cho Zalo Web: kết nối vào một phiên Brave đã đăng nhập, quan sát
HTTP/WebSocket, thử giải mã payload, trích xuất tin nhắn và lưu vào SQLite. Kèm
một module tách rời đẩy tin nhắn realtime lên Google Sheets, phân loại theo
keyword, mỗi thread một sheet con đặt theo tên nhóm.

> Chỉ sử dụng với tài khoản, hội thoại và dữ liệu mà bạn có quyền thu thập và xử
> lý. Project không tự đăng nhập, không vượt CAPTCHA/MFA và không gửi tin nhắn.

## Toàn cảnh: hai tiến trình

Hệ thống gồm **ba tiến trình chạy song song**, mỗi cái một terminal:

| # | Tiến trình | Vai trò |
| --- | --- | --- |
| 1 | **Brave + CDP** | Trình duyệt đã đăng nhập Zalo, mở cổng debug 9222 |
| 2 | **Listener** (`main.py`) | Hook traffic Zalo, giải mã, ghi tin nhắn + tên nhóm vào SQLite |
| 3 | **Sheets sync** (`sheets_sync.py`) | Đọc SQLite, phân loại, đẩy realtime lên Google Sheets |

Tiến trình 3 là tuỳ chọn — bỏ qua nếu chỉ cần dữ liệu trong SQLite.

## Cách hoạt động

```text
Brave đã đăng nhập Zalo Web
  -> inject.js bắt HTTP, XMLHttpRequest và WebSocket
  -> binding __ZL_SINK chuyển sự kiện về Python
  -> crypto.py bóc frame và giải mã Base64/deflate/AES-GCM
  -> extract.py nhận dạng message + bắt tên nhóm/tên người (name hints)
  -> privacy.py giả danh UID và che PII
  -> store.py ghi tin nhắn vào messages, tên nhóm vào threads.name

  (song song, đọc lại từ SQLite)
  -> sheets_sync.py đọc tin mới -> classify.py gắn nhãn keyword
  -> đẩy lên Google Sheets, mỗi thread một sheet con theo tên nhóm
```

Listener chỉ nhận dữ liệu mà trang Zalo tải sau khi hook được kích hoạt. Để lấy
thêm tin nhắn, hãy mở hoặc cuộn các cuộc trò chuyện trong tab Zalo, hoặc chờ tin
nhắn mới. Đây không phải công cụ đồng bộ toàn bộ lịch sử tài khoản.

## Yêu cầu

- Python 3.10 trở lên (cần SQLite hỗ trợ generated column, tức 3.31+)
- Brave Browser hoặc một trình duyệt Chromium hỗ trợ CDP
- Một tài khoản đã đăng nhập thủ công tại `https://chat.zalo.me/`
- (Nếu dùng Google Sheets) một project Google Cloud để tạo service account

## Cài đặt

Từ thư mục `zalo_listener`:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p data .runtime/brave-profile
```

`requirements.txt` gồm: `PyYAML`, `playwright`, `pycryptodome` (cho listener) và
`gspread`, `google-auth` (cho module Google Sheets). Nếu không dùng Sheets, hai
gói cuối có thể bỏ.

Không cần chạy `playwright install`: listener kết nối vào Brave có sẵn qua CDP,
thay vì khởi chạy browser do Playwright quản lý.

## Cấu hình

Copy file mẫu rồi chỉnh (`config.yaml` đã được `.gitignore` vì chứa `salt`,
`spreadsheet_id`, thread nội bộ — không commit):

```bash
cp config.example.yaml config.yaml
```

Nội dung `config.yaml`:

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

Ngoài các khối trên, `config.yaml` trong repo còn hai khối `sheets:` và
`classify:` phục vụ module Google Sheets — xem mục
[Đồng bộ lên Google Sheets](#đồng-bộ-lên-google-sheets). Listener bỏ qua hai khối
này nên có thể để nguyên nếu chưa dùng Sheets.

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

-- ts_local là cột giờ VN đọc được, tự suy từ ts_ms (vd "09:02 08/09/2026")
SELECT ts_local, thread_id, direction, uid_from, msg_type, text
FROM messages
ORDER BY ts_ms DESC
LIMIT 30;

-- Thread kèm tên nhóm/tên người đã bắt được (name = NULL nếu chưa về)
SELECT thread_id, thread_type, name, msg_count
FROM threads
ORDER BY msg_count DESC;

SELECT COUNT(*) AS raw_event_count
FROM raw_events;
```

Các bảng chính:

| Bảng | Nội dung |
| --- | --- |
| `messages` | Tin nhắn đã trích xuất, khử trùng theo `msg_id`; `ts_local` là giờ VN đọc được (generated từ `ts_ms`); `direction` là `incoming`, `outgoing` hoặc `unknown` |
| `threads` | Hội thoại/nhóm; `name` là tên nhóm/tên người do listener tự bắt (dùng để đặt tên sheet) |
| `users` | UID đã giả danh và tên hiển thị nếu được cho phép |
| `raw_events` | Payload thô, chỉ được ghi khi bật lưu raw |

## Đồng bộ lên Google Sheets

Module `sheets_sync` chạy **tách khỏi** listener: đọc tin nhắn mới từ DB (chỉ
đọc, read-only nên không tranh chấp), phân loại theo keyword, rồi đẩy realtime
lên một Google Spreadsheet — **mỗi thread một sheet con**, tên sheet lấy theo
tên nhóm/tên người (`threads.name`, do listener tự bắt), fallback về `thread_id`.

### 1. Tạo Service Account

1. Vào [Google Cloud Console](https://console.cloud.google.com/) → tạo project.
2. Bật **Google Sheets API** (APIs & Services → Enable APIs).
3. Tạo **Service Account** → tạo **key** dạng JSON → tải về, lưu vào
   `data/service_account.json` (hoặc đường dẫn bất kỳ, khớp `sheets.credentials_json`).
4. Mở file JSON, copy `client_email` (dạng `...@...iam.gserviceaccount.com`).
5. Tạo một Google Sheet, bấm **Share** và mời email đó với quyền **Editor**.
6. Lấy `spreadsheet_id` từ URL: `docs.google.com/spreadsheets/d/<ID>/edit`.

### 2. Cấu hình `config.yaml`

Xem khối `sheets:` và `classify:` trong `config.yaml`. Bắt buộc:

- `sheets.credentials_json`: đường dẫn file JSON service account.
- `sheets.spreadsheet_id` (hoặc `spreadsheet_url`).
- `sheets.track_thread_ids`: danh sách thread cần đẩy (lấy từ bảng `threads`).
- `classify.categories`: map `"Tên nhãn": [keyword, ...]` — so khớp bỏ dấu,
  không phân biệt hoa thường, một tin có thể mang nhiều nhãn.

Tuỳ chọn: `sheets.thread_names` để ép tên sheet, `backfill: true` để đẩy cả
lịch sử cũ (mặc định chỉ đẩy tin mới từ lúc chạy), `columns` để chọn cột.

### 3. Chạy

```bash
cd /home/taviethung/Storage_2/Work/Company/MB
zalo_listener/.venv/bin/python -m zalo_listener.sheets_sync \
  -c zalo_listener/config.yaml
```

Chạy **song song** với listener. Vị trí đã xử lý được nhớ bằng `rowid` trong
`data/sheets_sync.state.json` nên khởi động lại không ghi trùng. Thêm `--once`
để chạy một vòng rồi thoát (tiện để test cấu hình).

> Tên nhóm được listener bắt dần từ traffic Zalo; nếu một sheet ban đầu mang tên
> `thread-<id>`, nó sẽ **tự đổi tên** khi tên nhóm về. Muốn có tên ngay, khai báo
> `sheets.thread_names`.

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
.venv/bin/pip install -r requirements.txt
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

### Module Sheets báo lỗi

- `No module named gspread`: cài `.venv/bin/pip install -r requirements.txt`.
- `Chưa khai báo sheets.track_thread_ids`: điền ít nhất một `thread_id` (lấy từ
  bảng `threads`) vào `sheets.track_thread_ids`.
- `PermissionError` / `403` khi mở sheet: chưa **Share** spreadsheet cho
  `client_email` của service account (quyền Editor).
- `APIError 429`: vượt hạn ngạch Google Sheets — tăng `sheets.poll_seconds` hoặc
  giảm số thread theo dõi. Module tự retry vài lần trước khi bỏ qua vòng.
- Sheet mang tên `thread-<id>` mãi không đổi: tên nhóm chưa về trong traffic —
  mở nhóm đó trong Zalo, hoặc khai báo `sheets.thread_names`.
- Muốn đẩy lại từ đầu: dừng module, xoá `data/sheets_sync.state.json` (và đặt
  `sheets.backfill: true` nếu muốn lấy cả lịch sử cũ) rồi chạy lại.

## Giới hạn hiện tại

- Framing WebSocket hiện hỗ trợ header 4 byte và các chế độ Base64, deflate,
  AES-GCM đang quan sát được; Zalo vẫn có thể thay đổi protocol bất kỳ lúc nào.
- Listener không tự lấy toàn bộ lịch sử; dữ liệu phụ thuộc vào những gì tab Zalo
  thực sự tải.
- Tên nhóm/tên người (`threads.name`) chỉ về khi payload Zalo có chứa tên (mở
  nhóm, đồng bộ danh sách hội thoại...). Trước khi có, sheet mang tên tạm
  `thread-<id>` và tự đổi tên sau; hoặc ép sẵn qua `sheets.thread_names`.
- Chiều tin nhắn được suy ra bằng cách so sánh `uidFrom` với `userId`; nếu Zalo
  không gửi đủ hai field thì giá trị sẽ là `unknown`.
- Phân loại là so khớp keyword (bỏ dấu), không phải NLP — chỉnh
  `classify.categories` cho đúng nghiệp vụ.

## Cấu trúc mã nguồn

| File | Vai trò |
| --- | --- |
| `main.py` | Kết nối CDP, nhận sự kiện, lọc và điều phối lưu trữ |
| `inject.js` | Hook `fetch`, `XMLHttpRequest`, `WebSocket` và DOM tùy chọn |
| `crypto.py` | Chuẩn hoá key và thử giải mã AES-CBC/AES-ECB |
| `extract.py` | Duyệt JSON: nhận dạng message + bắt tên nhóm/tên người (`extract_name_hints`) |
| `privacy.py` | Chuẩn hoá Unicode, giả danh UID và che PII |
| `store.py` | Tạo schema, ghi SQLite, ghi `threads.name`, cột `ts_local` |
| `classify.py` | Phân loại đa nhãn theo keyword, bỏ dấu khi so khớp |
| `sheets_sync.py` | Đọc DB read-only, phân loại và đẩy realtime lên Google Sheets |
