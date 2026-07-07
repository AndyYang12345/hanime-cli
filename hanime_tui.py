#!/usr/bin/env python3
"""
hanime1.me 视频搜索 + 视频源提取 TUI

基于 Textual 构建，支持鼠标点击 / 键盘操作：
  1. 输入关键词搜索视频
  2. 鼠标/键盘浏览结果列表
  3. 点击/回车选择视频 → 自动爬取视频源 (1080p/720p/480p)
  4. 输出视频源 URL 到终端

用法：
    conda activate hanime-scraper
    python hanime_tui.py [关键词]

依赖：cloudscraper, beautifulsoup4, textual
"""

import sys
from dataclasses import dataclass
from typing import ClassVar

import cloudscraper
from bs4 import BeautifulSoup
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    RichLog,
    Static,
)
from textual.binding import Binding
from textual.reactive import reactive

# ── 配置 ──────────────────────────────────────────────────
BASE_URL = "https://hanime1.me"
SEARCH_URL = f"{BASE_URL}/search"


@dataclass
class VideoResult:
    """搜索结果中的视频条目。"""
    title: str
    url: str
    duration: str
    views: str
    likes: str
    author: str
    thumbnail: str


@dataclass
class VideoSources:
    """视频页面提取的源文件信息。"""
    title: str
    poster: str
    sources: list[tuple[str, str]]  # [(resolution, url), ...]
    page_url: str


# ── 网络请求 ──────────────────────────────────────────────
def create_scraper() -> cloudscraper.CloudScraper:
    """创建 cloudscraper 实例。"""
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        }
    )


def search_videos(query: str) -> list[VideoResult]:
    """搜索视频，返回结果列表。"""
    scraper = create_scraper()
    url = f"{SEARCH_URL}?query={query}&type="
    resp = scraper.get(url, timeout=20)

    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for el in soup.select(".video-item-container"):
        link = el.select_one("a.video-link")
        href = link.get("href", "") if link else ""

        # 过滤广告
        if "erolabs" in href.lower() or "erodalabs" in href.lower():
            continue
        if not href.startswith("http"):
            href = BASE_URL + href

        full_title = el.get("title", "").strip()

        # 时长
        dur_el = el.select_one("div.duration")
        duration = dur_el.get_text(strip=True) if dur_el else ""

        # 统计
        stats = el.select("div.stat-item")
        likes = views = ""
        if len(stats) > 0:
            for i_tag in stats[0].select("i"):
                i_tag.decompose()
            likes = stats[0].get_text(strip=True)
        if len(stats) > 1:
            for i_tag in stats[1].select("i"):
                i_tag.decompose()
            views = stats[1].get_text(strip=True)

        # 标题
        title_el = el.select_one("div.title")
        title_text = title_el.get_text(strip=True) if title_el else full_title

        # 作者
        sub_el = el.select_one("div.subtitle a")
        author = sub_el.get_text(strip=True) if sub_el else ""

        # 缩略图
        thumb_el = el.select_one("img.main-thumb")
        thumb = thumb_el.get("src", "") if thumb_el else ""

        results.append(
            VideoResult(
                title=title_text or full_title,
                url=href,
                duration=duration,
                likes=likes,
                views=views,
                author=author,
                thumbnail=thumb,
            )
        )

    return results


def extract_video_sources(video_url: str) -> VideoSources | None:
    """从视频页面提取视频源文件 URL。"""
    scraper = create_scraper()
    resp = scraper.get(video_url, timeout=20)

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 标题 — 优先用 h1（视频页标题），回退到 <title>
    h1_el = soup.select_one("h1")
    title_el = soup.select_one("title")
    title = ""
    if h1_el:
        title = h1_el.get_text(strip=True)
    elif title_el:
        title = title_el.get_text(strip=True)
    # 清理站点名后缀 "… - Hanime1.me" 等（注意 &nbsp; → \xa0）
    for sep in ("\xa0-\xa0H動漫", "\xa0-\xa0Hanime1.me", " - H動漫", " - Hanime1.me"):
        if sep in title:
            title = title.split(sep)[0].strip()

    # 视频元素
    player = soup.select_one("video#player")
    poster = player.get("poster", "") if player else ""

    sources: list[tuple[str, str]] = []
    if player:
        for s in player.select("source"):
            size = s.get("size", "?")
            src = s.get("src", "")
            if src:
                sources.append((f"{size}p", src))

    # 按分辨率排序（高到低）
    sources.sort(key=lambda x: int(x[0].replace("p", "")), reverse=True)

    return VideoSources(
        title=title,
        poster=poster,
        sources=sources,
        page_url=video_url,
    )


# ── TUI 界面 ──────────────────────────────────────────────
class ResultItem(ListItem):
    """搜索结果列表项。"""

    def __init__(self, video: VideoResult) -> None:
        self.video = video
        # 提前构建元信息字符串
        meta_parts = []
        if video.duration:
            meta_parts.append(f"⏱ {video.duration}")
        if video.views:
            meta_parts.append(f"👁 {video.views}")
        if video.likes:
            meta_parts.append(f"♥ {video.likes}")
        self._meta_str = "  ·  ".join(meta_parts)
        self._author_str = f"by {video.author}" if video.author else ""
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold]{self.video.title}[/bold]\n"
            f"  [dim]{self._author_str}[/dim]  {self._meta_str}"
        )


