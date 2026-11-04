# Universal Crawler – crawl ChoTot và mọi trang web bằng Scrapling

`universal_crawler.py` là một crawler dùng chung cho mọi trang web. Bạn chỉ cần viết **một file cấu hình YAML**
cho mỗi trang (danh sách link cần đi theo, selector của từng trường, cách phân trang, định dạng xuất), không phải
viết code mới. Có sẵn preset cho **ChoTot / NhaTot**.

## Cài đặt

```bash
pip install "scrapling[fetchers]" pyyaml openpyxl markdownify
scrapling install          # tải trình duyệt cho chế độ stealth
```

## Cách dùng nhanh

```bash
# 1) ChoTot qua API JSON (nhanh nhất): BĐS TP.HCM, kèm chi tiết từng tin -> JSON + CSV + Excel
python universal_crawler.py run presets/chotot_api.yaml --max-items 200

# 2) Trang bất kỳ, không cần file cấu hình
python universal_crawler.py quick https://example.com/ \
    --follow "/san-pham/" --match "/san-pham/" \
    --field ten="h1::text" --field gia=".price::text" \
    --next "a.next::attr(href)"

# 3) Không biết selector? Đưa 1 đoạn văn bản mẫu -> tự sinh selector danh sách
python universal_crawler.py similar https://example.com/danh-muc "Tên một sản phẩm đang thấy"

# 4) Tìm API JSON ẩn phía sau trang (trang dùng React/Vue…)
python universal_crawler.py discover https://www.chotot.com/mua-ban-bat-dong-san --filter gateway

# 5) Crawl cả website thành Markdown (cho ChatGPT/Claude/RAG)
python universal_crawler.py markdown https://docs.example.com/ --max-pages 100

# 6) Trang cần đăng nhập (Facebook…): tự đăng nhập một lần, phiên được lưu vào thư mục profile
python universal_crawler.py login https://www.facebook.com/ --profile profiles/facebook
python universal_crawler.py run presets/facebook_marketplace.yaml --max-items 200
```

