"""
淘宝搜索页 Selenium 数据采集模板
====================================
适用场景：毕业设计数据采集（教学示例）
使用方式：python taobao_selenium_crawler.py

依赖安装：
    pip install selenium

运行环境要求：
    - Python 3.8+
    - Google Chrome 已安装（Selenium 4 会自动管理 ChromeDriver）

说明：
    淘宝页面结构和 CSS 类名会频繁更新，本脚本中的选择器可能随时失效。
    若提取字段为空，请用浏览器开发者工具（F12）重新检查 HTML 结构，
    更新对应的 CSS_SELECTOR 常量即可，无需改动其他逻辑。

合规提示：
    - 仅采集公开展示内容，不绕过技术保护措施
    - 控制采集频率，避免对服务器造成压力
    - 遵守淘宝用户服务协议与 robots.txt
"""

import csv
import re
import time
from datetime import datetime
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ──────────────────────────────────────────────
# 配置区：按需修改
# ──────────────────────────────────────────────

# 搜索关键词（修改为你需要的品类）
KEYWORD = "手机"

# 滚动次数（每次滚动 800px，建议 5~10 次）
SCROLL_TIMES = 8

# 每次滚动后等待的秒数（模拟真实用户浏览，不要设置过小）
SCROLL_PAUSE = 2

# 等待页面元素出现的超时时间（秒）
PAGE_WAIT_TIMEOUT = 30

# ──────────────────────────────────────────────
# CSS 选择器区：淘宝页面改版后在此更新
# ──────────────────────────────────────────────
# 以下选择器为参考值，实际页面结构请用 F12 检查后更新。
# 提示：右键目标元素 → 检查 → 查看 class 属性。

# 商品卡片容器（每张商品卡片对应的最外层标签）
SEL_ITEM_CARD = "div[class*='Card--doubleCard'], div[class*='item'], .pc-result-list .pc-result-item"

# 以下为每张卡片内的子元素选择器
SEL_TITLE     = "[class*='title'], [class*='Title']"      # 商品标题
SEL_PRICE     = "[class*='price'], [class*='Price']"      # 价格
SEL_SHOP      = "[class*='shop'], [class*='Shop'], [class*='store'], [class*='Store']"  # 店铺名
SEL_LOCATION  = "[class*='location'], [class*='Location'], [class*='area'], [class*='Area']"  # 发货地
SEL_SALES     = "[class*='sale'], [class*='Sale'], [class*='sold'], [class*='Sold']"   # 销量文本
SEL_AD_FLAG   = "[class*='ad'], [class*='Ad'], [class*='mark'], [class*='Mark']"       # 广告标记

# 搜索框与搜索按钮（淘宝首页）
SEL_SEARCH_INPUT  = "input#q, input[name='q'], input[type='search']"
SEL_SEARCH_BUTTON = "button[type='submit'], .btn-search, #J_TSearchForm button"


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    """创建并返回 Chrome WebDriver 实例。"""
    options = Options()
    # 关闭自动化特征标识，降低被检测为机器人的概率
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # 设置窗口大小，模拟普通用户分辨率
    options.add_argument("--window-size=1280,900")
    # 如需无头模式（不弹出浏览器窗口），取消下面这行的注释
    # options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    return driver


def scroll_page(driver: webdriver.Chrome, times: int = SCROLL_TIMES, pause: float = SCROLL_PAUSE) -> None:
    """
    逐步向下滚动页面，触发懒加载内容。

    Args:
        driver: WebDriver 实例
        times:  滚动次数
        pause:  每次滚动后等待时间（秒）
    """
    print(f"  正在滚动页面（共 {times} 次）...")
    for i in range(times):
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(pause)
        print(f"    滚动 {i + 1}/{times} 完成")


