#!/usr/bin/env python3
"""
hanime_kitty.py — Kitty-native TUI for hanime1.me

A full-featured terminal application using Kitty Graphics Protocol for
pixel-perfect thumbnail rendering and Kitty Keyboard Protocol for precise
keyboard + mouse input.

Requires: Kitty terminal (>= 0.29), Python 3.12+, conda env `hanime-scraper`

Usage:
    kitty python hanime_kitty.py [optional-search-query]

Screens:
    HomeScreen         — banner, category rows with thumbnails, search entry
    SearchScreen       — text input, result list with thumbnails
    VideoDetailScreen  — poster, source URLs, clickable tags

Navigation:
    Home → Enter/click search bar → Search
    Home → Click video/banner → Detail
    Search → Enter/click result → Detail
    Detail → Click tag → Search
    Esc → back to previous screen
    q   → quit
"""

# ═══════════════════════════════════════════════════════════════════════
# Section 1: Imports
# ═══════════════════════════════════════════════════════════════════════

import sys
import os
import queue
import threading
import subprocess
import time
from urllib.parse import urlparse, parse_qs

from kitty_ui import (
    Terminal, KittyGraphics, KeyboardReader, UIRenderer,
    Screen, install_resize_handler, resize_pending,
    RESET, BOLD, DIM, rgb_fg, rgb_bg,
)
from kitty_scraper import (
    VideoResult, VideoDetail, HomePageData, Category,
    search_videos, extract_video_detail, fetch_homepage,
    download_thumbnail, BASE_URL,
)


# ═══════════════════════════════════════════════════════════════════════
# Section 2: Constants
# ═══════════════════════════════════════════════════════════════════════

# Thumbnail display sizes (in terminal cells)
THUMB_COLS = 8           # category-row thumbnails
RESULT_THUMB_COLS = 12   # search-result thumbnails
BANNER_COLS = 38         # homepage banner
POSTER_COLS = 50         # video-detail poster

# Layout
SEARCH_ROW = 1           # search-bar / search-hint row (0-indexed)
BANNER_HEADER_ROW = 2    # "▼ 头版推荐" header
BANNER_IMG_ROW = 3       # banner image top
BANNER_IMG_ROWS = 10     # estimated banner image height in cells
CAT_START_ROW = 14       # first category row (after banner)
CAT_IMG_ROWS = 3         # category thumbnail height in cells
RESULT_START_ROW = 3     # first result row (after input + underline)
RESULT_IMG_ROWS = 4      # search result thumbnail height

# Colour palette
ACCENT = rgb_fg(100, 200, 255)
TAG_COLOUR = rgb_fg(180, 130, 255)
SOURCE_COLOUR = rgb_fg(100, 255, 130)
WARN_COLOUR = rgb_fg(255, 200, 100)


# ═══════════════════════════════════════════════════════════════════════
# Section 3: App — main event loop, navigation, threading
# ═══════════════════════════════════════════════════════════════════════

