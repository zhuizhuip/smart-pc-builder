import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import JavascriptException, TimeoutException, WebDriverException
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = DATA_DIR / "debug_pages"
DB_PATH = DATA_DIR / "taobao_market.db"
CSV_PATH = DATA_DIR / "taobao_pc_parts_snapshot.csv"
DEFAULT_PROFILE_DIR = BASE_DIR / "edge_profile"
SOURCE_PLATFORM = "taobao"

LOGIN_URL_MARKERS = (
    "login.taobao.com",
    "passport.taobao.com",
    "identity_verify",
    "login_unusual",
    "member/login",
)

PRICE_RE = re.compile(r"(?:¥|￥)\s*([0-9]+(?:\.[0-9]{1,2})?)")
GENERIC_PRICE_RE = re.compile(r"\b([0-9]{2,6}(?:\.[0-9]{1,2})?)\b")
SPACE_RE = re.compile(r"\s+")

WINDOW_STATE_JS = """
const candidates = [
  window.__INITIAL_STATE__,
  window.__PRELOADED_STATE__,
  window.g_page_config,
  window.__INIT_DATA__,
  window.__SEARCH_DATA__
];
for (const item of candidates) {
  if (item) {
    return item;
  }
}
return null;
"""

CARD_SNAPSHOT_JS = """
const result = [];
const seen = new Set();
const anchors = Array.from(
  document.querySelectorAll(
    "a[href*='item.taobao.com/item.htm'], a[href*='detail.tmall.com/item.htm']"
  )
);

for (const anchor of anchors) {
  const href = anchor.href || anchor.getAttribute("href") || "";
  if (!href || seen.has(href)) {
    continue;
  }

  let current = anchor;
  let best = anchor;
  for (let i = 0; i < 8 && current; i += 1) {
    current = current.parentElement;
    if (!current) {
      break;
    }
    const text = (current.innerText || "").trim();
    if (text.length >= 20) {
      best = current;
    }
    if (text.length >= 40 && text.length <= 1200) {
      break;
    }
  }

  result.push({
    href,
    title: (anchor.getAttribute("title") || anchor.textContent || "").trim(),
    text: (best.innerText || "").trim()
  });
  seen.add(href);
}
return result;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="淘宝公开搜索结果低频采集脚本（最小可运行版）"
    )
    parser.add_argument("--keyword", default="内存条", help="搜索关键词")
    parser.add_argument("--pages", type=int, default=1, help="采集页数，建议先用 1")
    parser.add_argument(
        "--page-wait-seconds",
        type=float,
        default=8.0,
        help="每页加载后的额外等待时间",
    )
    parser.add_argument(
        "--min-interval-seconds",
        type=float,
        default=18.0,
        help="两次页面访问之间的最小等待时间",
    )
    parser.add_argument(
        "--max-interval-seconds",
        type=float,
        default=28.0,
        help="两次页面访问之间的最大等待时间",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=180,
        help="如果页面跳转到登录/验证页，预留的人工处理时间",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Edge 浏览器用户目录，默认复用项目内 edge_profile",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="不建议开启；默认使用可见浏览器窗口",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="采集完成后保留浏览器窗口，方便你检查结果",
    )
    parser.add_argument(
        "--html-file",
        default="",
        help="手动保存的淘宝搜索结果 HTML 文件路径；提供后将跳过浏览器抓取，直接解析入库",
    )
    parser.add_argument(
        "--scan-saved-html",
        action="store_true",
        help="自动扫描当前目录下匹配模式的已保存 HTML 并批量入库",
    )
    parser.add_argument(
        "--html-glob",
        default="*淘宝搜索.html",
        help="批量扫描时使用的文件匹配模式，默认 *淘宝搜索.html",
    )
    return parser.parse_args()


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


def normalize_text(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ")
    return SPACE_RE.sub(" ", text).strip()


def normalize_url(url: str) -> str:
    url = normalize_text(url)
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def normalize_source_url(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    if "click.simba.taobao.com" not in normalized:
        return normalized
    try:
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        item_id = (query.get("id") or [""])[0]
        if item_id:
            return f"https://item.taobao.com/item.htm?id={item_id}"
    except Exception:
        return normalized
    return normalized


def normalize_price(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    match = PRICE_RE.search(text)
    if match:
        return match.group(1)
    if text.replace(".", "", 1).isdigit():
        return text
    generic = GENERIC_PRICE_RE.search(text)
    return generic.group(1) if generic else ""


def build_price_from_parts(int_part: Any, float_part: Any) -> str:
    int_text = normalize_text(int_part)
    float_text = normalize_text(float_part).lstrip(".")
    if not int_text:
        return ""
    if float_text:
        return f"{int_text}.{float_text}"
    return int_text


def infer_prices_from_blob(text_blob: str) -> tuple[str, str]:
    matches = PRICE_RE.findall(text_blob or "")
    if not matches:
        generic = GENERIC_PRICE_RE.findall(text_blob or "")
        matches = [value for value in generic if len(value.replace(".", "")) >= 2]
    unique_prices: list[str] = []
    for price in matches:
        if price not in unique_prices:
            unique_prices.append(price)
    if not unique_prices:
        return "", ""
    if len(unique_prices) == 1:
        return unique_prices[0], unique_prices[0]
    return unique_prices[0], unique_prices[1]


def infer_shop_from_blob(text_blob: str, title: str) -> str:
    lines = [normalize_text(line) for line in (text_blob or "").splitlines()]
    title = normalize_text(title)
    skip_keywords = (
        "付款",
        "人收货",
        "评价",
        "包邮",
        "退货",
        "客服",
        "天猫",
        "淘宝",
        "进店",
        "找相似",
        "¥",
        "￥",
    )
    for line in lines:
        if not line or line == title:
            continue
        if any(keyword in line for keyword in skip_keywords):
            continue
        if normalize_price(line):
            continue
        if len(line) > 30:
            continue
        return line
    return ""


def is_valid_row(row: dict[str, Any]) -> bool:
    sku_name = normalize_text(row.get("sku_name"))
    if len(sku_name) < 4:
        return False
    coupon_price = normalize_price(row.get("coupon_price"))
    original_price = normalize_price(row.get("original_price"))
    return bool(coupon_price or original_price)


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        row["source_url"] = normalize_url(row.get("source_url", ""))
        row["source_url"] = normalize_source_url(row["source_url"])
        row["shop"] = normalize_text(row.get("shop", ""))
        row["sku_name"] = normalize_text(row.get("sku_name", ""))
        row["coupon_price"] = normalize_price(row.get("coupon_price", ""))
        row["original_price"] = normalize_price(row.get("original_price", ""))
        if not row["coupon_price"] and row["original_price"]:
            row["coupon_price"] = row["original_price"]
        if not row["original_price"] and row["coupon_price"]:
            row["original_price"] = row["coupon_price"]
        if not is_valid_row(row):
            continue

        key = (
            row["source_url"] or row["sku_name"],
            row["shop"],
            row["coupon_price"] or row["original_price"],
        )
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = row
            continue

        existing_score = int(bool(existing.get("shop"))) + int(bool(existing.get("source_url")))
        current_score = int(bool(row.get("shop"))) + int(bool(row.get("source_url")))
        if current_score >= existing_score:
            best_by_key[key] = row

    return list(best_by_key.values())


def walk_data_object(obj: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        title = (
            obj.get("raw_title")
            or obj.get("title")
            or obj.get("name")
            or obj.get("itemTitle")
        )
        price_show = obj.get("priceShow") if isinstance(obj.get("priceShow"), dict) else {}
        coupon_price = (
            obj.get("coupon_price")
            or obj.get("couponPrice")
            or price_show.get("price")
            or obj.get("view_price")
            or obj.get("price")
            or obj.get("priceText")
        )
        original_price = (
            obj.get("originalPrice")
            or obj.get("origin_price")
            or price_show.get("originPrice")
            or price_show.get("price")
            or obj.get("reserve_price")
            or coupon_price
        )
        shop_info = obj.get("shopInfo") if isinstance(obj.get("shopInfo"), dict) else {}
        shop = (
            obj.get("nick")
            or obj.get("shopName")
            or obj.get("sellerNick")
            or shop_info.get("title")
        )
        url = (
            obj.get("detail_url")
            or obj.get("auctionURL")
            or obj.get("url")
            or obj.get("item_url")
        )

        if title and (coupon_price or original_price):
            out.append(
                {
                    "shop": shop or "",
                    "sku_name": title,
                    "coupon_price": coupon_price or "",
                    "original_price": original_price or "",
                    "source_url": url or "",
                }
            )

        for value in obj.values():
            walk_data_object(value, out)
        return

    if isinstance(obj, list):
        for item in obj:
            walk_data_object(item, out)


def extract_rows_from_html(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    patterns = (
        r"g_page_config\s*=\s*(\{.*?\})\s*;",
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;",
        r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;",
        r"window\.__INIT_DATA__\s*=\s*(\{.*?\})\s*;",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.S):
            try:
                walk_data_object(json.loads(match.group(1)), rows)
            except json.JSONDecodeError:
                continue

    soup = BeautifulSoup(html, "lxml")

    card_rows: list[dict[str, Any]] = []
    for card in soup.select("a[id^='item_id_']"):
        href = normalize_url(card.get("href") or "")
        if not href:
            continue

        title_node = card.select_one("[class*='title--'][title]")
        if title_node is None:
            title_node = card.select_one("[class*='title--']")
        title = ""
        if title_node is not None:
            title = normalize_text(title_node.get("title") or title_node.get_text(" ", strip=True))

        shop_node = card.select_one("span[class*='shopNameText']")
        shop = normalize_text(shop_node.get_text(" ", strip=True) if shop_node else "")

        price_int_node = card.select_one("[class*='priceInt']")
        price_float_node = card.select_one("[class*='priceFloat']")
        coupon_price = build_price_from_parts(
            price_int_node.get_text(" ", strip=True) if price_int_node else "",
            price_float_node.get_text(" ", strip=True) if price_float_node else "",
        )

        if not title or not coupon_price:
            continue

        card_rows.append(
            {
                "shop": shop,
                "sku_name": title,
                "coupon_price": coupon_price,
                "original_price": coupon_price,
                "source_url": href,
            }
        )

    if card_rows:
        return dedupe_rows(card_rows)

    links = soup.select("a[href*='item.taobao.com/item.htm'], a[href*='detail.tmall.com/item.htm']")
    for link in links:
        title = normalize_text(link.get("title") or link.get_text(" ", strip=True))
        if not title:
            continue
        parent = link
        blob = ""
        for _ in range(6):
            parent = parent.parent
            if parent is None:
                break
            blob = normalize_text(parent.get_text("\n", strip=True))
            if len(blob) >= 20:
                break
        coupon_price, original_price = infer_prices_from_blob(blob)
        if not (coupon_price or original_price):
            continue
        rows.append(
            {
                "shop": infer_shop_from_blob(blob, title),
                "sku_name": title,
                "coupon_price": coupon_price,
                "original_price": original_price,
                "source_url": link.get("href") or "",
            }
        )

    return dedupe_rows(rows)


def is_deny_h5_html(html: str) -> bool:
    lowered = (html or "").lower()
    return "deny_h5" in lowered or "action=deny" in lowered


def extract_rows_from_card_snapshots(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        title = normalize_text(item.get("title"))
        text_blob = item.get("text", "")
        coupon_price, original_price = infer_prices_from_blob(text_blob)
        if not title or not (coupon_price or original_price):
            continue
        rows.append(
            {
                "shop": infer_shop_from_blob(text_blob, title),
                "sku_name": title,
                "coupon_price": coupon_price,
                "original_price": original_price,
                "source_url": item.get("href", ""),
            }
        )
    return dedupe_rows(rows)


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def init_database() -> None:
    ensure_storage()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS taobao_pc_parts_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crawl_batch_id TEXT NOT NULL,
                crawl_time TEXT NOT NULL,
                keyword TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                source_platform TEXT NOT NULL,
                shop TEXT,
                sku_name TEXT NOT NULL,
                coupon_price TEXT,
                original_price TEXT,
                source_url TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_html_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                html_sha256 TEXT NOT NULL UNIQUE,
                source_file TEXT NOT NULL,
                keyword TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def compute_html_sha256(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()


def is_html_already_imported(html_sha256: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM imported_html_registry WHERE html_sha256 = ? LIMIT 1",
            (html_sha256,),
        ).fetchone()
    return row is not None


def register_imported_html(
    html_sha256: str,
    source_file: str,
    keyword: str,
    page_no: int,
    row_count: int,
    imported_at: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO imported_html_registry (
                html_sha256,
                source_file,
                keyword,
                page_no,
                row_count,
                imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (html_sha256, source_file, keyword, page_no, row_count, imported_at),
        )
        conn.commit()


def persist_rows(
    rows: list[dict[str, Any]],
    crawl_batch_id: str,
    crawl_time: str,
    keyword: str,
    page_no: int,
) -> None:
    if not rows:
        return

    db_rows = [
        (
            crawl_batch_id,
            crawl_time,
            keyword,
            page_no,
            SOURCE_PLATFORM,
            row.get("shop", ""),
            row.get("sku_name", ""),
            row.get("coupon_price", ""),
            row.get("original_price", ""),
            row.get("source_url", ""),
        )
        for row in rows
    ]

    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            """
            INSERT INTO taobao_pc_parts_snapshot (
                crawl_batch_id,
                crawl_time,
                keyword,
                page_no,
                source_platform,
                shop,
                sku_name,
                coupon_price,
                original_price,
                source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            db_rows,
        )
        conn.commit()

    file_exists = CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "crawl_batch_id",
                "crawl_time",
                "keyword",
                "page_no",
                "source_platform",
                "shop",
                "sku_name",
                "coupon_price",
                "original_price",
                "source_url",
            ],
        )
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "crawl_batch_id": crawl_batch_id,
                    "crawl_time": crawl_time,
                    "keyword": keyword,
                    "page_no": page_no,
                    "source_platform": SOURCE_PLATFORM,
                    "shop": row.get("shop", ""),
                    "sku_name": row.get("sku_name", ""),
                    "coupon_price": row.get("coupon_price", ""),
                    "original_price": row.get("original_price", ""),
                    "source_url": row.get("source_url", ""),
                }
            )


