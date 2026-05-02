"""
淘宝 Selenium 爬虫模板
======================
用途：毕业设计数据采集 —— 搜索指定关键词，翻页抓取，将每页完整 HTML 保存到本地。
后续可用 BeautifulSoup 等工具离线解析 HTML，提取商品标题、价格等字段。

使用前准备
----------
1. 安装依赖：
       pip install selenium
2. 安装 Chrome 浏览器（推荐最新稳定版）。
3. Selenium 4+ 会自动下载匹配版本的 ChromeDriver，无需手动配置。
4. 将下方 KEYWORD 改为你要搜索的关键词，MAX_PAGES 改为要抓取的页数。

如何用浏览器开发者工具查找选择器
----------------------------------
1. 按 F12 打开开发者工具。
2. 点击左上角「元素选择」按钮（或按 Ctrl+Shift+C）。
3. 鼠标悬停到你想定位的元素上，单击。
4. 右侧 Elements 面板会高亮对应的 HTML 代码。
5. 右键该行 → Copy → Copy selector 即可得到 CSS 选择器。

注意：淘宝页面结构经常更新，选择器可能随时失效。
     出现 NoSuchElementException 时请重新用开发者工具确认选择器。

免责声明
--------
本模板仅供学习与毕业设计研究使用。
请遵守淘宝用户协议及相关法律法规，不得用于商业爬取或大规模采集。
"""

import os
import time
import random

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ============================================================
# 用户配置区 —— 根据需要修改这里
# ============================================================
KEYWORD = "手机"           # 搜索关键词
MAX_PAGES = 3              # 最多抓取页数（淘宝免登录通常只能看前几页）
SAVE_DIR = "html_pages"    # HTML 文件保存目录
SCROLL_STEP = 800          # 每次滚动像素数（可根据屏幕分辨率调整）
SCROLL_REPEATS = 6         # 每页滚动次数
MAX_DISPLAY_ITEMS = 5      # 离线解析示例最多打印的商品条数
# ============================================================


def build_driver() -> webdriver.Chrome:
    """构建并返回一个隐藏自动化特征的 Chrome 实例。"""
    options = Options()
    # 如需无头模式（不弹出窗口），取消下一行注释；
    # 但注意无头模式下淘宝更易触发验证，建议调试时保持有头模式。
    # options.add_argument("--headless=new")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # 可选：指定浏览器缓存目录，保留登录 Cookie，避免每次重新扫码
    # options.add_argument("--user-data-dir=/path/to/chrome_profile")

    driver = webdriver.Chrome(options=options)
    # 覆盖 navigator.webdriver 属性，进一步降低被识别概率
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def wait_for_login(driver: webdriver.Chrome) -> None:
    """
    打开淘宝首页，暂停程序，等待用户手动扫码/登录。
    登录完成后在终端按回车，程序继续执行。
    """
    driver.get("https://www.taobao.com")
    print(f"\n[提示] 浏览器已打开淘宝首页。")
    print(f"       请在浏览器中完成登录（扫码或账号密码均可）。")
    print(f"       登录完成后，回到此终端按【回车键】继续...\n")
    input(">>> 按回车继续: ")


def search_keyword(driver: webdriver.Chrome, keyword: str) -> None:
    """在淘宝搜索框输入关键词并提交搜索。"""
    wait = WebDriverWait(driver, 20)

    # 搜索框选择器：若失效请用 F12 重新确认
    # 常见选择器：#q  /  input[name='q']  /  .search-combobox-input
    try:
        search_box = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#q, input[name='q']"))
        )
        search_box.clear()
        search_box.send_keys(keyword)
        search_box.send_keys(Keys.RETURN)
        print(f"[搜索] 关键词：{keyword}")
        time.sleep(random.uniform(3, 5))  # 随机等待，模拟真实用户
    except Exception as e:
        print(f"[警告] 搜索框定位失败，请检查选择器：{e}")
        raise


def scroll_page(driver: webdriver.Chrome, step: int = SCROLL_STEP, repeats: int = SCROLL_REPEATS) -> None:
    """
    逐步向下滚动页面，触发懒加载内容。
    step   : 每次滚动像素数
    repeats: 滚动次数
    """
    for _ in range(repeats):
        driver.execute_script(f"window.scrollBy(0, {step});")
        time.sleep(random.uniform(1.0, 2.0))
    # 最后滚回顶部，方便翻页按钮可见
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)