class App:
    """Application controller — owns terminal, graphics, input, screen stack."""

    def __init__(self):
        self.t = Terminal()
        self.gfx = KittyGraphics(self.t)
        self.kbd = KeyboardReader(self.t)
        self.ui = UIRenderer(self.t)
        self._screens: list[Screen] = []
        self._running = False
        self._job_queue: queue.Queue = queue.Queue()
        # URL → image_id cache so repeated thumbnails don't re-download
        self._thumb_cache: dict[str, int] = {}

    # ── Lifecycle ──────────────────────────────────────────────

    def run(self, initial_query: str = ""):
        """Enter raw mode, show first screen, start event loop."""
        # Check we're running inside Kitty (or another terminal with graphics support)
        term = os.environ.get("TERM", "")
        if "kitty" not in term:
            print(
                "WARNING: This app requires the Kitty terminal for graphics support.\n"
                f"         Current TERM={term or '(unset)'}. Images will not render.\n"
                "         Run with:  kitty python hanime_kitty.py",
                file=sys.stderr,
            )

        install_resize_handler()
        self.t.enter_raw()
        self.kbd.enable()
        self._running = True

        self.push_screen(HomeScreen(self))
        if initial_query:
            self.push_screen(SearchScreen(self, initial_query=initial_query))

        try:
            self._event_loop()
        finally:
            self.t.exit_raw()

    def _event_loop(self):
        """Main loop: resize → jobs → input → dispatch."""
        while self._running:
            # ── Handle terminal resize ──
            global resize_pending
            if resize_pending:
                resize_pending = False
                self.t.query_size()
                self.current_screen.on_resize()

            # ── Process background-job results ──
            self._process_jobs()

            # ── Read & dispatch input events ──
            events = self.kbd.read_events(timeout=0.05)
            for ev in events:
                if ev is None:
                    continue
                consumed = self.current_screen.handle_event(ev)
                if not consumed:
                    self._handle_global_event(ev)

            # Small sleep to avoid busy-waiting
            time.sleep(0.005)

    def _handle_global_event(self, ev: dict):
        """Handle events not consumed by the current screen."""
        if ev.get("type") == "key" and ev.get("event") == "press":
            code = ev["key_code"]
            if code == 113:  # 'q'
                self._running = False

    # ── Navigation ─────────────────────────────────────────────

    @property
    def current_screen(self) -> Screen:
        return self._screens[-1] if self._screens else None

    def push_screen(self, screen: Screen):
        """Push *screen* onto the navigation stack, making it active."""
        if self._screens:
            self._screens[-1].on_leave()
        self._screens.append(screen)
        screen.on_enter()

    def pop_screen(self):
        """Pop the current screen.  Does nothing if only one screen remains."""
        if len(self._screens) > 1:
            self._screens.pop().on_leave()
            self._screens[-1].on_enter()

    def replace_screen(self, screen: Screen):
        """Replace the top screen without growing the stack."""
        if self._screens:
            self._screens.pop().on_leave()
        self._screens.append(screen)
        screen.on_enter()

    # ── Background work ────────────────────────────────────────

    def run_in_background(self, target, callback):
        """Run *target()* on a daemon thread, then call *callback(result)* on main."""

        def _wrapper():
            try:
                result = target()
            except Exception:
                result = None
            self._job_queue.put((callback, result))

        t = threading.Thread(target=_wrapper, daemon=True)
        t.start()

    def _process_jobs(self):
        """Drain the job queue, calling each callback with its result."""
        try:
            while True:
                cb, result = self._job_queue.get_nowait()
                cb(result)
        except queue.Empty:
            pass

    # ── Thumbnail helpers ──────────────────────────────────────

    def get_cached_thumb(self, url: str) -> int | None:
        """Return cached image_id for *url*, or None."""
        return self._thumb_cache.get(url)

    def cache_thumb(self, url: str, image_id: int):
        """Store *image_id* for *url*."""
        self._thumb_cache[url] = image_id

    def display_thumb(self, img_data: bytes, row: int, col: int,
                      img_cols: int, url: str = "") -> int:
        """Display a thumbnail (or placeholder), returning the image_id."""
        image_id = 0
        if url:
            cached = self._thumb_cache.get(url)
            if cached:
                image_id = cached
        image_id = self.gfx.display_image(
            img_data, row=row, col=col, image_id=image_id,
            img_cols=img_cols, quiet=True,
        )
        if url and url not in self._thumb_cache:
            self._thumb_cache[url] = image_id
        return image_id


# ═══════════════════════════════════════════════════════════════════════
# Section 4: HomeScreen
# ═══════════════════════════════════════════════════════════════════════

