#!/usr/bin/env python3
"""
Universal Crawler — crawler đa năng dựa trên Scrapling, dùng được cho mọi trang web.

Tất cả hành vi được khai báo bằng một file cấu hình YAML/JSON (xem thư mục `presets/`),
không cần viết code mới cho từng trang.

Các chức năng của Scrapling được tận dụng:
  * FetcherSession (curl_cffi)      – HTTP nhanh, giả lập TLS fingerprint của Chrome/Firefox/Safari
  * AsyncStealthySession             – trình duyệt ẩn danh, render JavaScript, vượt Cloudflare
  * Chế độ "auto"                    – chạy HTTP trước, tự chuyển sang trình duyệt khi bị chặn
  * ProxyRotator                     – xoay vòng proxy, tự đổi proxy khi request bị chặn
  * AutoThrottle / robots.txt        – tự điều chỉnh tốc độ theo phản hồi của server
  * Pause/Resume (crawldir)          – Ctrl+C để dừng, chạy lại để tiếp tục
  * Development mode                 – cache response ra đĩa để chỉnh selector không phải tải lại
  * LinkExtractor                    – lọc link theo regex allow/deny, domain, vùng CSS
  * Adaptive selectors               – tự tìm lại phần tử khi website đổi giao diện
  * find_similar / find_by_text      – tự phát hiện danh sách sản phẩm từ 1 mẫu văn bản
  * record_requests (network)        – bắt các API JSON ẩn phía sau trang web
  * markdown()                       – xuất nội dung trang thành Markdown sạch (cho AI/RAG)
  * Xuất JSON / JSONL / CSV / XML (+ Excel nếu có openpyxl)

Lệnh:
  python universal_crawler.py run presets/chotot_api.yaml
  python universal_crawler.py quick URL --field title="h1::text" --follow "/san-pham/"
  python universal_crawler.py discover URL               # tìm API JSON ẩn
  python universal_crawler.py similar URL "mẫu văn bản"   # tự sinh selector danh sách
  python universal_crawler.py markdown URL --max-pages 50
  python universal_crawler.py login URL --profile ./profiles/fb   # đăng nhập thủ công, lưu phiên
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Union
from urllib.parse import urlparse

from scrapling.fetchers import AsyncStealthySession, FetcherSession, ProxyRotator
from scrapling.spiders import LinkExtractor, Request, Response, SessionManager, Spider

log = logging.getLogger("universal_crawler")

BLOCK_STATUS = {401, 403, 407, 429, 444, 500, 502, 503, 504, 520, 521, 522, 524}
DEFAULT_BLOCK_MARKERS = [
    "cf-chl-",  # Cloudflare challenge
    "challenge-platform",
    "Just a moment...",
    "Attention Required! | Cloudflare",
    "captcha-delivery.com",  # DataDome
    "px-captcha",  # PerimeterX
    "Access Denied",
]


# --------------------------------------------------------------------------------------
# Đọc cấu hình
# --------------------------------------------------------------------------------------
def load_config(path: Union[str, Path]) -> Dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as e:  # pragma: no cover
            raise SystemExit("Cần cài PyYAML để đọc file .yaml:  pip install pyyaml") from e
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# --------------------------------------------------------------------------------------
# Chuyển đổi kiểu dữ liệu (có hỗ trợ định dạng tiền Việt Nam)
# --------------------------------------------------------------------------------------
_VND_UNITS = [
    (re.compile(r"t[ỷyỉ]", re.I), 1_000_000_000),
    (re.compile(r"(?:tri[ệe]u|tr)\b", re.I), 1_000_000),
    (re.compile(r"(?:ngh[ìi]n|k)\b", re.I), 1_000),
]


def parse_number(value: Any) -> Optional[float]:
    """'1.500.000 đ' -> 1500000 ; '12,5 m²' -> 12.5 ; '1,234.56' -> 1234.56"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"-?\d[\d.,\s]*", str(value))
    if not m:
        return None
    raw = re.sub(r"\s", "", m.group()).rstrip(".,")
    if "," in raw and "." in raw:
        # dấu xuất hiện sau cùng là dấu thập phân
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(".") > 1 or re.fullmatch(r"-?\d{1,3}(\.\d{3})+", raw):
        raw = raw.replace(".", "")
    elif raw.count(",") > 1 or re.fullmatch(r"-?\d{1,3}(,\d{3})+", raw):
        raw = raw.replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        num = float(raw)
    except ValueError:
        return None
    return int(num) if num.is_integer() else num


def parse_vnd(value: Any) -> Optional[float]:
    """'2,5 tỷ' -> 2500000000 ; '850 triệu' -> 850000000 ; '1 tỷ 200 triệu' -> 1200000000"""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    total, found = 0.0, False
    for pattern, mult in _VND_UNITS:
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(" + pattern.pattern + r")", text, re.I):
            total += float(m.group(1).replace(",", ".")) * mult
            found = True
    if found:
        return int(total) if float(total).is_integer() else total
    return parse_number(text)


def parse_timestamp(value: Any) -> Optional[str]:
    """Unix timestamp (giây hoặc mili-giây) -> ISO 8601."""
    num = parse_number(value)
    if num is None:
        return str(value) if value else None
    if num > 1e12:
        num /= 1000
    return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()


