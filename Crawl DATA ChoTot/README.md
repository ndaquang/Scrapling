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
* `presets/generic_ecommerce.yaml`: mẫu cho shop, rao vặt, tin tức (ví dụ books.toscrape.com).

> Các endpoint và selector của ChoTot có thể thay đổi. Khi preset không còn chạy, dùng `discover` để tìm API
> mới, hoặc `similar` để sinh lại selector.

## Kiểm thử

```bash
python -m pytest tests -q -p no:cacheprovider
```

Bộ test dựng một web server giả lập (HTML và API giống ChoTot) nên chạy được khi không có Internet.

## Lưu ý

Hãy tuân thủ điều khoản sử dụng của từng website, giữ tốc độ crawl hợp lý (bật `autothrottle`) và không thu thập
dữ liệu cá nhân trái phép.