class HomeScreen(Screen):
    """Homepage — banner, category rows, search entry."""

    def __init__(self, app: App):
        super().__init__(app)
        self.data: HomePageData | None = None
        self._loading = True
        self._error: str = ""
        self._scroll_offset = 0      # first visible category index
        self._banner_img_id = 0
        self._thumb_img_ids: dict[str, int] = {}   # url → image_id
        self._max_visible_cats = 0   # computed on draw

    # ── Lifecycle ──────────────────────────────────────────────

    def on_enter(self):
        if self.data is not None:
            # Already loaded (e.g. navigating back from search) — just redraw
            self.draw()
            return
        self._loading = True
        self._error = ""
        self.draw()  # shows "Loading homepage…" via the loading branch
        self.app.run_in_background(fetch_homepage, self._on_data_loaded)

    def on_resize(self):
        self.app.t.query_size()
        self.draw()

    # ── Data callback ──────────────────────────────────────────

    def _on_data_loaded(self, data: HomePageData | None):
        self._loading = False
        if data is None:
            self._error = "Failed to load homepage — check network / Cloudflare"
            self.draw()
            return
        self.data = data
        self.draw()
        # Kick off progressive thumbnail downloads
        self._load_thumbnails()

    def _load_thumbnails(self):
        """Download category thumbnails progressively, displaying each as it arrives."""
        if not self.data:
            return
        # Collect (url, video_url) pairs
        jobs: list[tuple[str, str]] = []
        for cat in self.data.categories:
            for v in cat.videos:
                if v.thumbnail and v.thumbnail not in self._thumb_img_ids:
                    jobs.append((v.thumbnail, v.url))
        if not jobs:
            return

        def _download_sequentially():
            """Download one at a time; enqueue each result individually."""
            for thumb_url, video_url in jobs:
                data = download_thumbnail(thumb_url)
                self.app._job_queue.put(
                    (self._on_single_thumb, (thumb_url, video_url, data))
                )

        self.app.run_in_background(_download_sequentially, lambda _: None)

    def _on_single_thumb(self, result: tuple):
        """Display a single downloaded thumbnail (called per-thumb, progressively)."""
        if self is not self.app.current_screen:
            return
        thumb_url, video_url, img_data = result
        if img_data and video_url not in self._thumb_img_ids:
            self._thumb_img_ids[video_url] = True
            self._redraw_single_thumb(video_url, img_data)

    def _redraw_single_thumb(self, video_url: str, img_data: bytes):
        """Redraw one thumbnail after download completes."""
        if not self.data:
            return
        cat_offset = 0
        for ci, cat in enumerate(self.data.categories):
            if ci < self._scroll_offset:
                continue
            vis_idx = ci - self._scroll_offset
            if vis_idx >= self._max_visible_cats:
                break
            cat_row = CAT_START_ROW + vis_idx * (CAT_IMG_ROWS + 1)
            for vi, v in enumerate(cat.videos):
                if v.url != video_url:
                    continue
                col = 2 + vi * (THUMB_COLS + 1)
                self.app.display_thumb(img_data, cat_row, col, THUMB_COLS, url=v.thumbnail)
                return

    # ── Draw ───────────────────────────────────────────────────

    def draw(self):
        """Full redraw of the homepage."""
        self.clear_click_zones()
        self.app.t.clear_screen()
        self._draw_bars()

        if self._loading:
            self.app.ui.draw_centered_text("⏳ Loading homepage…")
            return
        if self._error:
            self.app.ui.draw_centered_text(f"❌ {self._error}", style=WARN_COLOUR)
            return
        if not self.data:
            return

        self._draw_search_hint()
        self._draw_banner()
        self._draw_categories()

    def _draw_bars(self):
        """Title bar (with embedded search hint) + status bar."""
        t = self.app.t
        w = t.cols
        # Title bar: left-aligned title + right-aligned search hint
        left = " 🏠  Hanime1.me"
        right = "🔍 /搜索 "
        bar = left + " " * max(1, w - len(left) - len(right)) + right
        t.draw_text(0, 0, bar[:w], style=b"\x1b[7m")
        # Click zone on the search-hint part of the title bar
        self.add_click_zone(0, 0, w - len(right) - 1, w - 1, "_on_search_click", None)

        self.app.ui.draw_status_bar(
            "  ⬆⬇ navigate  ·  Enter 详情  ·  / 搜索  ·  q 退出"
        )

    def _draw_search_hint(self):
        """Draw a subtle clickable search hint below the title bar."""
        t = self.app.t
        w = t.cols
        hint = "Press / or Enter to search — type anything to start"
        c1 = max(2, (w - len(hint)) // 2)
        t.draw_text(SEARCH_ROW, c1, hint, style=DIM)
        self.add_click_zone(
            SEARCH_ROW, SEARCH_ROW, c1, c1 + len(hint),
            "_on_search_click", None,
        )

    def _draw_banner(self):
        """Draw the hero banner section."""
        if not self.data or not self.data.banner:
            return
        banner = self.data.banner
        t = self.app.t
        w = t.cols

        # Section header
        t.draw_text(BANNER_HEADER_ROW, 2, "▼ 头版推荐", style=BOLD)
        self.app.ui.draw_horizontal_separator(BANNER_HEADER_ROW + 1, 2, w - 2)

        # Image region
        img_r1 = BANNER_IMG_ROW
        banner_cols = min(BANNER_COLS, w - 30)  # leave room for text
        img_r2 = img_r1 + BANNER_IMG_ROWS - 1

        # Text column
        text_col = 4 + banner_cols
        text_r = img_r1

        if banner.title:
            t.draw_text(text_r, text_col, banner.title,
                        style=BOLD, max_width=w - text_col - 2)
            text_r += 2
        if banner.author:
            t.draw_text(text_r, text_col, banner.author,
                        style=DIM, max_width=w - text_col - 2)
            text_r += 1
        if self.data.banner_tags:
            tags_text = " · ".join(self.data.banner_tags[:8])
            t.draw_text(text_r, text_col, tags_text,
                        style=ACCENT, max_width=w - text_col - 2)

        # Banner image (progressive — show placeholder, then load)
        if banner.thumbnail:
            # Dim placeholder rectangle
            t.fill_rect(img_r1, 2, img_r2 - img_r1 + 1, banner_cols + 1, "░")
            # Click zone on banner image
            self.add_click_zone(img_r1, img_r2, 2, 2 + banner_cols,
                               "_on_video_click", banner.url)

            # Background download
            thumb_url = banner.thumbnail
            self.app.run_in_background(
                lambda: download_thumbnail(thumb_url),
                lambda data: self._on_banner_img(data, thumb_url, img_r1, banner_cols),
            )

    def _on_banner_img(self, img_data: bytes | None, url: str,
                       row: int, img_cols: int):
        """Display downloaded banner image."""
        if self is not self.app.current_screen:
            return
        if img_data:
            self.app.display_thumb(img_data, row, 2, img_cols, url=url)

    def _draw_categories(self):
        """Draw visible category rows with thumbnail placeholders."""
        if not self.data:
            return
        t = self.app.t
        w = t.cols
        cats = self.data.categories
        if not cats:
            return

        # How many categories can we show?
        avail_rows = (t.rows - 2) - CAT_START_ROW  # minus status bar
        cat_block = CAT_IMG_ROWS + 1  # header + image rows
        self._max_visible_cats = max(0, min(
            len(cats) - self._scroll_offset,
            avail_rows // cat_block,
        ))

        for vi in range(self._max_visible_cats):
            ci = self._scroll_offset + vi
            cat = cats[ci]
            cat_row = CAT_START_ROW + vi * cat_block

            # Category header
            header = f"▼ {cat.name} ({len(cat.videos)}部)"
            t.draw_text(cat_row, 2, header, style=BOLD)
            cat_row += 1

            # Thumbnails
            max_thumbs = min(len(cat.videos), (w - 4) // (THUMB_COLS + 1))
            for ti in range(max_thumbs):
                v = cat.videos[ti]
                col = 2 + ti * (THUMB_COLS + 1)
                # Placeholder
                placeholder = f"  {v.title[:THUMB_COLS - 2]:^{THUMB_COLS - 2}}"
                for r_off in range(CAT_IMG_ROWS):
                    if r_off == CAT_IMG_ROWS // 2:
                        t.draw_text(cat_row + r_off, col, placeholder,
                                    style=DIM, max_width=THUMB_COLS)
                    else:
                        t.fill_rect(cat_row + r_off, col, 1, THUMB_COLS, " ")
                # Click zone
                self.add_click_zone(
                    cat_row, cat_row + CAT_IMG_ROWS - 1,
                    col, col + THUMB_COLS,
                    "_on_video_click", v.url,
                )

        # Scroll indicator
        if self._scroll_offset > 0:
            t.draw_text(CAT_START_ROW + self._max_visible_cats * cat_block,
                        2, f"  ⬆ {self._scroll_offset} more above", style=DIM)
        if self._scroll_offset + self._max_visible_cats < len(cats):
            remaining = len(cats) - self._scroll_offset - self._max_visible_cats
            t.draw_text(CAT_START_ROW + self._max_visible_cats * cat_block,
                        2, f"  ⬇ {remaining} more below  (scroll)", style=DIM)

    # ── Event handlers ─────────────────────────────────────────

    def on_key(self, ev: dict) -> bool:
        if ev.get("event") != "press":
            return False
        code = ev["key_code"]
        if code in (13, 47, 115):  # Enter, '/', 's' → search
            self._on_search_click(None)
            return True
        elif code == 27:  # Esc
            self.app._running = False
            return True
        return False

    def on_mouse(self, ev: dict) -> bool:
        """Handle wheel scrolling in addition to click zones."""
        action = ev.get("action", "")
        # Left-click: delegate to base for click-zone matching
        if action == "press" and ev.get("button") == 0:
            if super().on_mouse(ev):
                return True

        # Wheel: scroll categories
        if action == "wheel":
            btn = ev.get("button", 0)  # 0=up, 1=down
            if btn == 0:  # wheel up
                self._scroll_offset = max(0, self._scroll_offset - 1)
            elif btn == 1:  # wheel down
                if self.data:
                    max_off = max(0, len(self.data.categories) - self._max_visible_cats)
                    self._scroll_offset = min(max_off, self._scroll_offset + 1)
            self.draw()
            return True
        return False

    def on_text(self, ev: dict) -> bool:
        """Typing anything starts search."""
        text = ev.get("text", "")
        if text and text.isprintable():
            self._on_search_click(text)
            return True
        return False

    def _on_search_click(self, data):
        """Navigate to search screen, optionally with initial text."""
        initial = data if isinstance(data, str) else ""
        self.app.push_screen(SearchScreen(self.app, initial_query=initial))

    def _on_video_click(self, url: str):
        """Navigate to video detail screen."""
        if url:
            self.app.push_screen(VideoDetailScreen(self.app, url))


# ═══════════════════════════════════════════════════════════════════════
# Section 5: SearchScreen
# ═══════════════════════════════════════════════════════════════════════

class SearchScreen(Screen):
    """Search input + result list with thumbnails."""

    def __init__(self, app: App, initial_query: str = ""):
        super().__init__(app)
        self.query = initial_query
        self.results: list[VideoResult] = []
        self._loading = False
        self._error = ""
        self._selected_idx = 0
        self._scroll_offset = 0
        self._result_count = 0
        self._max_visible = 0
        self._placeholder = "..."
        self._cursor_visible = True
        self._cursor_toggle_time = time.time()

    # ── Lifecycle ──────────────────────────────────────────────

    def on_enter(self):
        if self.results or self._loading:
            # Already loaded, or already searching — just redraw
            self.draw()
            return
        self.draw()
        if self.query:
            self._perform_search()

    def on_resize(self):
        self.app.t.query_size()
        self.draw()

    # ── Draw ───────────────────────────────────────────────────

    def draw(self):
        """Full redraw of the search screen."""
        self.clear_click_zones()
        self.app.t.clear_screen()
        self.app.ui.draw_title_bar("🔍  搜索")
        self._draw_input()
        self._draw_results()

    def _draw_input(self):
        """Draw the search input bar (text + underline, no box)."""
        t = self.app.t
        w = t.cols
        # Build display string
        cursor = "▊"
        prefix = "🔍 "
        display = f"{prefix}{self.query}{cursor}"
        max_vis = w - 8
        if len(display) > max_vis:
            display = "…" + display[-(max_vis - 1):]

        c1 = max(2, (w - max_vis) // 2)
        t.draw_text(SEARCH_ROW, c1, display, style=BOLD)
        # Underline the input area
        underline = "─" * min(len(display) + 2, w - c1 - 2)
        t.draw_text(SEARCH_ROW + 1, c1 - 1, underline, style=DIM)

        status = ""
        if self._loading:
            status = "  ⏳ 搜索中…"
        elif self._error:
            status = f"  ❌ {self._error}"
        elif self._result_count > 0:
            status = f"  ✅ {self._result_count} 部作品  ·  ↑↓选择  ·  Enter详情  ·  Esc返回"
        elif self.query:
            status = "  📭 没有找到结果"
        else:
            status = "  输入关键词搜索  ·  Esc返回首页"
        self.app.ui.draw_status_bar(status)

    def _draw_results(self):
        """Draw the scrollable results list."""
        if not self.results:
            return
        t = self.app.t
        w = t.cols
        avail = t.rows - 2 - RESULT_START_ROW
        thumb_block = RESULT_IMG_ROWS + 1  # image rows + spacer
        self._max_visible = max(0, min(
            len(self.results) - self._scroll_offset,
            avail // thumb_block,
        ))

        for vi in range(self._max_visible):
            ri = self._scroll_offset + vi
            v = self.results[ri]
            row = RESULT_START_ROW + vi * thumb_block

            is_sel = (ri == self._selected_idx)
            prefix = "▶" if is_sel else " "
            style = b"\x1b[1;33m" if is_sel else b""

            # Thumbnail placeholder region
            thumb_c1 = 3
            thumb_width = RESULT_THUMB_COLS

            # Draw placeholder border
            for r_off in range(RESULT_IMG_ROWS):
                t.fill_rect(row + r_off, thumb_c1, 1, thumb_width, " ")

            # Title + meta
            text_c = thumb_c1 + thumb_width + 2
            t.draw_text(row, text_c, f"{prefix} {v.title}",
                        style=style, max_width=w - text_c - 2)
            meta_parts = []
            if v.duration:
                meta_parts.append(f"⏱ {v.duration}")
            if v.likes:
                meta_parts.append(f"♥ {v.likes}")
            if v.views:
                meta_parts.append(f"👁 {v.views}")
            if v.author:
                meta_parts.append(f"✎ {v.author}")
            t.draw_text(row + 1, text_c + 2, " · ".join(meta_parts),
                        style=DIM, max_width=w - text_c - 4)

            # Click zone on thumbnail + text area
            self.add_click_zone(
                row, row + RESULT_IMG_ROWS - 1,
                thumb_c1, w - 2,
                "_on_result_click", ri,
            )

            # Progressive thumbnail load
            if v.thumbnail:
                thumb_url = v.thumbnail
                self.app.run_in_background(
                    lambda u=thumb_url: download_thumbnail(u),
                    lambda data, r=row, c=thumb_c1, u=thumb_url:
                        self._on_thumb(data, r, c, u),
                )

        # Scroll indicators
        if self._scroll_offset > 0:
            t.draw_text(RESULT_START_ROW - 1, 2,
                        f"⬆ {self._scroll_offset} more above", style=DIM)
        remaining = len(self.results) - self._scroll_offset - self._max_visible
        if remaining > 0:
            last_row = RESULT_START_ROW + self._max_visible * thumb_block
            if last_row < t.rows - 2:
                t.draw_text(last_row, 2, f"⬇ {remaining} more below", style=DIM)

    def _on_thumb(self, img_data: bytes | None, row: int, col: int, url: str):
        """Display a downloaded search-result thumbnail."""
        if img_data:
            self.app.display_thumb(img_data, row, col, RESULT_THUMB_COLS, url=url)

    # ── Search ─────────────────────────────────────────────────

    def _perform_search(self):
        """Kick off a background search for the current query."""
        if not self.query.strip():
            return
        self._loading = True
        self._error = ""
        self.results = []
        self._selected_idx = 0
        self._scroll_offset = 0
        self.draw()
        q = self.query.strip()
        self.app.run_in_background(
            lambda: search_videos(q),
            self._on_results,
        )

    def _on_results(self, results: list[VideoResult]):
        self._loading = False
        if results is None:
            self._error = "Search request failed"
            results = []
        self.results = results
        self._result_count = len(results)
        self._selected_idx = 0
        self._scroll_offset = 0
        self.draw()

    # ── Event handlers ─────────────────────────────────────────

    def on_key(self, ev: dict) -> bool:
        if ev.get("event") != "press":
            return False
        code = ev["key_code"]
        mod = ev.get("modifiers", 0)

        if code == 27:  # Esc
            self.app.pop_screen()
            return True
        elif code == 13:  # Enter
            if self.results and 0 <= self._selected_idx < len(self.results):
                self._on_result_click(self._selected_idx)
            else:
                self._perform_search()
            return True
        elif code == 65 and (mod & 1):  # Shift+Up (prior page)
            self._scroll_offset = max(0, self._scroll_offset - self._max_visible)
            self._selected_idx = max(0, self._selected_idx - self._max_visible)
            self.draw()
            return True
        elif code == 66 and (mod & 1):  # Shift+Down (next page)
            if self.results:
                max_off = max(0, len(self.results) - self._max_visible)
                self._scroll_offset = min(max_off, self._scroll_offset + self._max_visible)
                self._selected_idx = min(
                    len(self.results) - 1,
                    self._selected_idx + self._max_visible,
                )
            self.draw()
            return True
        elif code == 65:  # Up arrow
            if self._selected_idx > 0:
                self._selected_idx -= 1
                if self._selected_idx < self._scroll_offset:
                    self._scroll_offset = self._selected_idx
            self.draw()
            return True
        elif code == 66:  # Down arrow
            if self.results and self._selected_idx < len(self.results) - 1:
                self._selected_idx += 1
                if self._selected_idx >= self._scroll_offset + self._max_visible:
                    self._scroll_offset += 1
            self.draw()
            return True
        return False

    def on_mouse(self, ev: dict) -> bool:
        action = ev.get("action", "")
        # Left-click: delegate to base for click-zone matching
        if action == "press" and ev.get("button") == 0:
            if super().on_mouse(ev):
                return True

        # Wheel scrolling
        if action == "wheel":
            btn = ev.get("button", 0)  # 0=up, 1=down
            if btn == 0 and self._scroll_offset > 0:
                self._scroll_offset -= 1
                self.draw()
            elif btn == 1 and self.results:
                max_off = max(0, len(self.results) - self._max_visible)
                if self._scroll_offset < max_off:
                    self._scroll_offset += 1
                    self.draw()
            return True
        return False

    def on_text(self, ev: dict) -> bool:
        text = ev.get("text", "")
        if text in ("\x08", "\x7f"):  # Backspace
            if self.query:
                self.query = self.query[:-1]
        elif text == "\t":  # Tab — ignore
            return True
        elif text.isprintable():
            self.query += text
        else:
            return False  # ignore other control chars (Enter, Esc, etc.)
        self.draw()
        return True

    def _on_result_click(self, idx: int):
        """Navigate to detail for the selected result."""
        if 0 <= idx < len(self.results):
            v = self.results[idx]
            if v.url:
                self.app.push_screen(VideoDetailScreen(self.app, v.url))


# ═══════════════════════════════════════════════════════════════════════
# Section 6: VideoDetailScreen
# ═══════════════════════════════════════════════════════════════════════

class VideoDetailScreen(Screen):
    """Video detail — poster, sources, tags, metadata."""

    def __init__(self, app: App, video_url: str):
        super().__init__(app)
        self.video_url = video_url
        self.detail: VideoDetail | None = None
        self._loading = True
        self._error = ""
        self._poster_img_id = 0
        self._tag_row_start = 0

    # ── Lifecycle ──────────────────────────────────────────────

    def on_enter(self):
        self._loading = True
        self._error = ""
        self._draw_loading()
        self.app.run_in_background(
            lambda: extract_video_detail(self.video_url),
            self._on_data_loaded,
        )

    def on_resize(self):
        self.app.t.query_size()
        self.draw()

    # ── Data callback ──────────────────────────────────────────

    def _on_data_loaded(self, detail: VideoDetail | None):
        self._loading = False
        if detail is None:
            self._error = "Failed to load video detail"
        else:
            self.detail = detail
        self.draw()

    # ── Draw ───────────────────────────────────────────────────

    def _draw_loading(self):
        self.app.t.clear_screen()
        self.app.ui.draw_title_bar("🎬  Video Detail")
        self.app.ui.draw_status_bar("  Esc 返回  ·  q 退出")
        self.app.ui.draw_centered_text("⏳ Loading video detail…")

    def draw(self):
        """Full redraw of the detail screen."""
        self.clear_click_zones()
        self.app.t.clear_screen()

        if self._loading:
            self._draw_loading()
            return
        if self._error:
            self.app.ui.draw_title_bar("🎬  Error")
            self.app.ui.draw_status_bar("  Esc 返回")
            self.app.ui.draw_centered_text(f"❌ {self._error}", style=WARN_COLOUR)
            return
        if not self.detail:
            return

        d = self.detail
        t = self.app.t
        w = t.cols

        # Title bar with video title
        title_short = d.title[:w - 20] if d.title else "Untitled"
        self.app.ui.draw_title_bar(f"🎬  {title_short}")
        self.app.ui.draw_status_bar(
            "  Esc 返回  ·  点击海报→打开1080p  ·  点击标签→搜索  ·  q 退出"
        )

        # ── Poster ──────────────────────────────────────────
        poster_row = 2
        poster_cols = min(POSTER_COLS, w - 6)
        poster_rows = max(8, int(poster_cols * 9 / 16))  # estimate 16:9

        # Placeholder
        t.fill_rect(poster_row, 2, poster_rows, poster_cols + 1, "░")
        t.draw_text(poster_row + poster_rows // 2 - 1, 2 + poster_cols // 2 - 4,
                    "LOADING…", style=DIM)

        # Click zone on poster → opens best URL
        best_url = d.sources[0][1] if d.sources else ""
        self.add_click_zone(
            poster_row, poster_row + poster_rows - 1,
            2, 2 + poster_cols,
            "_on_poster_click", best_url,
        )

        # Download poster in background
        if d.poster:
            poster_url = d.poster
            self.app.run_in_background(
                lambda: download_thumbnail(poster_url),
                lambda data, pr=poster_row, pc=poster_cols, pu=poster_url:
                    self._on_poster(data, pr, pc, pu),
            )

        # ── Text metadata column (right of poster) ──────────
        text_col = 2 + poster_cols + 3
        if text_col < w - 10:
            tr = poster_row + 1
            if d.duration:
                t.draw_text(tr, text_col, f"⏱ {d.duration}", style=BOLD)
                tr += 1
            if d.views:
                t.draw_text(tr, text_col, f"👁 {d.views}", style=DIM)
                tr += 1
            if d.likes:
                t.draw_text(tr, text_col, f"♥ {d.likes}", style=DIM)
                tr += 1
            if d.author:
                t.draw_text(tr, text_col, f"✎ {d.author}", style=DIM)
                tr += 1

        # ── Sources ─────────────────────────────────────────
        src_row = poster_row + poster_rows + 1
        t.draw_text(src_row, 2, "📥 视频源:", style=BOLD)
        src_row += 1

        for si, (res, url) in enumerate(d.sources):
            is_best = (si == 0)
            label = f"{'⭐ ' if is_best else '   '}[{res}] {url}"
            style = SOURCE_COLOUR if is_best else DIM
            # Truncate URL for display
            display = label
            if len(display) > w - 4:
                # Keep URL visible but truncated
                prefix = f"{'⭐ ' if is_best else '   '}[{res}] "
                remain = w - 4 - len(prefix)
                url_short = url[:max(0, remain - 3)] + "…" if remain > 10 else url[:remain]
                display = prefix + url_short

            t.draw_text(src_row, 4, display, style=style, max_width=w - 6)
            # Click zone on source row
            self.add_click_zone(src_row, src_row, 4, w - 2,
                               "_on_source_click", url)
            src_row += 1

        # ── Page URL ────────────────────────────────────────
        src_row += 1
        t.draw_text(src_row, 2, "🔗 页面:", style=BOLD)
        page_display = d.page_url
        if len(page_display) > w - 10:
            page_display = page_display[:w - 13] + "…"
        t.draw_text(src_row, 10, page_display, style=DIM, max_width=w - 12)
        # Click zone on page URL
        self.add_click_zone(src_row, src_row, 10, w - 2,
                           "_on_source_click", d.page_url)
        src_row += 2

        # ── Tags ────────────────────────────────────────────
        if d.tags:
            t.draw_text(src_row, 2, "🏷 标签:", style=BOLD)
            self._tag_row_start = src_row
            src_row += 1

            tag_col = 4
            max_tag_col = w - 4
            for tag_text, search_param in d.tags:
                display = f" {tag_text} "
                tag_width = len(display)
                if tag_col + tag_width > max_tag_col:
                    src_row += 1
                    tag_col = 4
                t.draw_text(src_row, tag_col, display, style=TAG_COLOUR)
                # Click zone on each tag
                self.add_click_zone(
                    src_row, src_row,
                    tag_col, tag_col + tag_width,
                    "_on_tag_click", search_param,
                )
                tag_col += tag_width + 1  # gap between tags

    def _on_poster(self, img_data: bytes | None, row: int, img_cols: int, url: str):
        """Display the downloaded poster image."""
        if img_data:
            self.app.display_thumb(img_data, row, 2, img_cols, url=url)

    # ── Event handlers ─────────────────────────────────────────

    def on_key(self, ev: dict) -> bool:
        if ev.get("event") != "press":
            return False
        code = ev["key_code"]
        if code == 27:  # Esc
            self.app.pop_screen()
            return True
        return False

    def _on_poster_click(self, url: str):
        """Open the poster's best video source URL via xdg-open."""
        if url:
            self._xdg_open(url)

    def _on_source_click(self, url: str):
        """Open a source URL via xdg-open."""
        if url:
            self._xdg_open(url)

    def _on_tag_click(self, search_param: str):
        """Navigate to SearchScreen for the clicked tag."""
        if not search_param:
            return
        # Resolve search_param:
        #   - hashtag: plain query string (e.g. "絕區零")
        #   - category tag: full URL with tags%5B%5D=
        #   - genre: "genre:genre_name"
        if search_param.startswith("genre:"):
            genre = search_param[6:]
            self.app.push_screen(SearchScreen(self.app, initial_query=genre))
            return

        if search_param.startswith("http"):
            # Category tag — extract the tag from the URL
            parsed = urlparse(search_param)
            qs = parse_qs(parsed.query)
            tag_vals = qs.get("tags[]", [])
            if tag_vals:
                self.app.push_screen(SearchScreen(self.app, initial_query=tag_vals[0]))
                return
            self.app.push_screen(SearchScreen(self.app, initial_query=search_param))
            return

        # Plain text hashtag query
        self.app.push_screen(SearchScreen(self.app, initial_query=search_param))

    @staticmethod
    def _xdg_open(url: str):
        """Open *url* with the default system handler."""
        try:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Section 7: main()
# ═══════════════════════════════════════════════════════════════════════

def main():
    """Entry point — create and run the application."""
    initial_query = ""
    if len(sys.argv) > 1:
        initial_query = " ".join(sys.argv[1:])

    app = App()
    app.run(initial_query=initial_query)


if __name__ == "__main__":
    main()