class SearchScreen(Screen):
    """搜索界面。"""

    CSS = """
    SearchScreen {
        align: center middle;
    }
    #search-container {
        width: 90%;
        height: 90%;
        border: solid $primary;
        background: $surface;
    }
    #search-header {
        padding: 1 2;
        background: $primary-darken-1;
        dock: top;
        height: auto;
    }
    #search-header Label {
        text-style: bold;
        color: $text;
    }
    #query-input {
        dock: top;
        margin: 1 2;
        height: 3;
    }
    #results-area {
        height: 1fr;
    }
    #results-list {
        height: 1fr;
    }
    #results-list > ListItem {
        padding: 0 2;
        height: auto;
    }
    #results-list > ListItem:hover {
        background: $primary-lighten-1;
    }
    #results-list > ListItem > Static {
        width: 100%;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $primary-darken-1;
        padding: 0 2;
    }
    .result-title {
        text-style: bold;
        color: $text;
    }
    .result-meta {
        color: $text-disabled;
    }
    .result-meta-highlight {
        color: $success;
    }
    .loading-container {
        width: 100%;
        height: auto;
        padding: 2 4;
    }
    #no-results {
        width: 100%;
        height: 100%;
        content-align: center middle;
        color: $text-disabled;
    }
    .help-text {
        color: $text-disabled;
        text-style: italic;
    }
    """

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Label("🔍 Hanime1.me 视频搜索", id="search-title"),
                id="search-header",
            ),
            Input(
                placeholder="输入关键词搜索… (Enter 搜索)",
                id="query-input",
            ),
            VerticalScroll(
                ListView(id="results-list"),
                id="results-area",
            ),
            Static("", id="status-bar"),
            id="search-container",
        )

    def on_mount(self) -> None:
        """初始化：如果有命令行参数作为搜索词，直接搜索。"""
        self.query_one("#query-input").focus()
        # 如果传入了初始搜索词
        if self.app._initial_query:
            inp = self.query_one("#query-input", Input)
            inp.value = self.app._initial_query
            self.perform_search(self.app._initial_query)

    def update_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)

    @on(Input.Submitted, "#query-input")
    def on_search_submit(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.perform_search(query)

    @work(thread=True, exclusive=True)
    def perform_search(self, query: str) -> None:
        """后台执行搜索（不阻塞 UI）。"""
        self.update_status(f"🔍 搜索中：{query} …")
        results = search_videos(query)
        self.app.call_from_thread(self.display_results, results)

    def display_results(self, results: list[VideoResult]) -> None:
        """在 UI 中显示搜索结果（清空旧的，填入新的）。"""
        list_view = self.query_one("#results-list", ListView)

        if not results:
            list_view.clear()
            self.update_status("0 个结果")
            return

        # 用新条目替换旧条目
        items = [ResultItem(v) for v in results]
        list_view.clear()
        list_view.mount(*items)
        self.update_status(f"✅ 找到 {len(results)} 个结果 — ↑↓ 选择  Enter 打开  Esc 退出")

    @on(ListView.Selected, "#results-list")
    def on_result_selected(self, event: ListView.Selected) -> None:
        """选中搜索结果 → 进入详情页面。"""
        item = event.item
        if isinstance(item, ResultItem):
            self.app.push_screen(VideoDetailScreen(item.video))

    def on_screen_resume(self) -> None:
        """从详情页返回时刷新状态。"""
        inp = self.query_one("#query-input", Input)
        inp.focus()


class VideoDetailScreen(Screen):
    """视频详情 + 源文件提取界面。"""

    video: VideoResult
    _sources_data: VideoSources | None = None

    CSS = """
    VideoDetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 85%;
        height: 85%;
        border: solid $primary;
        background: $surface;
    }
    #detail-header {
        padding: 1 2;
        background: $primary-darken-2;
        dock: top;
        height: auto;
    }
    #video-title {
        text-style: bold;
    }
    #sources-area {
        height: 1fr;
        padding: 1 2;
    }
    #sources-area RichLog {
        height: 1fr;
        background: $surface-darken-1;
        border: solid $surface-lighten-1;
        min-height: 10;
    }
    .source-section {
        padding: 1 0;
    }
    .source-section Label {
        text-style: bold;
        color: $secondary;
    }
    #loading-indicator {
        height: auto;
        padding: 1 2;
    }
    #detail-actions {
        dock: bottom;
        height: auto;
        padding: 1 2;
        background: $surface-darken-1;
        align-horizontal: center;
    }
    #detail-actions Button {
        margin: 0 1;
    }
    #download-btn {
        background: $success;
        color: $text;
    }
    .resolution-1080 {
        color: $success;
        text-style: bold;
    }
    .resolution-720 {
        color: $warning;
    }
    .resolution-480 {
        color: $text-disabled;
    }
    """

    def __init__(self, video: VideoResult) -> None:
        super().__init__()
        self.video = video

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Label(f"🎬 {self.video.title}", id="video-title"),
                id="detail-header",
            ),
            VerticalScroll(
                Vertical(
                    Label("⏳ 正在提取视频源… 请稍候"),
                    LoadingIndicator(id="loading-indicator"),
                    id="loading-container",
                ),
                id="sources-area",
            ),
            Horizontal(
                Button("🔙 返回搜索结果", variant="default", id="back-btn"),
                Button("📋 复制 1080p URL", variant="primary", id="copy-1080-btn"),
                Button("📋 复制全部", variant="primary", id="copy-all-btn"),
                id="detail-actions",
            ),
            Footer(),
            id="detail-container",
        )

    def on_mount(self) -> None:
        """进入详情页即开始提取视频源。"""
        self.extract_sources()

    @work(thread=True, exclusive=True)
    def extract_sources(self) -> None:
        """后台提取视频源（不阻塞 UI）。"""
        data = extract_video_sources(self.video.url)
        self._sources_data = data
        self.app.call_from_thread(self.display_sources, data)

    def display_sources(self, data: VideoSources | None) -> None:
        """在 RichLog 中显示视频源信息。"""
        area = self.query_one("#sources-area", VerticalScroll)
        area.remove_children()

        if not data or not data.sources:
            area.mount(Label("❌ 未能提取到视频源 URL。"))
            return

        log = RichLog(markup=True, wrap=True, highlight=True)
        log.write(f"[bold]📺 {data.title}[/bold]")
        log.write(f"[dim]🔗 页面: {data.page_url}[/dim]")
        log.write("")

        if data.poster:
            log.write(f"[dim]🖼 封面: {data.poster}[/dim]")
            log.write("")

        log.write("[bold underline]📥 视频源文件：[/bold underline]")
        log.write("")

        for res, url in data.sources:
            if res == "1080p":
                prefix = "⭐"
                color = "bold green"
            elif res == "720p":
                prefix = "  "
                color = "yellow"
            else:
                prefix = "  "
                color = "dim"

            log.write(f"[{color}]{prefix} [{res}] {url}[/{color}]")

        log.write("")
        if any(r == "1080p" for r, _ in data.sources):
            log.write("[bold green]✅ 已找到 1080p 源！[/bold green]")
        else:
            log.write("[bold yellow]⚠ 无 1080p 源，使用最高可用画质。[/bold yellow]")

        area.mount(log)

    @on(Button.Pressed, "#back-btn")
    def go_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#copy-1080-btn")
    def copy_1080(self) -> None:
        """复制 1080p URL 到剪贴板。"""
        url_1080 = self._get_best_url("1080p")
        if url_1080:
            self._copy_to_clipboard(url_1080)
            self.notify("✅ 1080p URL 已复制！", severity="information", timeout=3)
        else:
            self.notify("⚠ 该视频无 1080p 源", severity="warning", timeout=3)

    @on(Button.Pressed, "#copy-all-btn")
    def copy_all(self) -> None:
        """复制所有分辨率 URL 到剪贴板。"""
        if not self._sources_data:
            self.notify("⚠ 无数据可复制", severity="warning", timeout=3)
            return

        lines = [f"{res}: {url}" for res, url in self._sources_data.sources]
        text = "\n".join(lines)
        self._copy_to_clipboard(text)
        self.notify("✅ 全部 URL 已复制！", severity="information", timeout=3)

    def _get_best_url(self, target_res: str = "1080p") -> str | None:
        """获取指定分辨率的 URL，找不到则回退到最高可用画质。"""
        if not self._sources_data or not self._sources_data.sources:
            return None
        # 精确匹配
        for res, url in self._sources_data.sources:
            if res == target_res:
                return url
        # 回退到最高分辨率（已按分辨率降序排列）
        return self._sources_data.sources[0][1]

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        """将文本复制到系统剪贴板。"""
        import shutil
        import subprocess

        try:
            # 优先 xclip
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                check=False,
            )
        except FileNotFoundError:
            try:
                # 备选 xsel
                subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text.encode(),
                    check=False,
                )
            except FileNotFoundError:
                # 最后尝试通过 Python 打印（用户需手动复制）
                pass


class HanimeTuiApp(App):
    """主应用。"""

    TITLE = "Hanime1.me 视频搜索"
    SUB_TITLE = "搜索 · 选择 · 提取视频源"

    CSS = """
    Screen {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("escape", "back_or_quit", "返回/退出"),
    ]

    _initial_query: str = ""

    def __init__(self, initial_query: str = "") -> None:
        super().__init__()
        self._initial_query = initial_query

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())

    def action_back_or_quit(self) -> None:
        """Esc: 如果在详情页则返回，在搜索页则退出。"""
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()


# ── 入口 ──────────────────────────────────────────────────
def main():
    initial_query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    app = HanimeTuiApp(initial_query)
    app.run()


if __name__ == "__main__":
    main()
