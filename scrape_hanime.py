#!/usr/bin/env python3
"""
hanime1.me 首页爬虫
抓取首页信息，按大标题 + 小标题格式输出到终端。

用法：
    conda activate hanime-scraper
    python scrape_hanime.py

依赖：cloudscraper, beautifulsoup4, requests
"""

import sys
from datetime import datetime

import cloudscraper
from bs4 import BeautifulSoup

# ── 配置 ──────────────────────────────────────────────────
BASE_URL = "https://hanime1.me"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ANSI 颜色 / 样式 (终端输出美化)
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

SEPARATOR = "─" * 72
THIN_SEP = "·" * 72


# ── 工具函数 ──────────────────────────────────────────────
def create_scraper() -> cloudscraper.CloudScraper:
    """创建 cloudscraper 实例，绕过 Cloudflare 防护。"""
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        }
    )


def parse_video_item(video_el) -> dict | None:
    """解析单个视频卡片，返回视频信息字典。"""
    try:
        # 标题（来自容器的 title 属性，最完整）
        full_title = video_el.get("title", "").strip()

        # 链接
        link_el = video_el.select_one("a.video-link")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = BASE_URL + href

        # 缩略图
        thumb_el = video_el.select_one("img.main-thumb")
        thumb_src = thumb_el.get("src", "") if thumb_el else ""

        # 时长
        duration_el = video_el.select_one("div.duration")
        duration = duration_el.get_text(strip=True) if duration_el else ""

        # 好评率 & 观看次数
        stats = video_el.select("div.stat-item")
        likes = ""
        if len(stats) > 0:
            # 移除 <i> 标签（material-icons），只保留数字文本
            for i_tag in stats[0].select("i"):
                i_tag.decompose()
            likes = stats[0].get_text(strip=True)
        views = stats[1].get_text(strip=True) if len(stats) > 1 else ""
        # 同样清理 views 中可能的 icon 文字
        if len(stats) > 1:
            for i_tag in stats[1].select("i"):
                i_tag.decompose()
            views = stats[1].get_text(strip=True)

        # 原标题（div.title）
        title_el = video_el.select_one("div.title")
        title_text = title_el.get_text(strip=True) if title_el else full_title

        # 作者 & 发布时间
        subtitle_el = video_el.select_one("div.subtitle a")
        author_info = subtitle_el.get_text(strip=True) if subtitle_el else ""

        return {
            "title": full_title or title_text,
            "url": href,
            "thumbnail": thumb_src,
            "duration": duration,
            "likes": likes,
            "views": views,
            "author": author_info,
        }
    except Exception:
        return None


def parse_banner(soup: BeautifulSoup) -> dict:
    """解析头图 / Banner 区域。"""
    banner = soup.select_one("#home-banner-wrapper")
    if not banner:
        return {}

    # 视频标题
    title_el = banner.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    # 副标题（作者·观看次数·时间）
    subtitle_el = banner.select_one("h4.hidden-xs")
    subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""

    # 标签
    tags = []
    tag_container = banner.select_one("div.hidden-sm.hidden-md")
    if tag_container:
        for span in tag_container.select("span"):
            tag_text = span.get_text(strip=True)
            if tag_text:
                tags.append(tag_text)

    # 播放链接
    play_btn = banner.select_one("a.home-banner-play-btn")
    play_url = play_btn.get("href", "") if play_btn else ""

    return {
        "title": title,
        "subtitle": subtitle,
        "tags": tags,
        "play_url": play_url,
    }


def parse_category_rows(soup: BeautifulSoup) -> list[dict]:
    """解析视频分类行（最新上市、裏番、3DCG 等）。"""
    rows = []

    # 直接遍历 #home-rows-wrapper 的子元素：标题(a) + 视频容器(div) 交替出现
    home_rows = soup.select_one("#home-rows-wrapper")
    if not home_rows:
        return rows

    current_category = None
    for child in home_rows.children:
        if not hasattr(child, "name") or child.name is None:
            continue

        if child.name == "a" and "horizontal-row-title" in child.get("class", []):
            # 这是一个分类标题
            raw_text = child.get_text(strip=True)
            # 去掉 "查看更多arrow_forward_ios" 后缀，提取纯分类名
            category_name = raw_text.replace("查看更多arrow_forward_ios", "").strip()
            category_url = child.get("href", "")
            if category_url and not category_url.startswith("http"):
                category_url = BASE_URL + category_url
            current_category = {
                "name": category_name,
                "url": category_url,
                "videos": [],
            }
            rows.append(current_category)

        elif child.name == "div" and current_category is not None:
            # 这是视频容器
            video_items = child.select(".video-item-container")
            for vi in video_items:
                info = parse_video_item(vi)
                if info:
                    current_category["videos"].append(info)

    return rows


def parse_nav_genres(soup: BeautifulSoup) -> list[dict]:
    """从导航栏提取分类标签。"""
    genres = []
    seen = set()

    # 从主导航 (main-nav-home) 中提取 genre 链接，排除 horizontal-row-title 里的重复项
    for a in soup.select("#main-nav-home a[href], #sub-nav-home-mobile a[href]"):
        href = a.get("href", "")
        if "/search?genre=" in href:
            text = a.get_text(strip=True)
            # 跳过带 "查看更多" 的行标题链接
            if text and "查看更多" not in text and text not in seen:
                seen.add(text)
                genres.append({"name": text, "url": href if href.startswith("http") else BASE_URL + href})

    return genres