CONVERTERS = {
    "str": lambda v: None if v is None else str(v).strip(),
    "int": lambda v: None if (n := parse_number(v)) is None else int(n),
    "float": lambda v: None if (n := parse_number(v)) is None else float(n),
    "number": parse_number,
    "vnd": parse_vnd,
    "bool": lambda v: None if v is None else str(v).strip().lower() in {"1", "true", "yes", "có", "co"},
    "timestamp": parse_timestamp,
}


# --------------------------------------------------------------------------------------
# Trích xuất dữ liệu
# --------------------------------------------------------------------------------------
def json_path(data: Any, path: str, default: Any = None) -> Any:
    """Lấy giá trị theo đường dẫn chấm: 'ads.0.subject', 'params[].value', '*' (toàn bộ)."""
    if path in ("", "*", "$"):
        return data
    current = [data]
    is_multi = False
    for part in path.split("."):
        nxt: List[Any] = []
        many = part.endswith("[]")
        key = part[:-2] if many else part
        for node in current:
            if key:
                if isinstance(node, dict):
                    node = node.get(key)
                elif isinstance(node, list) and key.lstrip("-").isdigit():
                    idx = int(key)
                    node = node[idx] if -len(node) <= idx < len(node) else None
                else:
                    node = None
            if node is None:
                continue
            if many and isinstance(node, list):
                nxt.extend(node)
                is_multi = True
            else:
                nxt.append(node)
        current = nxt
    if is_multi:
        return current
    return current[0] if current else default


def deep_find(data: Any, key: str) -> List[Dict[str, Any]]:
    """Tìm đệ quy mọi object (dict) có chứa `key`, ở bất kỳ độ sâu nào.

    Hữu ích với API GraphQL (Facebook, Instagram…) có cấu trúc lồng sâu và hay thay đổi.
    """
    found: List[Dict[str, Any]] = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                found.append(node)
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return found


def parse_json_documents(body: Union[bytes, str]) -> List[Any]:
    """Đọc body JSON, kể cả dạng nhiều object nối nhau theo dòng và tiền tố chống JSON-hijacking ('for (;;);')."""
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    text = re.sub(r"^\s*(for\s*\(;;\);|while\s*\(1\);|\)\]\}',?)", "", text)
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue
    return docs


def embedded_json(response: Response) -> List[Any]:
    """Lấy dữ liệu JSON nhúng sẵn trong HTML (Next.js __NEXT_DATA__, Facebook, JSON-LD…)."""
    docs = []
    for raw in response.css("script[type*='json']::text").getall():
        docs.extend(parse_json_documents(str(raw)))
    return docs


def _normalize_spec(spec: Any) -> Dict[str, Any]:
    """Một trường có thể là chuỗi 'h1::text' hoặc dict chi tiết."""
    if isinstance(spec, str):
        if spec.startswith(("/", "(")):
            return {"xpath": spec}
        if spec.startswith("json:"):
            return {"json": spec[5:]}
        return {"css": spec}
    return dict(spec or {})


class FieldExtractor:
    """Trích xuất 1 trường theo spec:

    css / xpath / json / regex / meta / const / all / join / type / default / absolute / adaptive / format / html
    """

    def __init__(self, name: str, spec: Any):
        self.name = name
        self.spec = _normalize_spec(spec)
        self.regex = re.compile(self.spec["regex"], re.S) if self.spec.get("regex") else None
        type_name = self.spec.get("type", "str")
        if type_name not in CONVERTERS:
            raise ValueError(f"Trường '{name}': kiểu '{type_name}' không hỗ trợ. Dùng: {', '.join(CONVERTERS)}")
        self.convert = CONVERTERS[type_name]

    def _raw_values(self, node: Any, data: Any, response: Response) -> List[Any]:
        s = self.spec
        if "const" in s:
            return [s["const"]]
        if "meta" in s:
            return _as_list(json_path(response.meta, s["meta"]))
        if "json" in s:
            source = data if data is not None else _response_json(response)
            return _as_list(json_path(source, s["json"]))
        if node is None:
            return []
        if "css" in s:
            kwargs = {}
            if s.get("adaptive"):
                kwargs = {"adaptive": True, "auto_save": True, "identifier": f"{self.name}:{s['css']}"}
            found = node.css(s["css"], **kwargs)
        elif "xpath" in s:
            found = node.xpath(s["xpath"])
        else:
            return []
        values: List[Any] = []
        for el in found:
            if isinstance(el, str):  # TextHandler (kết quả của ::text, ::attr)
                values.append(str(el))
            elif s.get("html"):
                values.append(el.html_content)
            else:
                values.append(el.get_all_text(separator=" ", strip=True))
        return values

    def extract(self, node: Any, data: Any, response: Response) -> Any:
        values = self._raw_values(node, data, response)
        if self.regex is not None:
            matched = []
            for v in values:
                m = self.regex.search(str(v))
                if m:
                    matched.append(m.group(1) if m.groups() else m.group(0))
            values = matched
        if isinstance(values, list):
            values = [v.strip() if isinstance(v, str) else v for v in values]
            values = [v for v in values if v not in ("", None)]
        if self.spec.get("absolute"):
            values = [response.urljoin(str(v)) for v in values]
        values = [self.convert(v) if not isinstance(v, (dict, list)) else v for v in values]
        if self.spec.get("format"):
            values = [str(self.spec["format"]).format(value=v) for v in values]
        if self.spec.get("all"):
            if self.spec.get("join") is not None:
                return str(self.spec["join"]).join(str(v) for v in values)
            return values
        return values[0] if values else self.spec.get("default")