def locate_edge_driver() -> str:
    candidates = [
        shutil.which("msedgedriver"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedgedriver.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedgedriver.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise FileNotFoundError("未找到 msedgedriver，请先确认 Edge Driver 已安装。")


def build_search_url(keyword: str, page_no: int) -> str:
    offset = max(page_no - 1, 0) * 44
    query = quote_plus(keyword)
    return f"https://s.taobao.com/search?q={query}&s={offset}"


def create_driver(profile_dir: Path, headless: bool) -> webdriver.Edge:
    service = Service(executable_path=locate_edge_driver())
    options = Options()
    options.use_chromium = True
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=zh-CN")
    if headless:
        options.add_argument("--headless=new")

    driver = webdriver.Edge(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver


def is_login_or_risk_page(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in LOGIN_URL_MARKERS)


def wait_for_document_ready(driver: webdriver.Edge, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )


def wait_for_manual_login(driver: webdriver.Edge, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current_url = driver.current_url
        if not is_login_or_risk_page(current_url):
            return True
        remaining = int(deadline - time.time())
        log(f"当前处于登录/验证页面，请手动处理，剩余等待 {remaining} 秒")
        time.sleep(5)
    return False


def gentle_scroll(driver: webdriver.Edge) -> None:
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.35);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
    except JavascriptException:
        return


def fetch_window_state_rows(driver: webdriver.Edge) -> list[dict[str, Any]]:
    try:
        payload = driver.execute_script(WINDOW_STATE_JS)
    except JavascriptException:
        return []
    if not payload:
        return []
    rows: list[dict[str, Any]] = []
    walk_data_object(payload, rows)
    return dedupe_rows(rows)


def fetch_card_snapshot_rows(driver: webdriver.Edge) -> list[dict[str, Any]]:
    try:
        items = driver.execute_script(CARD_SNAPSHOT_JS)
    except JavascriptException:
        return []
    if not isinstance(items, list):
        return []
    return extract_rows_from_card_snapshots(items)


def save_debug_html(keyword: str, page_no: int, html: str) -> Path:
    safe_keyword = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "_", keyword)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = DEBUG_DIR / f"debug_{safe_keyword}_p{page_no}_{timestamp}.html"
    file_path.write_text(html, encoding="utf-8", errors="ignore")
    return file_path


def crawl_page(driver: webdriver.Edge, keyword: str, page_no: int, page_wait_seconds: float) -> tuple[list[dict[str, Any]], Path]:
    search_url = build_search_url(keyword, page_no)
    log(f"访问第 {page_no} 页：{search_url}")
    driver.get(search_url)
    wait_for_document_ready(driver)
    time.sleep(page_wait_seconds)
    gentle_scroll(driver)

    html = driver.page_source
    debug_path = save_debug_html(keyword, page_no, html)

    if is_deny_h5_html(html):
        return [], debug_path

    rows = fetch_window_state_rows(driver)
    if rows:
        return rows, debug_path

    rows = extract_rows_from_html(html)
    if rows:
        return rows, debug_path

    rows = fetch_card_snapshot_rows(driver)
    return rows, debug_path


def visit_interval(min_seconds: float, max_seconds: float) -> None:
    lower = min(min_seconds, max_seconds)
    upper = max(min_seconds, max_seconds)
    sleep_seconds = random.uniform(lower, upper)
    log(f"进入下一页前等待 {sleep_seconds:.1f} 秒")
    time.sleep(sleep_seconds)


def print_storage_summary(total_rows: int) -> None:
    log(f"本次采集完成，共写入 {total_rows} 条记录")
    log(f"SQLite: {DB_PATH}")
    log(f"CSV   : {CSV_PATH}")
    log(f"调试页: {DEBUG_DIR}")


def infer_keyword_from_html_filename(file_path: Path) -> str:
    stem = file_path.stem.strip()
    if "_taobao_search" in stem:
        keyword = stem.split("_taobao_search", 1)[0].strip()
        if keyword:
            return keyword
    if "taobao_search" in stem:
        keyword = stem.split("taobao_search", 1)[0].strip(" _-")
        if keyword:
            return keyword
    if "_淘宝搜索" in stem:
        keyword = stem.split("_淘宝搜索", 1)[0].strip()
        if keyword:
            return keyword
    if "淘宝搜索" in stem:
        keyword = stem.split("淘宝搜索", 1)[0].strip(" _-")
        if keyword:
            return keyword
    return stem


def import_single_html_file(
    html_path: Path,
    crawl_batch_id: str,
    keyword: str,
    page_no: int,
) -> int:
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    html_sha256 = compute_html_sha256(html)
    if is_html_already_imported(html_sha256):
        log(f"跳过重复 HTML（哈希已存在）：{html_path.name}")
        return 0

    if is_deny_h5_html(html):
        log(f"跳过风控页：{html_path.name}")
        return 0

    rows = extract_rows_from_html(html)
    if not rows:
        log(f"未从 HTML 提取到商品数据：{html_path.name}")
        return 0

    crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    persist_rows(
        rows=rows,
        crawl_batch_id=crawl_batch_id,
        crawl_time=crawl_time,
        keyword=keyword,
        page_no=page_no,
    )
    register_imported_html(
        html_sha256=html_sha256,
        source_file=str(html_path),
        keyword=keyword,
        page_no=page_no,
        row_count=len(rows),
        imported_at=crawl_time,
    )
    log(f"已导入 {html_path.name} -> 关键词={keyword}，记录数={len(rows)}")
    return len(rows)


def import_saved_html_files(html_glob: str, crawl_batch_id: str) -> int:
    matched_files = sorted(
        [
            path
            for path in BASE_DIR.rglob(html_glob)
            if path.is_file()
            and (
                "taobao_search" in path.stem.lower()
                or "淘宝搜索" in path.stem
                or "淘宝数据" in str(path.parent)
            )
        ],
        key=lambda path: path.name.lower(),
    )
    if not matched_files:
        log(f"未找到匹配文件：{html_glob}")
        return -1

    total_rows = 0
    page_counter_by_keyword: dict[str, int] = {}
    for html_path in matched_files:
        keyword = infer_keyword_from_html_filename(html_path)
        page_counter_by_keyword[keyword] = page_counter_by_keyword.get(keyword, 0) + 1
        total_rows += import_single_html_file(
            html_path=html_path,
            crawl_batch_id=crawl_batch_id,
            keyword=keyword,
            page_no=page_counter_by_keyword[keyword],
        )
    return total_rows


def main() -> int:
    args = parse_args()
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    init_database()
    crawl_batch_id = datetime.now().strftime("%Y%m%d%H%M%S")
    total_rows = 0
    driver: webdriver.Edge | None = None

    if args.scan_saved_html:
        total_rows = import_saved_html_files(
            html_glob=args.html_glob,
            crawl_batch_id=crawl_batch_id,
        )
        if total_rows < 0:
            return 9
        print_storage_summary(total_rows)
        return 0

    if args.html_file:
        html_path = Path(args.html_file).resolve()
        if not html_path.exists():
            log(f"HTML 文件不存在：{html_path}")
            return 6
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        if is_deny_h5_html(html):
            log("提供的 HTML 仍然是 deny_h5/风控页，无法提取商品数据")
            return 7
        rows = extract_rows_from_html(html)
        if not rows:
            log("HTML 已读取，但未提取到商品数据")
            return 8
        crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persist_rows(
            rows=rows,
            crawl_batch_id=crawl_batch_id,
            crawl_time=crawl_time,
            keyword=args.keyword,
            page_no=1,
        )
        total_rows = len(rows)
        print_storage_summary(total_rows)
        return 0

    try:
        driver = create_driver(profile_dir=profile_dir, headless=args.headless)
        log("浏览器已启动，准备进入淘宝搜索页面")

        first_url = build_search_url(args.keyword, 1)
        driver.get(first_url)
        wait_for_document_ready(driver)

        if is_login_or_risk_page(driver.current_url):
            log("检测到淘宝要求登录或身份验证，开始等待人工处理")
            if not wait_for_manual_login(driver, args.login_wait_seconds):
                log("人工登录/验证超时，本次未继续采集")
                return 2
            log("人工处理完成，开始正式采集")

        for page_no in range(1, max(args.pages, 1) + 1):
            if page_no > 1:
                visit_interval(args.min_interval_seconds, args.max_interval_seconds)

            rows, debug_path = crawl_page(
                driver=driver,
                keyword=args.keyword,
                page_no=page_no,
                page_wait_seconds=args.page_wait_seconds,
            )
            crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if not rows:
                log(f"第 {page_no} 页未提取到商品数据，已保存调试页面：{debug_path}")
                continue

            persist_rows(
                rows=rows,
                crawl_batch_id=crawl_batch_id,
                crawl_time=crawl_time,
                keyword=args.keyword,
                page_no=page_no,
            )
            total_rows += len(rows)
            log(f"第 {page_no} 页提取 {len(rows)} 条，调试页面：{debug_path}")

        print_storage_summary(total_rows)
        return 0

    except FileNotFoundError as exc:
        log(str(exc))
        return 3
    except TimeoutException as exc:
        log(f"页面加载超时：{exc}")
        return 4
    except WebDriverException as exc:
        log(f"浏览器驱动异常：{exc}")
        return 5
    finally:
        if driver is not None and not args.keep_browser_open:
            driver.quit()
            log("浏览器已关闭")


if __name__ == "__main__":
    sys.exit(main())