def save_page_html(driver: webdriver.Chrome, page_num: int, save_dir: str) -> str:
    """将当前页面的完整 HTML 保存到本地文件，返回文件路径。"""
    os.makedirs(save_dir, exist_ok=True)
    filename = os.path.join(save_dir, f"page_{page_num:02d}.html")
    html = driver.page_source
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[保存] 第 {page_num} 页 HTML → {filename}  (字节数: {len(html.encode())})")
    return filename


def go_to_next_page(driver: webdriver.Chrome) -> bool:
    """
    尝试点击"下一页"按钮。
    返回 True 表示成功翻页，False 表示已到最后一页或翻页失败。

    说明：淘宝分页按钮的选择器可能随版本变化，
          常见选择器参考：
            .next-pagination-item.next  （PC 版）
            a[aria-label='下一页']
            .pg-next
          建议使用 F12 在当前页面确认实际选择器后修改下方代码。
    """
    wait = WebDriverWait(driver, 10)
    next_selectors = [
        ".next-pagination-item.next",   # 常见 PC 端分页
        "a[aria-label='下一页']",
        ".pg-next",
        "button.next-btn",
    ]
    for selector in next_selectors:
        try:
            btn = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            btn.click()
            time.sleep(random.uniform(3, 5))
            return True
        except Exception:
            continue

    print("[提示] 未找到下一页按钮，可能已到最后一页或选择器需更新。")
    return False


def run_crawler(keyword: str = KEYWORD, max_pages: int = MAX_PAGES, save_dir: str = SAVE_DIR) -> None:
    """主流程：登录 → 搜索 → 翻页 → 保存 HTML。"""
    driver = build_driver()
    try:
        # 1. 打开淘宝，等待手动登录
        wait_for_login(driver)

        # 2. 执行搜索
        search_keyword(driver, keyword)

        # 3. 逐页抓取
        for page_num in range(1, max_pages + 1):
            print(f"\n--- 正在处理第 {page_num} 页 ---")
            print(f"    当前 URL: {driver.current_url}")

            # 滚动页面，触发懒加载
            scroll_page(driver)

            # 保存当前页 HTML
            save_page_html(driver, page_num, save_dir)

            # 最后一页不需要翻页
            if page_num < max_pages:
                success = go_to_next_page(driver)
                if not success:
                    print(f"[完成] 共成功保存 {page_num} 页数据。")
                    break
            else:
                print(f"[完成] 已到达设定最大页数 {max_pages}，停止抓取。")

    except KeyboardInterrupt:
        print("\n[中断] 用户手动停止爬虫。")
    except Exception as e:
        print(f"[错误] 爬虫运行异常：{e}")
        raise
    finally:
        driver.quit()
        print("[结束] 浏览器已关闭。")


# ============================================================
# 后续离线解析示例（仅供参考，不影响爬虫运行）
# ============================================================
def parse_saved_html(html_path: str) -> None:
    """
    示例：用 BeautifulSoup 解析已保存的 HTML 文件，提取商品信息。
    需要安装：pip install beautifulsoup4

    由于淘宝页面结构经常变化，下方选择器仅作参考，
    实际使用时请用 F12 在保存的 HTML 文件中确认正确标签与类名。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("请先安装 beautifulsoup4：pip install beautifulsoup4")
        return

    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    # 示例：尝试提取商品卡片（选择器需根据实际 HTML 调整）
    items = soup.select(".item, .card, [data-item-id]")
    print(f"解析到 {len(items)} 个商品卡片（选择器可能需调整）")

    for idx, item in enumerate(items[:MAX_DISPLAY_ITEMS], 1):  # 只打印前几条，数量由 MAX_DISPLAY_ITEMS 控制
        title_tag = item.select_one(".title, .name, h3")
        price_tag = item.select_one(".price, .priceInt")
        title = title_tag.get_text(strip=True) if title_tag else "（未找到标题）"
        price = price_tag.get_text(strip=True) if price_tag else "（未找到价格）"
        print(f"  [{idx}] 标题: {title[:40]}  价格: {price}")


# ============================================================
if __name__ == "__main__":
    run_crawler()