# ── 输出函数 ──────────────────────────────────────────────
def print_banner(banner: dict):
    """格式化输出 Banner 信息。"""
    if not banner:
        print(f"{RED}⚠ 未能解析 Banner 区域{RESET}")
        return

    print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  🎬  頭 版 推 薦  (Featured Banner)               ║{RESET}")
    print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════╝{RESET}\n")

    print(f"  {BOLD}{YELLOW}▶ {banner['title']}{RESET}")
    if banner["subtitle"]:
        print(f"  {DIM}{banner['subtitle']}{RESET}")

    if banner["tags"]:
        print(f"\n  {GREEN}🏷  標籤：{RESET}")
        # 每行放 6 个标签
        for i in range(0, len(banner["tags"]), 6):
            chunk = banner["tags"][i : i + 6]
            print(f"     " + "  ·  ".join(chunk))

    print()


def print_category(cat: dict, index: int):
    """格式化输出一个视频分类行。"""
    name = cat["name"]
    videos = cat["videos"]

    # 分类标题
    print(f"\n{BOLD}{MAGENTA}┌─ {index}. {name} ({len(videos)} 部) {RESET}")
    print(f"{MAGENTA}│  {THIN_SEP}{RESET}")

    for i, v in enumerate(videos, 1):
        # 标题行
        title = v["title"]
        if len(title) > 55:
            title = title[:52] + "..."

        duration_str = f"{CYAN}[{v['duration']}]{RESET}" if v["duration"] else ""
        likes_str = f"{GREEN}♥ {v['likes']}{RESET}" if v["likes"] else ""
        views_str = f"{DIM}{v['views']}{RESET}" if v["views"] else ""

        print(f"  {MAGENTA}│{RESET} {BOLD}{i:2d}.{RESET} {title}  {duration_str}  {likes_str}  {views_str}")

        # 作者信息
        if v["author"]:
            print(f"  {MAGENTA}│{RESET}     {DIM}by {v['author']}{RESET}")

        # 链接
        if v["url"]:
            print(f"  {MAGENTA}│{RESET}     {BLUE}{v['url']}{RESET}")

    print(f"{MAGENTA}└{SEPARATOR}{RESET}")


def print_nav_genres(genres: list[dict]):
    """输出导航栏分类速览。"""
    if not genres:
        return
    print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  📂  分 類 快 速 導 覽 (Genre Quick Links)       ║{RESET}")
    print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════╝{RESET}\n")
    for i, g in enumerate(genres, 1):
        print(f"  {BOLD}{i:2d}.{RESET} {g['name']}")
        print(f"     {BLUE}{g['url']}{RESET}")
    print()


def print_summary(banner: dict, categories: list[dict], genres: list[dict]):
    """输出汇总统计。"""
    total_videos = sum(len(c["videos"]) for c in categories)

    print(f"\n{BOLD}{CYAN}╔════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  📊  匯  總  統  計                                ║{RESET}")
    print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════╝{RESET}\n")

    print(f"  {GREEN}✅ 站點狀態：正常訪問{RESET}")
    print(f"  {GREEN}🎬 頭版推薦：{banner['title'] if banner else 'N/A'}{RESET}")
    print(f"  {GREEN}📂 視頻分類：{len(categories)} 個{RESET}")
    print(f"  {GREEN}🎥 視頻總數：{total_videos} 部{RESET}")
    print(f"  {GREEN}🏷  導航標籤：{len(genres)} 個{RESET}")
    print(f"  {DIM}🕐 抓取時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print()


# ── 主逻辑 ────────────────────────────────────────────────
def main():
    print(f"\n{BOLD}{YELLOW}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{YELLOW}║     Hanime1.me 首頁爬蟲                                  ║{RESET}")
    print(f"{BOLD}{YELLOW}╚══════════════════════════════════════════════════════════════╝{RESET}")
    print(f"\n  {DIM}正在連線到 {BASE_URL} ...{RESET}\n")

    # 1. 创建 scraper 并发起请求
    scraper = create_scraper()
    try:
        resp = scraper.get(BASE_URL, timeout=30)
    except Exception as e:
        print(f"{RED}✖ 連線失敗：{e}{RESET}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"{RED}✖ HTTP 狀態碼異常：{resp.status_code}{RESET}")
        sys.exit(1)

    print(f"  {GREEN}✔ 連線成功 (HTTP {resp.status_code})，頁面大小 {len(resp.text):,} 字節{RESET}")

    # 2. 解析 HTML
    soup = BeautifulSoup(resp.text, "html.parser")

    # 3. 提取各区域数据
    banner = parse_banner(soup)
    categories = parse_category_rows(soup)
    genres = parse_nav_genres(soup)

    # 4. 格式化输出
    # 4a. 头版推荐
    print_banner(banner)

    # 4b. 导航分类速览
    print_nav_genres(genres)

    # 4c. 各分类视频
    print(f"{BOLD}{CYAN}╔════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  🎥  各 分 類 視 頻 詳 情                          ║{RESET}")
    print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════╝{RESET}")

    for i, cat in enumerate(categories, 1):
        print_category(cat, i)

    # 5. 汇总
    print_summary(banner, categories, genres)


if __name__ == "__main__":
    main()