def _response_json(response: Response) -> Any:
    # Cache trên chính đối tượng response (không để trong meta vì meta được sao chép sang request con)
    if not hasattr(response, "_uc_json"):
        try:
            response._uc_json = json.loads(response.body)
        except (ValueError, TypeError):
            response._uc_json = None
    return response._uc_json


def auto_extract(response: Response) -> Dict[str, Any]:
    """Trích xuất tự động (không cần selector): JSON-LD, OpenGraph, meta, tiêu đề, giá, ảnh."""
    item: Dict[str, Any] = {
        "title": (
            response.css("meta[property='og:title']::attr(content)").get() or response.css("title::text").get() or ""
        ).strip(),
        "h1": " ".join(h.get_all_text(strip=True) for h in response.css("h1")).strip(),
        "description": (
            response.css("meta[property='og:description']::attr(content)").get()
            or response.css("meta[name='description']::attr(content)").get()
            or ""
        ).strip(),
        "image": response.css("meta[property='og:image']::attr(content)").get(),
        "canonical": response.css("link[rel='canonical']::attr(href)").get(),
    }
    structured = []
    for raw in response.css("script[type='application/ld+json']::text").getall():
        try:
            structured.append(json.loads(str(raw)))
        except ValueError:
            continue
    if structured:
        item["json_ld"] = structured
        flat = []
        for block in structured:
            flat.extend(block.get("@graph", [block]) if isinstance(block, dict) else block)
        for block in flat:
            if not isinstance(block, dict):
                continue
            offers = block.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict) and "price" in offers:
                item.setdefault("price", parse_number(offers.get("price")))
                item.setdefault("currency", offers.get("priceCurrency"))
            if block.get("name") and not item.get("name"):
                item["name"] = block["name"]
    if "price" not in item:
        price = response.css("meta[property='product:price:amount']::attr(content)").get()
        if price:
            item["price"] = parse_number(price)
    return {k: v for k, v in item.items() if v not in (None, "", [])}


class ItemRule:
    """Một quy tắc trích xuất: áp dụng cho các URL khớp `match`, có thể trả nhiều item/trang (`list`)."""

    def __init__(self, cfg: Dict[str, Any], index: int):
        self.name = cfg.get("name", f"rule{index}")
        self.match = re.compile(cfg["match"]) if cfg.get("match") else None
        self.kind = cfg.get("type", "auto")  # html | json | auto
        self.list = cfg.get("list")
        self.fields = [FieldExtractor(k, v) for k, v in (cfg.get("fields") or {}).items()]
        self.auto = cfg.get("auto_extract", not self.fields)
        self.markdown = cfg.get("markdown", False)
        self.required = _as_list(cfg.get("required"))
        self.follow = cfg.get("follow")  # {"url": "https://.../{id}", "rule": "detail", "merge": true}
        self.yield_items = cfg.get("emit", True)
        self.only_followed = cfg.get("only_followed", False)
        # Nguồn dữ liệu: page (nội dung response), embedded (JSON nhúng trong <script>),
        # network (các response XHR/fetch mà trình duyệt đã gọi, khớp `network_pattern`)
        self.sources = _as_list(cfg.get("source", "page"))
        self.network_pattern = cfg.get("network_pattern")
        self.find_key = cfg.get("find_key")  # tìm đệ quy các object có khoá này

    def applies(self, response: Response) -> bool:
        target = response.meta.get("_rule")
        if target:  # request được tạo bởi `follow` của rule khác, chỉ định rõ rule đích
            return target == self.name
        if self.only_followed:
            return False
        return self.match is None or bool(self.match.search(response.url))

    def is_json(self, response: Response) -> bool:
        if self.kind != "auto":
            return self.kind == "json"
        ctype = str(response.headers.get("content-type", "") or response.headers.get("Content-Type", ""))
        if "json" in ctype:
            return True
        return response.body[:1].strip() in (b"{", b"[")

    def _json_nodes(self, docs: List[Any]) -> List[Any]:
        nodes: List[Any] = []
        for doc in docs:
            if self.find_key:
                nodes.extend(deep_find(doc, self.find_key))
            elif self.list:
                nodes.extend(_as_list(json_path(doc, self.list)))
            else:
                nodes.append(doc)
        return nodes

    def extract(self, response: Response, network_bodies: Optional[List[bytes]] = None) -> List[Dict[str, Any]]:
        parent = response.meta.get("_parent") or {}
        docs: List[Any] = []
        if "embedded" in self.sources:
            docs.extend(embedded_json(response))
        if "network" in self.sources:
            for body in network_bodies or []:
                docs.extend(parse_json_documents(body))
        as_json = bool(docs) or (self.sources != ["page"]) or self.is_json(response)
        if "page" in self.sources and self.is_json(response):
            docs.append(_response_json(response))
        if as_json:
            pairs = [(None, d) for d in self._json_nodes(docs)]
        else:
            nodes = list(response.css(self.list)) if self.list else [response]
            pairs = [(n, None) for n in nodes]

        items = []
        for node, data in pairs:
            item: Dict[str, Any] = dict(parent) if parent else {}
            if self.auto and not as_json:
                item.update(auto_extract(response) if node is response else {})
            for field in self.fields:
                item[field.name] = field.extract(node, data, response)
            if as_json and not self.fields and isinstance(data, dict):
                item.update(data)
            if self.markdown and not as_json:
                item["markdown"] = response.markdown(main_content_only=True)
            if any(item.get(r) in (None, "", []) for r in self.required):
                continue
            item["_url"] = response.url
            item["_rule"] = self.name
            items.append(item)
        return items