def save_html(driver: webdriver.Chrome, keyword: str) -> str:
    """
    将当前页面 HTML 保存到本地文件。

    Args:
        driver:  WebDriver 实例
        keyword: 搜索关键词（用于文件命名）

    Returns:
        保存的文件路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_kw = re.sub(r"[^\w\u4e00-\u9fff]", "_", keyword)  # 文件名安全处理
    filename = f"taobao_page_{safe_kw}_{timestamp}.html"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    print(f"  页面 HTML 已保存 → {filename}")
    return filename


def extract_item_id(url: str) -> str:
    """从商品 URL 中提取 item ID（用于去重与追踪）。"""
    # 淘宝商品链接通常包含 id=<数字> 或 /item/<数字>
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/item/(\d+)", url)
    if m:
        return m.group(1)
    return ""


def extract_products(driver: webdriver.Chrome, keyword: str, page_num: int = 1) -> list[dict]:
    """
    从当前搜索结果页提取商品信息。

    注意：选择器基于参考页面结构，淘宝改版后需重新更新 SEL_* 常量。
    每个字段均用 try/except 包裹，单字段失败不影响整条记录。

    Args:
        driver:   WebDriver 实例
        keyword:  搜索关键词（写入每行数据用于区分数据集）
        page_num: 当前页码（写入数据集，方便后续分析）

    Returns:
        商品信息字典列表，字段见下方 row 构造
    """
    crawl_time = datetime.now().isoformat(timespec="seconds")
    results = []

    # 尝试获取商品卡片列表
    cards = driver.find_elements(By.CSS_SELECTOR, SEL_ITEM_CARD)

    if not cards:
        print("  ⚠️  未找到商品卡片，选择器可能已失效，请用 F12 检查页面结构。")
        print(f"     当前 URL：{driver.current_url}")
        return results

    print(f"  找到 {len(cards)} 个商品卡片，开始提取字段...")

    for card in cards:
        # ── 标题 ──────────────────────────────────
        title = ""
        try:
            el = card.find_element(By.CSS_SELECTOR, SEL_TITLE)
            title = el.text.strip()
        except Exception:
            pass

        # ── 价格 ──────────────────────────────────
        price = ""
        try:
            el = card.find_element(By.CSS_SELECTOR, SEL_PRICE)
            price = el.text.strip()
        except Exception:
            pass

        # ── 店铺名 ────────────────────────────────
        shop_name = ""
        try:
            el = card.find_element(By.CSS_SELECTOR, SEL_SHOP)
            shop_name = el.text.strip()
        except Exception:
            pass

        # ── 发货地 ────────────────────────────────
        location = ""
        try:
            el = card.find_element(By.CSS_SELECTOR, SEL_LOCATION)
            location = el.text.strip()
        except Exception:
            pass

        # ── 销量文本 ──────────────────────────────
        sales_text = ""
        try:
            el = card.find_element(By.CSS_SELECTOR, SEL_SALES)
            sales_text = el.text.strip()
        except Exception:
            pass

        # ── 商品链接 ──────────────────────────────
        item_url = ""
        try:
            el = card.find_element(By.TAG_NAME, "a")
            item_url = el.get_attribute("href") or ""
            # 部分链接是 // 相对协议，补全
            if item_url.startswith("//"):
                item_url = "https:" + item_url
        except Exception:
            pass

        # ── 商品 ID（从链接提取）──────────────────
        item_id = extract_item_id(item_url)

        # ── 是否广告 ──────────────────────────────
        is_ad = 0
        try:
            card.find_element(By.CSS_SELECTOR, SEL_AD_FLAG)
            is_ad = 1
        except Exception:
            pass

        # 过滤完全空白的卡片（可能是广告占位或结构外层容器）
        if not any([title, price, item_url]):
            continue

        row = {
            "keyword":    keyword,      # 搜索关键词
            "crawl_time": crawl_time,   # 采集时间
            "page_num":   page_num,     # 页码
            "item_id":    item_id,      # 商品 ID
            "title":      title,        # 商品标题
            "price":      price,        # 价格
            "shop_name":  shop_name,    # 店铺名
            "location":   location,     # 发货地
            "sales_text": sales_text,   # 销量文本
            "item_url":   item_url,     # 商品链接
            "is_ad":      is_ad,        # 是否广告（1=是，0=否）
            "label":      "",           # 自定义标签（手动标注用，初始为空）
        }
        results.append(row)

    return results


def save_csv(data: list[dict], keyword: str) -> str:
    """
    将提取结果写入 CSV 文件。

    Args:
        data:    商品字典列表
        keyword: 关键词（用于文件命名）

    Returns:
        保存的文件路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_kw = re.sub(r"[^\w\u4e00-\u9fff]", "_", keyword)
    filename = f"taobao_{safe_kw}_{timestamp}.csv"

    fieldnames = [
        "keyword", "crawl_time", "page_num", "item_id",
        "title", "price", "shop_name", "location",
        "sales_text", "item_url", "is_ad", "label",
    ]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        # utf-8-sig 带 BOM，Excel 可直接正确识别中文
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    print(f"  数据已写入 CSV → {filename}（共 {len(data)} 行）")
    return filename


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def main() -> None:
    print("=" * 55)
    print(" 淘宝 Selenium 数据采集脚本（毕业设计教学版）")
    print("=" * 55)

    raw = input(f"\n请输入搜索关键词（直接回车使用默认值 '{KEYWORD}'）：").strip()
    keyword = raw if raw else KEYWORD
    # 基本输入校验：限制长度，去除控制字符，防止意外注入
    keyword = keyword[:100]
    keyword = re.sub(r"[\x00-\x1f\x7f]", "", keyword)
    if not keyword:
        keyword = KEYWORD
    print(f"\n关键词：{keyword}")

    driver = make_driver()
    wait = WebDriverWait(driver, PAGE_WAIT_TIMEOUT)

    try:
        # ── 步骤 1：打开淘宝首页 ──────────────────
        print("\n[1/5] 打开淘宝首页...")
        driver.get("https://www.taobao.com")
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        # ── 步骤 2：等待手动登录 ──────────────────
        print("\n[2/5] 请在浏览器中完成登录（扫码或账号密码均可）。")
        print("      登录完成后，回到此终端窗口，按 Enter 继续...")
        input("      >> 按 Enter 继续：")

        # ── 步骤 3：执行搜索 ──────────────────────
        print(f"\n[3/5] 搜索关键词：{keyword}")
        # 直接拼接搜索 URL 更可靠，避免搜索框选择器失效
        search_url = f"https://s.taobao.com/search?q={quote(keyword)}"
        driver.get(search_url)

        # 等待页面商品卡片或主体内容出现，超时则退回 body
        try:
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, SEL_ITEM_CARD + ", body")
            ))
        except Exception:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        print(f"      当前 URL：{driver.current_url}")
        print(f"      页面标题：{driver.title}")

        # ── 步骤 4：滚动触发懒加载 ────────────────
        print(f"\n[4/5] 滚动页面（{SCROLL_TIMES} 次）...")
        scroll_page(driver)

        # ── 步骤 5：保存 HTML ─────────────────────
        print("\n[5/5] 保存页面 HTML...")
        html_file = save_html(driver, keyword)

        # ── 步骤 6（可选）：提取结构化字段 ──────────
        print("\n[可选] 尝试提取商品字段（选择器可能已失效，失败不影响 HTML 保存）...")
        products = extract_products(driver, keyword, page_num=1)

        if products:
            csv_file = save_csv(products, keyword)
            print(f"\n✅ 采集完成！")
            print(f"   HTML 页面：{html_file}")
            print(f"   CSV 数据 ：{csv_file}（{len(products)} 条）")
        else:
            print("\n⚠️  结构化提取未获取到数据，但 HTML 已保存。")
            print(f"   你可以离线解析 HTML 文件：{html_file}")
            print("   建议用 F12 检查当前页面选择器后更新脚本中的 SEL_* 常量。")

    except KeyboardInterrupt:
        print("\n\n用户中断，退出程序。")
    finally:
        print("\n关闭浏览器...")
        driver.quit()


if __name__ == "__main__":
    main()