Tuỳ chọn thêm cho lệnh `run`:

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--mode http\|stealth\|auto` | `http` nhanh; `stealth` dùng trình duyệt ẩn danh (render JS, vượt Cloudflare); `auto` chạy HTTP trước, **tự chuyển sang trình duyệt khi bị chặn** |
| `--proxy URL` (lặp lại nhiều lần) | Xoay vòng proxy; request bị chặn được thử lại với proxy khác |
| `--resume ./checkpoint` | Ctrl+C để tạm dừng, chạy lại cùng lệnh để **tiếp tục** từ chỗ cũ |
| `--dev` | Lưu cache response ra đĩa, chỉnh selector rồi chạy lại mà không tải lại trang |
| `--max-items`, `--max-pages`, `--out` | Giới hạn số item/trang, đổi nơi lưu file |

## Các chức năng của Scrapling đã đưa vào

| Chức năng | Scrapling cung cấp | Cách bật trong YAML |
|---|---|---|
| HTTP giả lập trình duyệt (TLS fingerprint) | `FetcherSession(impersonate=...)` | `fetch.impersonate: chrome` |
| Trình duyệt ẩn danh, render JavaScript, vượt Cloudflare | `AsyncStealthySession(solve_cloudflare=True)` | `fetch.mode: stealth` |
| Tự chuyển HTTP → trình duyệt khi bị chặn | `is_blocked` + `retry_blocked_request` | `fetch.mode: auto` |
| Nhận diện trang bị chặn (403/429, Cloudflare, DataDome…) | `is_blocked` | `fetch.block_markers: [...]` |
| Xoay vòng proxy | `ProxyRotator` | `fetch.proxies: [...]` |
| Tự điều chỉnh tốc độ | AutoThrottle | `crawl.autothrottle: true` |
| Tuân thủ robots.txt | `robots_txt_obey` | `crawl.robots_txt: true` |
| Tạm dừng / tiếp tục | `crawldir` checkpoint | `--resume DIR` |
| Cache để phát triển | `development_mode` | `crawl.dev_cache: true` |
| Lọc link theo regex, domain, vùng CSS | `LinkExtractor` | `follow: [{allow, deny, restrict_css}]` |
| Selector tự thích nghi khi web đổi giao diện | `css(..., adaptive=True, auto_save=True)` | `fetch.adaptive: true` + `adaptive: true` ở từng trường |
| Tự tìm danh sách từ 1 mẫu | `find_by_text` + `find_similar` + `generate_css_selector` | lệnh `similar` |
| Bắt API JSON ẩn | `record_requests=True` + `session.network.search()` | lệnh `discover` |
| Cuộn vô hạn / bấm "Xem thêm" | `page_action` (async) | `fetch.scroll: {times, pause_ms, click_css}` |
| Giữ phiên đăng nhập | `user_data_dir` | `fetch.user_data_dir` + lệnh `login` |
| Lấy dữ liệu từ API mà trang tự gọi | `record_requests` + `network.search()` | `source: network` + `network_pattern` |
| Markdown sạch cho AI | `response.markdown()` | `markdown: true` hoặc lệnh `markdown` |
| Xuất dữ liệu | `to_json/to_csv/to_xml` | `output.formats: [json, jsonl, csv, xlsx, xml]` |

Phần bổ sung riêng của bản này:

* **Đọc giá kiểu Việt Nam**: `type: vnd` hiểu `"2,5 tỷ"`, `"850 triệu"`, `"1 tỷ 200 triệu"`, `"500k"`;
  `type: number` hiểu `"1.500.000 đ"`, `"12,5 m²"`.
* **Đi từ trang danh sách vào trang chi tiết rồi ghép dữ liệu** (`follow` + `merge`), dùng được cho cả API JSON.
* **Chế độ incremental**: chỉ lấy tin mới so với lần chạy trước (`output.incremental: true` + `dedupe_key`),
  phù hợp để chạy theo lịch hằng ngày.
* **Ghi file trực tiếp** `*.live.jsonl` trong lúc crawl, nên dừng giữa chừng vẫn còn dữ liệu.
* **Tự trích xuất khi không khai báo trường**: lấy JSON-LD, OpenGraph, tiêu đề, mô tả, giá, ảnh.
* **Dữ liệu JSON nhúng trong HTML** (`source: embedded`): đọc `<script type="application/json">`
  (Next.js `__NEXT_DATA__`, Facebook…), không cần selector CSS.
* **`find_key`**: tìm đệ quy mọi object chứa một khoá, ở bất kỳ độ sâu nào. Dùng cho GraphQL lồng sâu
  và hay đổi cấu trúc. Đọc được cả body dạng `for (;;);{...}` và nhiều JSON nối theo dòng.
* CSV ghi bằng `utf-8-sig` để Excel hiển thị đúng tiếng Việt; có xuất thẳng `.xlsx`.

## Cấu trúc file cấu hình

```yaml
name: ten_du_an
start_urls: [https://site.com/danh-muc]
allowed_domains: [site.com]          # bỏ trống = lấy theo start_urls

fetch:
  mode: auto                         # http | stealth | auto
  proxies: []
  adaptive: false
  headers: {}
  stealth: {network_idle: true, solve_cloudflare: true, disable_resources: true}

crawl:
  concurrency: 8
  autothrottle: true
  robots_txt: false
  max_pages: 0                       # 0 = không giới hạn
  max_items: 0
  max_depth: 0
  dev_cache: false

pagination:                          # chọn một trong hai cách
  next_css: "a.next::attr(href)"     # (a) theo nút "Trang sau"
  # template: "https://api.site.com/list?page={page}&offset={offset}"   # (b) theo mẫu URL
  # start: 0
  # step: 20                         # offset = page * step
  # max: 50
  # stop_when_empty: true

follow:                              # link cần đi theo (regex)
  - allow: "/san-pham/\\d+"
    deny: "/dang-nhap"
    restrict_css: ".product-list"

items:                               # quy tắc trích xuất, theo thứ tự
  - name: chi_tiet
    match: "/san-pham/\\d+"          # regex URL áp dụng
    type: auto                       # html | json | auto
    list: null                       # selector/đường dẫn JSON nếu 1 trang có nhiều item
    required: [ten]                  # bỏ item thiếu trường này
    fields:
      ten: "h1::text"                                   # CSS rút gọn
      gia: {css: ".price::text", type: vnd}
      sku: {xpath: "//span[@id='sku']/text()"}
      anh: {css: "img::attr(src)", all: true, absolute: true}
      ton_kho: {css: ".stock", regex: "(\\d+)", type: int}
      link_goc: {json: "data.id", format: "https://site.com/p/{value}"}
    follow:                          # (tuỳ chọn) đi vào URL khác cho mỗi item, ghép dữ liệu
      url: "https://api.site.com/item/{sku}"
      rule: chi_tiet_api
    emit: true                       # false = chỉ xuất sau khi đã ghép chi tiết

output:
  path: output/ten_du_an
  formats: [json, csv, xlsx]
  dedupe_key: sku
  incremental: false
```

Các khoá của một trường: `css`, `xpath`, `json` (đường dẫn chấm, ví dụ `ads.0.subject` hay `params[].value`),
`regex`, `type` (`str`, `int`, `float`, `number`, `vnd`, `bool`, `timestamp`), `all`, `join`, `absolute`, `default`,
`format`, `html`, `adaptive`, `meta`, `const`.

## Presets có sẵn

* `presets/chotot_api.yaml`: ChoTot/NhaTot qua `gateway.chotot.com` (danh sách và chi tiết, incremental).
  Đổi `cg` (danh mục) và `region_v2` (tỉnh/thành) trong `template`.
* `presets/chotot_html.yaml`: ChoTot qua giao diện web bằng trình duyệt ẩn danh, có adaptive selector.
  Dùng làm dự phòng khi API thay đổi.
* `presets/facebook_marketplace.yaml`: Facebook Marketplace (xem mục bên dưới).
* `presets/generic_ecommerce.yaml`: mẫu cho shop, rao vặt, tin tức (ví dụ books.toscrape.com).

> Các endpoint và selector của ChoTot có thể thay đổi. Khi preset không còn chạy, dùng `discover` để tìm API
> mới, hoặc `similar` để sinh lại selector.

## Facebook Marketplace

Marketplace khác ChoTot ở bốn điểm, và preset xử lý từng điểm như sau:

| Vấn đề | Cách xử lý |
|---|---|
| Phần lớn nội dung chỉ hiện khi đã đăng nhập | Lệnh `login` mở trình duyệt để bạn tự đăng nhập. Phiên được lưu ở `profiles/facebook` và dùng lại cho các lần chạy sau |
| Trang render bằng JavaScript, class CSS bị mã hoá và đổi liên tục | Không dùng selector CSS. Lấy dữ liệu từ JSON nhúng trong HTML và từ response `/api/graphql/` |
| Tin mới chỉ tải khi cuộn trang | `fetch.scroll` cuộn trang nhiều lần, dừng khi không còn tải thêm |
| Cấu trúc GraphQL lồng sâu, hay đổi | `find_key: marketplace_listing_title` tìm mọi object tin đăng ở bất kỳ độ sâu nào |

Rủi ro cần biết:

* **Điều khoản của Meta cấm thu thập tự động.** Tài khoản dùng để crawl có thể bị checkpoint, giới hạn hoặc khoá.
  Hãy dùng tài khoản phụ.
* Preset chạy rất chậm: 1 tab, nghỉ 8 giây giữa các trang, tối đa 5 trang và 300 tin mỗi lần chạy.
  Tăng các con số này thì rủi ro bị khoá tăng theo.
* Preset **không** lấy tên hay hồ sơ người bán. Thu thập dữ liệu cá nhân phải tuân thủ Nghị định 13/2023/NĐ-CP.
* Tên trường GraphQL (`marketplace_listing_title`, `listing_price`, …) do Facebook tự đặt và có thể đổi bất kỳ lúc
  nào. Khi không ra dữ liệu, chạy
  `python universal_crawler.py discover "<URL marketplace>" --filter graphql --show` để xem response thật,
  rồi sửa `find_key` và `fields` trong preset.

## Kiểm thử

```bash
python -m pytest tests -q -p no:cacheprovider
```

Bộ test dựng một web server giả lập (HTML và API giống ChoTot) nên chạy được khi không có Internet.

## Lưu ý

Hãy tuân thủ điều khoản sử dụng của từng website, giữ tốc độ crawl hợp lý (bật `autothrottle`) và không thu thập
dữ liệu cá nhân trái phép.