# --------------------------------------------------------------------------------------
# Spider đa năng
# --------------------------------------------------------------------------------------
class UniversalSpider(Spider):
    name = "universal"

    def __init__(self, config: Dict[str, Any], crawldir: Optional[str] = None):
        self.cfg = config
        self.name = config.get("name", "universal")
        crawl = config.get("crawl", {})
        fetch = config.get("fetch", {})
        self.start_urls = _as_list(config.get("start_urls"))
        seeds = self.start_urls + _as_list((config.get("pagination") or {}).get("template"))
        self.allowed_domains = set(_as_list(config.get("allowed_domains"))) or {
            h
            for h in (urlparse(u).netloc for u in seeds)
            if h  # netloc: engine so sánh cả cổng
        }

        # --- các cờ có sẵn của Scrapling Spider ---
        self.concurrent_requests = int(crawl.get("concurrency", 8))
        self.concurrent_requests_per_domain = int(crawl.get("per_domain", 0))
        self.download_delay = float(crawl.get("delay", 0))
        self.autothrottle_enabled = bool(crawl.get("autothrottle", False))
        self.robots_txt_obey = bool(crawl.get("robots_txt", False))
        self.development_mode = bool(crawl.get("dev_cache", False))
        self.max_blocked_retries = int(crawl.get("max_blocked_retries", 3))
        self.logging_level = getattr(logging, str(crawl.get("log_level", "INFO")).upper(), logging.INFO)
        self.log_file = crawl.get("log_file")

        self.mode = fetch.get("mode", "http")  # http | stealth | auto
        self.block_markers = _as_list(fetch.get("block_markers", DEFAULT_BLOCK_MARKERS))
        self.max_pages = int(crawl.get("max_pages", 0))
        self.max_items = int(crawl.get("max_items", 0))
        self.max_depth = int(crawl.get("max_depth", 0))

        # Không khai báo `items` -> tự trích xuất mọi trang (JSON-LD, OpenGraph, giá…)
        self._net_after = 0  # id của response mạng cuối cùng đã xử lý
        self.rules = [ItemRule(r, i) for i, r in enumerate(_as_list(config.get("items")) or [{"auto_extract": True}])]
        self._uses_network = any("network" in r.sources for r in self.rules)
        if self._uses_network and self.mode != "stealth":
            raise ValueError(
                "Rule có `source: network` cần `fetch.mode: stealth` (chỉ trình duyệt mới ghi được network)"
            )
        self.follow_rules = [self._make_link_extractor(f) for f in _as_list(config.get("follow"))]
        self.pagination = config.get("pagination") or {}
        self._page_re = re.compile(self.pagination["match"]) if self.pagination.get("match") else None

        out = config.get("output", {})
        self.out_path = Path(out.get("path", f"output/{self.name}"))
        self.formats = _as_list(out.get("formats", ["json", "csv"]))
        self.dedupe_key = out.get("dedupe_key")
        self.incremental = bool(out.get("incremental", False))
        self._seen_keys: set = set()
        self._state_file = self.out_path.with_suffix(".seen.json")
        if self.incremental and self._state_file.exists():
            self._seen_keys = set(json.loads(self._state_file.read_text(encoding="utf-8")))
        self._pages = 0
        self._items = 0
        self._new_keys: set = set()
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._live = (self.out_path.with_suffix(".live.jsonl")).open("a", encoding="utf-8")

        super().__init__(crawldir=crawldir, interval=float(crawl.get("checkpoint_interval", 120)))

    # ---------------- sessions ----------------
    def configure_sessions(self, manager: SessionManager) -> None:
        fetch = self.cfg.get("fetch", {})
        proxies = _as_list(fetch.get("proxies"))
        rotator = ProxyRotator(proxies) if len(proxies) > 1 else None
        single_proxy = proxies[0] if len(proxies) == 1 else None

        http_kwargs: Dict[str, Any] = {
            "impersonate": fetch.get("impersonate", ["chrome", "firefox", "safari"]),
            "stealthy_headers": True,
            "timeout": fetch.get("timeout", 30),
            "retries": fetch.get("retries", 3),
            "headers": fetch.get("headers") or {},
        }
        stealth_kwargs: Dict[str, Any] = {
            "headless": True,
            "block_ads": True,
            "disable_resources": False,
            "network_idle": True,
            "solve_cloudflare": True,
            "block_webrtc": True,
            "hide_canvas": True,
            "locale": fetch.get("locale", "vi-VN"),
            "max_pages": int(fetch.get("browser_tabs", 2)),
        }
        if fetch.get("user_data_dir"):  # hồ sơ trình duyệt riêng: giữ cookie đăng nhập giữa các lần chạy
            Path(fetch["user_data_dir"]).mkdir(parents=True, exist_ok=True)
            stealth_kwargs["user_data_dir"] = str(Path(fetch["user_data_dir"]).resolve())
        if fetch.get("headless") is not None:
            stealth_kwargs["headless"] = bool(fetch["headless"])
        if fetch.get("scroll"):
            stealth_kwargs["page_action"] = make_scroller(fetch["scroll"])
        if self._uses_network:
            stealth_kwargs["record_requests"] = True
            stealth_kwargs["max_recorded_requests"] = int(fetch.get("max_recorded_requests", 2000))
        stealth_kwargs.update(fetch.get("stealth") or {})
        if fetch.get("adaptive"):
            selector_cfg = {"adaptive": True}
            http_kwargs["selector_config"] = selector_cfg
            stealth_kwargs["selector_config"] = selector_cfg
        if rotator:
            http_kwargs["proxy_rotator"] = rotator
            stealth_kwargs["proxy_rotator"] = rotator
        elif single_proxy:
            http_kwargs["proxy"] = single_proxy
            stealth_kwargs["proxy"] = single_proxy

        if self.mode == "stealth":
            manager.add("stealth", AsyncStealthySession(**stealth_kwargs), default=True)
        else:
            manager.add("http", FetcherSession(**http_kwargs), default=True)
            if self.mode == "auto":
                manager.add("stealth", AsyncStealthySession(**stealth_kwargs), lazy=True)

    # ---------------- chặn & thử lại ----------------
    async def is_blocked(self, response: Response) -> bool:
        if response.status in BLOCK_STATUS:
            return True
        if self.block_markers and response.status == 200 and len(response.body) < 200_000:
            head = response.body[:20_000].decode("utf-8", "ignore")
            return any(m in head for m in self.block_markers)
        return False

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        if self.mode == "auto" and request.sid != "stealth":
            self.logger.warning(f"Bị chặn ({response.status}) → chuyển sang trình duyệt ẩn danh: {request.url}")
            request.sid = "stealth"
            for key in ("impersonate", "stealthy_headers", "method", "data", "json", "params"):
                request._session_kwargs.pop(key, None)
        return request

    # ---------------- crawl ----------------
    def _make_link_extractor(self, cfg: Union[str, Dict[str, Any]]) -> LinkExtractor:
        if isinstance(cfg, str):
            cfg = {"allow": cfg}
        return LinkExtractor(
            allow=_as_list(cfg.get("allow")),
            deny=_as_list(cfg.get("deny")),
            allow_domains=_as_list(cfg.get("allow_domains")) or [d.split(":")[0] for d in self.allowed_domains],
            restrict_css=_as_list(cfg.get("restrict_css")),
            restrict_xpath=_as_list(cfg.get("restrict_xpath")),
        )

    def _request_kwargs(self) -> Dict[str, Any]:
        """Tham số thêm cho mỗi request (vd: headers riêng, wait_selector cho trình duyệt)."""
        return dict(self.cfg.get("request") or {})

    async def start_requests(self) -> AsyncGenerator[Request, None]:
        template = self.pagination.get("template")
        if template:
            start = int(self.pagination.get("start", 0))
            yield Request(
                template.format(page=start, offset=start * int(self.pagination.get("step", 1)), n=start),
                meta={"_page_index": start, "_depth": 0},
                **self._request_kwargs(),
            )
        for url in self.start_urls:
            yield Request(url, meta={"_depth": 0}, **self._request_kwargs())

    def _network_bodies(self, rule: "ItemRule") -> List[bytes]:
        """Lấy body các response XHR/fetch mới (chưa xử lý) mà trình duyệt đã ghi lại, khớp `network_pattern`."""
        if "network" not in rule.sources:
            return []
        recorder = self._session_manager.get("stealth").network
        records = recorder.search(url_pattern=rule.network_pattern, after_id=self._net_after, limit=None)
        self._net_after = max([self._net_after] + [int(r.meta.get("network_id", 0)) for r in records])
        return [r.body for r in records if r.body]

    def _limit_reached(self) -> bool:
        return bool(self.max_items and self._items >= self.max_items)

    async def parse(self, response: Response) -> AsyncGenerator[Union[Dict[str, Any], Request, None], None]:
        if self.max_pages and self._pages >= self.max_pages:
            return  # request đã nằm trong hàng đợi khi chạm giới hạn: bỏ qua
        self._pages += 1
        depth = int(response.meta.get("_depth", 0))
        found_items = 0

        for rule in self.rules:
            if not rule.applies(response):
                continue
            for item in rule.extract(response, self._network_bodies(rule)):
                found_items += 1
                if rule.follow:
                    url_tpl = rule.follow["url"] if isinstance(rule.follow, dict) else rule.follow
                    try:
                        url = response.urljoin(url_tpl.format(**{k: v for k, v in item.items() if k}))
                    except (KeyError, IndexError, ValueError):
                        url = None
                    if url and not self._limit_reached():
                        target = rule.follow.get("rule") if isinstance(rule.follow, dict) else None
                        merge = rule.follow.get("merge", True) if isinstance(rule.follow, dict) else True
                        meta = {"_depth": depth + 1, "_rule": target}
                        if merge:
                            meta["_parent"] = {k: v for k, v in item.items() if not k.startswith("_")}
                        yield response.follow(url, meta=meta, **self._request_kwargs())
                if rule.yield_items:
                    yield item
            break_after = self.cfg.get("first_rule_only", True)
            if break_after:
                break

        if self._limit_reached() or (self.max_pages and self._pages >= self.max_pages):
            return

        # --- phân trang ---
        is_listing = self._page_re is None or bool(self._page_re.search(response.url))
        if self.pagination and is_listing and not response.meta.get("_rule"):
            next_css = self.pagination.get("next_css")
            if next_css:
                nxt = response.css(next_css).get()
                if nxt:
                    yield response.follow(str(nxt), meta={"_depth": depth}, **self._request_kwargs())
            template = self.pagination.get("template")
            if template and "_page_index" in response.meta:
                idx = int(response.meta["_page_index"]) + 1
                max_p = int(self.pagination.get("max", 0))
                stop_empty = self.pagination.get("stop_when_empty", True)
                if (not stop_empty or found_items) and (
                    not max_p or idx < int(self.pagination.get("start", 0)) + max_p
                ):
                    yield Request(
                        template.format(page=idx, offset=idx * int(self.pagination.get("step", 1)), n=idx),
                        meta={"_page_index": idx, "_depth": depth},
                        **self._request_kwargs(),
                    )

        # --- theo link ---
        if self.follow_rules and (not self.max_depth or depth < self.max_depth) and not response.meta.get("_rule"):
            if response.body[:1].strip() not in (b"{", b"["):  # chỉ lấy link từ trang HTML
                for extractor in self.follow_rules:
                    for url in extractor.extract(response):
                        yield response.follow(url, meta={"_depth": depth + 1}, **self._request_kwargs())

    async def on_scraped_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._limit_reached():
            return None
        if self.dedupe_key:
            key = str(item.get(self.dedupe_key))
            if key in self._seen_keys:
                return None
            self._seen_keys.add(key)
            self._new_keys.add(key)
        item["_scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._items += 1
        self._live.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self._live.flush()
        if self._limit_reached():
            self.logger.info(f"Đã đủ {self.max_items} item, dừng crawl.")
            try:
                self.pause()
            except RuntimeError:
                pass
        return item

    async def on_close(self) -> None:
        self._live.close()
        if self.incremental and self.dedupe_key:
            self._state_file.write_text(json.dumps(sorted(self._seen_keys), ensure_ascii=False), encoding="utf-8")


def make_scroller(cfg: Any):
    """Tạo `page_action` cuộn trang để tải thêm nội dung (infinite scroll).

    cfg: số lần cuộn, hoặc {times, pause_ms, distance, stop_when_static, click_css}
    """
    if not isinstance(cfg, dict):
        cfg = {"times": int(cfg)}
    times = int(cfg.get("times", 5))
    pause = int(cfg.get("pause_ms", 1500))
    distance = int(cfg.get("distance", 3000))
    stop_static = bool(cfg.get("stop_when_static", True))
    click_css = cfg.get("click_css")  # vd: nút "Xem thêm"

    async def scroll(page):
        last_height = -1
        static_rounds = 0
        for _ in range(times):
            if click_css:
                try:
                    btn = page.locator(click_css).first
                    if await btn.count():
                        await btn.click(timeout=2000)
                except Exception:
                    pass
            await page.mouse.wheel(0, distance)
            await page.wait_for_timeout(pause)
            height = await page.evaluate("document.body.scrollHeight")
            if stop_static:
                static_rounds = static_rounds + 1 if height == last_height else 0
                if static_rounds >= 2:  # 2 lần liên tiếp không tải thêm -> hết dữ liệu
                    break
            last_height = height
        return page

    return scroll


# --------------------------------------------------------------------------------------
# Xuất dữ liệu
# --------------------------------------------------------------------------------------
def _flatten(item: Dict[str, Any]) -> Dict[str, Any]:
    flat = {}
    for k, v in item.items():
        if isinstance(v, (dict, list)):
            if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
                flat[k] = " | ".join(str(x) for x in v)
            else:
                flat[k] = json.dumps(v, ensure_ascii=False, default=str)
        else:
            flat[k] = v
    return flat


def export_items(items: List[Dict[str, Any]], base: Path, formats: Iterable[str]) -> List[Path]:
    written = []
    base.parent.mkdir(parents=True, exist_ok=True)
    fields: List[str] = []
    for it in items:
        for k in it:
            if k not in fields:
                fields.append(k)
    for fmt in formats:
        fmt = fmt.lower()
        path = base.with_suffix(f".{fmt}")
        if fmt == "json":
            path.write_text(json.dumps(items, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        elif fmt == "jsonl":
            path.write_text(
                "".join(json.dumps(i, ensure_ascii=False, default=str) + "\n" for i in items), encoding="utf-8"
            )
        elif fmt == "csv":
            with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig để Excel đọc đúng tiếng Việt
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(_flatten(i) for i in items)
        elif fmt == "xlsx":
            try:
                from openpyxl import Workbook
            except ImportError:
                log.warning("Bỏ qua xlsx: cần `pip install openpyxl`")
                continue
            wb = Workbook()
            ws = wb.active
            ws.title = "data"
            ws.append(fields)
            for it in items:
                row = _flatten(it)
                ws.append([str(row.get(f)) if isinstance(row.get(f), (dict, list)) else row.get(f) for f in fields])
            ws.freeze_panes = "A2"
            wb.save(path)
        elif fmt == "xml":
            from scrapling.spiders.result import ItemList

            ItemList(items).to_xml(path)
        else:
            log.warning(f"Định dạng không hỗ trợ: {fmt}")
            continue
        written.append(path)
    return written


def run_config(config: Dict[str, Any], crawldir: Optional[str] = None) -> List[Dict[str, Any]]:
    spider = UniversalSpider(config, crawldir=crawldir)
    started = time.time()
    result = spider.start()
    items = list(result.items)
    files = export_items(items, spider.out_path, spider.formats)
    print(f"\n✔ {len(items)} item / {spider._pages} trang trong {time.time() - started:.1f}s")
    if spider.incremental:
        print(f"  (chế độ incremental: {len(spider._new_keys)} item mới)")
    for f in files:
        print(f"  → {f}")
    if result.paused:
        print("  ⏸ Đã tạm dừng. Chạy lại cùng lệnh (với --resume) để tiếp tục.")
    return items


# --------------------------------------------------------------------------------------
# Công cụ hỗ trợ: discover (API ẩn) & similar (tự sinh selector)
# --------------------------------------------------------------------------------------
def discover_apis(
    url: str,
    pattern: Optional[str] = None,
    scroll: int = 2,
    headless: bool = True,
    executable_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Mở trang bằng trình duyệt ẩn danh, ghi lại mọi request XHR/fetch trả về JSON."""
    from scrapling.fetchers import StealthySession

    def scroll_page(page):
        for _ in range(scroll):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)

    found = []
    extra = {"executable_path": executable_path} if executable_path else {}
    with StealthySession(
        headless=headless, record_requests=True, block_ads=True, network_idle=True, **extra
    ) as session:
        session.fetch(url, page_action=scroll_page if scroll else None)
        for rec in session.network.search(url_pattern=pattern, limit=None, include_static=False):
            body = rec.body or b""
            info: Dict[str, Any] = {"method": rec.method, "status": rec.status, "url": rec.url, "bytes": len(body)}
            try:
                data = json.loads(body)
                info["json"] = True
                if isinstance(data, dict):
                    info["keys"] = list(data)[:15]
                    lists = {k: len(v) for k, v in data.items() if isinstance(v, list)}
                    if lists:
                        info["lists"] = lists
                elif isinstance(data, list):
                    info["lists"] = {"[root]": len(data)}
            except (ValueError, TypeError):
                info["json"] = False
            found.append(info)
    found.sort(key=lambda r: (not r.get("json"), -r["bytes"]))
    return found


def suggest_selectors(url: str, sample_text: str, stealth: bool = False) -> Dict[str, Any]:
    """Từ một đoạn văn bản mẫu (vd: tên 1 sản phẩm), tìm phần tử lặp lại và sinh selector danh sách."""
    if stealth:
        from scrapling.fetchers import StealthyFetcher

        page = StealthyFetcher.fetch(url, network_idle=True)
    else:
        from scrapling.fetchers import Fetcher

        page = Fetcher.get(url, impersonate="chrome")
    el = page.find_by_text(sample_text, partial=True, first_match=True)
    if el is None:
        raise SystemExit(f"Không tìm thấy văn bản mẫu: {sample_text!r}")

    # Leo lên các tầng cha cho tới khi gặp tầng có nhiều phần tử tương tự nhất (thẻ "card" sản phẩm)
    best, best_count, node = el, 0, el
    for _ in range(6):
        similar = node.find_similar()
        if len(similar) + 1 >= best_count and len(similar) >= 2:  # bằng nhau -> ưu tiên tầng cao hơn (cả "card")
            best, best_count = node, len(similar) + 1
        if node.parent is None or node.parent.tag in ("body", "html"):
            break
        node = node.parent
    similar = best.find_similar()
    # Bỏ ":nth-of-type(n)" để selector khớp mọi phần tử trong danh sách
    card_selector = re.sub(r":nth-of-type\(\d+\)", "", best.generate_css_selector)
    tag_classes = best.attrib.get("class", "").split()
    simple = best.tag + ("." + ".".join(tag_classes) if tag_classes else "")
    return {
        "sample_element": el.generate_css_selector,
        "list_selector_full": card_selector,
        "list_selector_simple": simple,
        "count": len(similar) + 1,
        "preview": [best.get_all_text(separator=" ", strip=True)[:120]]
        + [s.get_all_text(separator=" ", strip=True)[:120] for s in list(similar)[:4]],
    }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _parse_fields(pairs: List[str]) -> Dict[str, Any]:
    fields = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--field phải có dạng ten=selector, nhận được: {pair}")
        k, v = pair.split("=", 1)
        fields[k.strip()] = v.strip()
    return fields


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Universal Crawler (Scrapling)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Chạy theo file cấu hình YAML/JSON")
    p_run.add_argument("config")
    p_run.add_argument("--resume", metavar="DIR", help="Thư mục checkpoint để tạm dừng/tiếp tục")
    p_run.add_argument("--max-items", type=int)
    p_run.add_argument("--max-pages", type=int)
    p_run.add_argument("--mode", choices=["http", "stealth", "auto"])
    p_run.add_argument("--out", help="Đường dẫn file kết quả (không cần đuôi)")
    p_run.add_argument("--proxy", action="append", help="Thêm proxy (lặp lại để xoay vòng)")
    p_run.add_argument("--dev", action="store_true", help="Bật cache response (development mode)")

    p_quick = sub.add_parser("quick", help="Crawl nhanh không cần file cấu hình")
    p_quick.add_argument("url", nargs="+")
    p_quick.add_argument("--field", action="append", help='ten=selector, vd: title="h1::text"')
    p_quick.add_argument("--list", help="Selector của mỗi item trong trang danh sách")
    p_quick.add_argument("--follow", action="append", help="Regex link cần đi theo")
    p_quick.add_argument("--match", help="Regex URL trang chứa dữ liệu (trang chi tiết)")
    p_quick.add_argument("--next", help="Selector link trang kế tiếp, vd: 'a.next::attr(href)'")
    p_quick.add_argument("--mode", choices=["http", "stealth", "auto"], default="auto")
    p_quick.add_argument("--max-items", type=int, default=0)
    p_quick.add_argument("--max-pages", type=int, default=100)
    p_quick.add_argument("--out", default="output/quick")
    p_quick.add_argument("--formats", default="json,csv")

    p_login = sub.add_parser("login", help="Mở trình duyệt để bạn tự đăng nhập, lưu phiên vào thư mục profile")
    p_login.add_argument("url")
    p_login.add_argument("--profile", required=True, help="Thư mục profile (trùng với fetch.user_data_dir)")
    p_login.add_argument("--browser", help="Đường dẫn Chrome/Chromium riêng (executable_path)")

    p_disc = sub.add_parser("discover", help="Tìm các API JSON ẩn phía sau trang (dùng trình duyệt)")
    p_disc.add_argument("url")
    p_disc.add_argument("--filter", help="Regex lọc URL API")
    p_disc.add_argument("--scroll", type=int, default=2)
    p_disc.add_argument("--show", action="store_true", help="Hiện cửa sổ trình duyệt")
    p_disc.add_argument("--browser", help="Đường dẫn Chrome/Chromium riêng (executable_path)")

    p_sim = sub.add_parser("similar", help="Tự sinh selector danh sách từ một văn bản mẫu")
    p_sim.add_argument("url")
    p_sim.add_argument("sample")
    p_sim.add_argument("--stealth", action="store_true")

    p_md = sub.add_parser("markdown", help="Crawl cả website thành Markdown (cho AI/RAG)")
    p_md.add_argument("url")
    p_md.add_argument("--max-pages", type=int, default=50)
    p_md.add_argument("--allow", action="append")
    p_md.add_argument("--out", default="output/markdown")
    p_md.add_argument("--mode", choices=["http", "stealth", "auto"], default="auto")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.cmd == "run":
        cfg = load_config(args.config)
        cfg.setdefault("crawl", {})
        cfg.setdefault("fetch", {})
        cfg.setdefault("output", {})
        if args.max_items is not None:
            cfg["crawl"]["max_items"] = args.max_items
        if args.max_pages is not None:
            cfg["crawl"]["max_pages"] = args.max_pages
        if args.mode:
            cfg["fetch"]["mode"] = args.mode
        if args.out:
            cfg["output"]["path"] = args.out
        if args.proxy:
            cfg["fetch"]["proxies"] = args.proxy
        if args.dev:
            cfg["crawl"]["dev_cache"] = True
        run_config(cfg, crawldir=args.resume)

    elif args.cmd == "quick":
        fields = _parse_fields(args.field)
        cfg = {
            "name": "quick",
            "start_urls": args.url,
            "fetch": {"mode": args.mode},
            "crawl": {"max_items": args.max_items, "max_pages": args.max_pages, "autothrottle": True},
            "follow": [{"allow": f} for f in (args.follow or [])],
            "items": [{"match": args.match, "list": args.list, "fields": fields, "auto_extract": not fields}],
            "output": {"path": args.out, "formats": args.formats.split(",")},
        }
        if args.next:
            cfg["pagination"] = {"next_css": args.next}
        run_config(cfg)

    elif args.cmd == "login":
        from scrapling.fetchers import StealthySession

        profile = Path(args.profile).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        extra = {"executable_path": args.browser} if args.browser else {}

        def wait_for_user(page):
            input("\n>>> Hãy đăng nhập trong cửa sổ trình duyệt, xong thì quay lại đây nhấn Enter... ")
            return page

        with StealthySession(headless=False, user_data_dir=str(profile), timeout=600_000, **extra) as session:
            session.fetch(args.url, page_action=wait_for_user, network_idle=False)
        print(f"✔ Đã lưu phiên đăng nhập vào {profile}")

    elif args.cmd == "discover":
        for rec in discover_apis(args.url, args.filter, args.scroll, not args.show, args.browser):
            mark = "JSON" if rec.get("json") else "    "
            extra = f" lists={rec['lists']}" if rec.get("lists") else ""
            print(f"[{mark}] {rec['status']} {rec['method']:4} {rec['bytes']:>8}B  {rec['url']}{extra}")

    elif args.cmd == "similar":
        print(json.dumps(suggest_selectors(args.url, args.sample, args.stealth), ensure_ascii=False, indent=2))

    elif args.cmd == "markdown":
        cfg = {
            "name": "markdown",
            "start_urls": [args.url],
            "fetch": {"mode": args.mode},
            "crawl": {"max_pages": args.max_pages, "autothrottle": True},
            "follow": [{"allow": a} for a in (args.allow or [""])],
            "items": [{"fields": {"title": "title::text"}, "markdown": True}],
            "output": {"path": args.out, "formats": ["jsonl"]},
        }
        items = run_config(cfg)
        md_dir = Path(args.out)
        md_dir.mkdir(parents=True, exist_ok=True)
        for it in items:
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", urlparse(it["_url"]).path.strip("/"))[:120] or "index"
            (md_dir / f"{slug}.md").write_text(
                f"# {it.get('title') or ''}\n\n<{it['_url']}>\n\n{it.get('markdown', '')}", encoding="utf-8"
            )
        print(f"  → {len(items)} file .md trong {md_dir}/")


if __name__ == "__main__":
    main(sys.argv[1:])
