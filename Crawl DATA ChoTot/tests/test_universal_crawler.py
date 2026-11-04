"""Kiểm thử Universal Crawler với một web server giả lập chạy tại máy (không cần Internet).

Chạy:  python -m pytest "Crawl DATA ChoTot/tests" -q -p no:cacheprovider
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import universal_crawler as uc  # noqa: E402

ADS = [
    {"ad_id": 100 + i, "list_id": 9000 + i, "subject": f"Căn hộ số {i}", "price": 2_500_000_000 + i,
     "price_string": "2,5 tỷ", "size": 70 + i, "rooms": 2, "region_name": "Tp Hồ Chí Minh",
     "area_name": "Quận 7", "category_name": "Căn hộ/Chung cư", "list_time": 1730000000000}
    for i in range(7)
]


def _shop_page(page: int) -> str:
    cards = "".join(
        f'<article class="product_pod"><h3><a href="/catalogue/book-{page}{i}_{page}{i}/index.html">Sách {page}{i}</a></h3>'
        f'<p class="price_color">£{page}{i}.50</p></article>'
        for i in range(3)
    )
    nxt = f'<li class="next"><a href="/page-{page + 1}.html">next</a></li>' if page < 2 else ""
    return f"<html><head><title>Shop {page}</title></head><body><section>{cards}</section><ul>{nxt}</ul></body></html>"


def _book(slug: str) -> str:
    return f"""<html><head><title>{slug}</title>
    <script type="application/ld+json">{{"@type":"Product","name":"{slug}","offers":{{"price":"12.5","priceCurrency":"GBP"}}}}</script>
    </head><body><div class="product_main"><h1>{slug}</h1><p class="price_color">£1.234,50</p>
    <p class="availability">In stock (19 available)</p><p class="star-rating Three"></p></div>
    <table><tr><th>UPC</th><td>upc-{slug}</td></tr></table></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/v1/public/ad-listing":
            o, limit = int(q.get("o", ["0"])[0]), int(q.get("limit", ["3"])[0])
            return self._send(json.dumps({"total": len(ADS), "ads": ADS[o:o + limit]}), "application/json")
        if u.path.startswith("/v1/public/ad-listing/"):
            lid = int(u.path.rsplit("/", 1)[1])
            return self._send(json.dumps({
                "ad": {"list_id": lid, "body": f"Mô tả tin {lid}", "images": [f"https://img/{lid}/1.jpg", f"https://img/{lid}/2.jpg"]},
                "parameters": [{"id": "size", "label": "Diện tích", "value": "70 m²"}],
            }), "application/json")
        if u.path in ("/", "/page-1.html"):
            return self._send(_shop_page(1))
        if u.path == "/page-2.html":
            return self._send(_shop_page(2))
        if u.path.startswith("/catalogue/"):
            return self._send(_book(u.path.split("/")[2]))
        if u.path == "/blocked":
            return self._send("<html><body>Just a moment...<div id='cf-chl-widget'></div></body></html>")
        self._send("not found", status=404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


# ---------------- hàm tiện ích ----------------
@pytest.mark.parametrize("raw,expected", [
    ("1.500.000 đ", 1500000), ("12,5 m²", 12.5), ("1,234.56", 1234.56), ("£1.234,50", 1234.5),
    ("Giá: 850", 850), (None, None), ("abc", None), (42, 42),
])
def test_parse_number(raw, expected):
    assert uc.parse_number(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2,5 tỷ", 2_500_000_000), ("850 triệu", 850_000_000), ("1 tỷ 200 triệu", 1_200_000_000),
    ("500k", 500_000), ("3.200.000 đ", 3_200_000),
])
def test_parse_vnd(raw, expected):
    assert uc.parse_vnd(raw) == expected


def test_json_path():
    data = {"ads": [{"a": 1, "tags": ["x", "y"]}, {"a": 2, "tags": ["z"]}]}
    assert uc.json_path(data, "ads.1.a") == 2
    assert uc.json_path(data, "ads[].a") == [1, 2]
    assert uc.json_path(data, "ads[].tags[]") == ["x", "y", "z"]
    assert uc.json_path(data, "nope.x", "d") == "d"


# ---------------- crawl đầu-cuối ----------------
def _cfg_api(server, tmp_path, **extra):
    cfg = uc.load_config(Path(__file__).resolve().parents[1] / "presets" / "chotot_api.yaml")
    cfg["pagination"]["template"] = f"{server}/v1/public/ad-listing?limit=3&o={{offset}}"
    cfg["pagination"]["step"] = 3
    cfg["items"][0]["follow"]["url"] = f"{server}/v1/public/ad-listing/{{list_id}}"
    cfg["fetch"]["mode"] = "http"
    cfg["output"]["path"] = str(tmp_path / "chotot")
    cfg["output"]["formats"] = ["json", "csv", "xlsx"]
    cfg["crawl"]["log_level"] = "WARNING"
    cfg.update(extra)
    return cfg


def test_chotot_api_listing_and_detail(server, tmp_path):
    items = uc.run_config(_cfg_api(server, tmp_path))
    assert len(items) == len(ADS)  # 3 trang (3+3+1), trang 4 rỗng -> dừng
    first = sorted(items, key=lambda i: i["list_id"])[0]
    assert first["tieu_de"] == "Căn hộ số 0"
    assert first["gia"] == 2_500_000_000
    assert first["link"] == "https://www.chotot.com/9000.htm"
    assert first["mo_ta"] == "Mô tả tin 9000"  # ghép từ trang chi tiết
    assert len(first["tat_ca_anh"]) == 2
    assert first["ngay_dang"].startswith("2024-10-27")
    for ext in ("json", "csv", "xlsx"):
        assert (tmp_path / f"chotot.{ext}").exists()
    # incremental: lần chạy thứ 2 không có tin mới
    assert uc.run_config(_cfg_api(server, tmp_path)) == []


def test_max_items(server, tmp_path):
    cfg = _cfg_api(server, tmp_path)
    cfg["output"]["incremental"] = False
    cfg["crawl"]["max_items"] = 4
    assert len(uc.run_config(cfg)) == 4


def test_generic_html_shop(server, tmp_path):
    cfg = uc.load_config(Path(__file__).resolve().parents[1] / "presets" / "generic_ecommerce.yaml")
    cfg["start_urls"] = [server + "/"]
    cfg["crawl"].update({"robots_txt": False, "log_level": "WARNING"})
    cfg["fetch"]["mode"] = "http"
    cfg["output"].update({"path": str(tmp_path / "books"), "formats": ["json"]})
    items = uc.run_config(cfg)
    assert len(items) == 6  # 2 trang x 3 sách, đi qua phân trang "next"
    book = next(i for i in items if i["ten"] == "book-11_11")
    assert book["gia"] == 1234.5 and book["ton_kho"] == 19 and book["danh_gia"] == "Three"
    assert book["upc"] == "upc-book-11_11"


def test_list_mode_and_auto_extract(server, tmp_path):
    cfg = {
        "name": "t", "start_urls": [server + "/"], "fetch": {"mode": "http"},
        "crawl": {"log_level": "WARNING"},
        "items": [{"list": "article.product_pod", "fields": {
            "ten": "h3 a::text", "url": {"css": "h3 a::attr(href)", "absolute": True},
            "gia": {"css": ".price_color::text", "type": "number"}}}],
        "output": {"path": str(tmp_path / "list"), "formats": ["csv"]},
    }
    items = uc.run_config(cfg)
    assert [i["ten"] for i in items] == ["Sách 10", "Sách 11", "Sách 12"]
    assert items[0]["url"].startswith(server) and items[0]["gia"] == 10.5

    cfg2 = {"name": "a", "start_urls": [server + "/catalogue/x_1/index.html"], "fetch": {"mode": "http"},
            "crawl": {"log_level": "WARNING"}, "output": {"path": str(tmp_path / "auto"), "formats": ["json"]}}
    auto = uc.run_config(cfg2)[0]
    assert auto["name"] == "x_1" and auto["price"] == 12.5 and auto["currency"] == "GBP"


def test_block_detection(server, tmp_path):
    cfg = {"name": "b", "start_urls": [server + "/blocked"], "fetch": {"mode": "http"},
           "crawl": {"log_level": "CRITICAL", "max_blocked_retries": 1},
           "output": {"path": str(tmp_path / "blk"), "formats": ["json"]}}
    assert uc.run_config(cfg) == []  # trang challenge bị nhận diện là bị chặn, không xuất dữ liệu rác
