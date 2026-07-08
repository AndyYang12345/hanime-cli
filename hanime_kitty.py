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

# Layout anchors (all other positions are computed dynamically from terminal size)
SEARCH_ROW = 1              # search-bar / search-hint row
BANNER_HEADER_ROW = 2       # "▼ 头版推荐" header
BANNER_SEP_ROW = 3          # separator below banner header
BANNER_IMG_ROW = 4          # banner image top
RESULT_START_ROW = 3        # first result row (after input + underline)

# Thumbnail size limits (actual size computed dynamically, clamped to these)
THUMB_COLS_MIN, THUMB_COLS_MAX = 6, 15
BANNER_COLS_MIN, BANNER_COLS_MAX = 20, 50
POSTER_COLS_MIN, POSTER_COLS_MAX = 20, 60
RESULT_THUMB_MIN, RESULT_THUMB_MAX = 8, 18

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
        # Two-tier thumbnail cache: URL → image_id (kitty), URL → PNG bytes (network)
        self._thumb_ids: dict[str, int] = {}       # URL → kitty image_id
        self._thumb_data: dict[str, bytes] = {}    # URL → PNG bytes (downloaded once)

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

    def display_thumb(self, img_data: bytes, row: int, col: int,
                      img_cols: int, url: str = "") -> int:
        """Display a thumbnail at (row,col), caching both image_id and PNG data.

        The image is placed with z_index=-1 so text drawn on top remains visible.
        Returns the kitty image_id.
        """
        image_id = self._thumb_ids.get(url, 0) if url else 0
        image_id = self.gfx.display_image(
            img_data, row=row, col=col, image_id=image_id,
            img_cols=img_cols, quiet=True,
        )
        if url:
            self._thumb_ids[url] = image_id
            self._thumb_data[url] = img_data  # cache PNG bytes for instant redraw
        return image_id

    def has_thumb_data(self, url: str) -> bool:
        """Check if PNG data for *url* is already downloaded and cached."""
        return url in self._thumb_data

    def thumb_rows(self, img_cols: int) -> int:
        """Estimate cell rows a thumbnail will occupy at the given column width.

        Assumes a 16:9 aspect ratio for video thumbnails/posters.
        """
        t = self.t
        if t.cell_h > 0 and t.cell_w > 0:
            return max(2, int(img_cols * t.cell_w * 9 / 16 / t.cell_h))
        return max(2, img_cols // 2)  # fallback


# ═══════════════════════════════════════════════════════════════════════
# Section 4: HomeScreen
# ═══════════════════════════════════════════════════════════════════════

class HomeScreen(Screen):
    """Homepage — banner, category rows, search entry.

    Layout adapts to terminal size.  Category rows are drawn inside a scroll
    region so that wheel/arrow scrolling uses ANSI scroll commands (CSI S / T)
    to shift content without re-transmitting Kitty Graphics images.
    """

    def __init__(self, app: App):
        super().__init__(app)
        self.data: HomePageData | None = None
        self._loading = True
        self._error: str = ""
        self._scroll_offset = 0          # first visible category index
        self._max_visible_cats = 0       # computed on draw
        self._last_cat_block = 0         # cached cat_block for scroll calcs

    # ── Dynamic layout properties (recalculated on every draw/resize) ──

    @property
    def thumb_cols(self) -> int:
        """Thumbnail width in cells, based on terminal width."""
        w = self.app.t.cols
        thumbs_per_row = max(5, min(10, (w - 4) // 12))
        return max(THUMB_COLS_MIN, min(THUMB_COLS_MAX, (w - 4) // thumbs_per_row - 1))

    @property
    def banner_cols(self) -> int:
        """Banner width in cells."""
        w = self.app.t.cols
        return max(BANNER_COLS_MIN, min(BANNER_COLS_MAX, (w - 30) * 2 // 3))

    @property
    def banner_rows(self) -> int:
        """Banner height, capped so categories still have room in small terminals."""
        br = self.app.thumb_rows(self.banner_cols)
        # Leave at least 8 rows below the banner for categories + status bar
        max_br = max(3, self.app.t.rows - BANNER_IMG_ROW - 8)
        return min(br, max_br)

    @property
    def thumb_rows(self) -> int:
        return self.app.thumb_rows(self.thumb_cols)

    @property
    def cat_start_row(self) -> int:
        """First category row (below banner + spacer). Guaranteed to leave room."""
        csr = BANNER_IMG_ROW + self.banner_rows + 2
        # Never push categories past the bottom margin
        return min(csr, self.app.t.rows - 4)

    @property
    def cat_block(self) -> int:
        """Rows per category: header(1) + image rows + title row(1)."""
        return self.thumb_rows + 2

    @property
    def max_visible_cats(self) -> int:
        """How many categories fit in the content area."""
        avail = (self.app.t.rows - 2) - self.cat_start_row  # -2 leaves room for status bar + margin
        return max(1, avail // self.cat_block)

    # ── Lifecycle ──────────────────────────────────────────────

    def on_enter(self):
        if self.data is not None:
            self.draw()
            return
        self._loading = True
        self._error = ""
        self.draw()
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
        self._scroll_offset = 0
        self.draw()
        self._load_thumbnails()

    def _load_thumbnails(self):
        """Download uncached category thumbnails progressively."""
        if not self.data:
            return
        jobs: list[tuple[str, str]] = []
        for cat in self.data.categories:
            for v in cat.videos:
                if v.thumbnail and not self.app.has_thumb_data(v.thumbnail):
                    jobs.append((v.thumbnail, v.url))
        if not jobs:
            return

        def _download_sequentially():
            for thumb_url, video_url in jobs:
                data = download_thumbnail(thumb_url)
                self.app._job_queue.put(
                    (self._on_single_thumb, (thumb_url, video_url, data))
                )

        self.app.run_in_background(_download_sequentially, lambda _: None)

    def _on_single_thumb(self, result: tuple):
        """Display a single downloaded thumbnail (called progressively)."""
        if self is not self.app.current_screen:
            return
        thumb_url, video_url, img_data = result
        if not img_data:
            return
        self.app._thumb_data[thumb_url] = img_data
        self._redraw_single_thumb(video_url, img_data)

    def _redraw_single_thumb(self, video_url: str, img_data: bytes):
        """Display one thumbnail at its current layout position."""
        if not self.data:
            return
        tc = self.thumb_cols
        cb = self.cat_block
        for ci, cat in enumerate(self.data.categories):
            if ci < self._scroll_offset:
                continue
            vis_idx = ci - self._scroll_offset
            if vis_idx >= self._max_visible_cats:
                break
            cat_row = self.cat_start_row + vis_idx * cb
            img_row = cat_row + 1
            for vi, v in enumerate(cat.videos):
                if v.url != video_url:
                    continue
                col = 2 + vi * (tc + 1)
                if col + tc <= self.app.t.cols - 2:
                    self.app.display_thumb(img_data, img_row, col, tc, url=v.thumbnail)
                return

    # ═══════════════════════════════════════════════════════════
    # Full draw (first render, resize, navigation back)
    # ═══════════════════════════════════════════════════════════

    def draw(self):
        """Full redraw of the homepage."""
        self.clear_click_zones()
        self.app.t.clear_screen()
        self.app.t.reset_scroll_region()
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
        self._draw_all_categories()

    def _draw_bars(self):
        """Title bar (with embedded search hint) + status bar."""
        t = self.app.t
        w = t.cols
        left = " 🏠  Hanime1.me"
        right = "🔍 /搜索 "
        bar = left + " " * max(1, w - len(left) - len(right)) + right
        t.draw_text(0, 0, bar[:w], style=b"\x1b[7m")
        self.add_click_zone(0, 0, w - len(right) - 1, w - 1, "_on_search_click", None)
        self.app.ui.draw_status_bar(
            "  ⬆⬇ scroll  ·  Enter 详情  ·  / 搜索  ·  q 退出"
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

        # Section header + separator
        t.draw_text(BANNER_HEADER_ROW, 2, "▼ 头版推荐", style=BOLD)
        self.app.ui.draw_horizontal_separator(BANNER_SEP_ROW, 2, w - 2)

        bc = self.banner_cols
        br = self.banner_rows
        img_r1 = BANNER_IMG_ROW
        img_r2 = img_r1 + br - 1

        # Text on the right
        text_col = 4 + bc
        text_r = img_r1
        if banner.title:
            t.draw_text(text_r, text_col, banner.title, style=BOLD, max_width=w - text_col - 2)
            text_r += 2
        if banner.author:
            t.draw_text(text_r, text_col, banner.author, style=DIM, max_width=w - text_col - 2)
            text_r += 1
        if self.data.banner_tags:
            tags_text = " · ".join(self.data.banner_tags[:8])
            t.draw_text(text_r, text_col, tags_text, style=ACCENT, max_width=w - text_col - 2)

        # Banner image
        if banner.thumbnail:
            thumb_url = banner.thumbnail
            if self.app.has_thumb_data(thumb_url):
                self.app.display_thumb(
                    self.app._thumb_data[thumb_url], img_r1, 2, bc, url=thumb_url)
            else:
                t.fill_rect(img_r1, 2, br, bc + 1, "░")
                self.app.run_in_background(
                    lambda u=thumb_url: download_thumbnail(u),
                    lambda data, r=img_r1, b=bc, u=thumb_url:
                        self._on_banner_img(data, u, r, b),
                )
            self.add_click_zone(img_r1, img_r2, 2, 2 + bc, "_on_video_click", banner.url)

    def _on_banner_img(self, img_data: bytes | None, url: str, row: int, img_cols: int):
        if self is not self.app.current_screen:
            return
        if img_data:
            self.app.display_thumb(img_data, row, 2, img_cols, url=url)

    # ═══════════════════════════════════════════════════════════
    # Category drawing (used by both full draw & incremental scroll)
    # ═══════════════════════════════════════════════════════════

    def _draw_all_categories(self):
        """Draw all visible category slots (full redraw path)."""
        if not self.data:
            return
        cats = self.data.categories
        if not cats:
            return

        self._max_visible_cats = min(
            len(cats) - self._scroll_offset, self.max_visible_cats)
        self._last_cat_block = self.cat_block

        for slot in range(self._max_visible_cats):
            ci = self._scroll_offset + slot
            self._draw_one_category(ci, slot)

        self._draw_scroll_indicators()

    def _draw_one_category(self, ci: int, slot: int):
        """Draw a single category at the given visual *slot* (0 = first visible).

        Called both from full-redraw and incremental-scroll paths.
        """
        if not self.data:
            return
        cats = self.data.categories
        if ci < 0 or ci >= len(cats):
            return
        cat = cats[ci]
        t = self.app.t
        w = t.cols
        tc = self.thumb_cols
        cb = self.cat_block
        cat_row = self.cat_start_row + slot * cb

        # ── Header ──
        header = f"▼ {cat.name} ({len(cat.videos)}部)"
        t.draw_text(cat_row, 2, header, style=BOLD)

        # ── Thumbnails ──
        max_thumbs = min(len(cat.videos), (w - 4) // (tc + 1))
        img_row = cat_row + 1
        title_row = img_row + self.thumb_rows

        # Clear the image + title area for this category first
        t.clear_rect(img_row, 2, self.thumb_rows + 1, w - 2)

        for ti in range(max_thumbs):
            v = cat.videos[ti]
            col = 2 + ti * (tc + 1)
            thumb_url = v.thumbnail

            if thumb_url and self.app.has_thumb_data(thumb_url):
                self.app.display_thumb(
                    self.app._thumb_data[thumb_url], img_row, col, tc, url=thumb_url)
            else:
                # Placeholder — empty rect (no text overlay to avoid covering image)
                t.fill_rect(img_row, col, self.thumb_rows, tc, " ")

            # ── Title below thumbnail ──
            title = v.title[:tc] if v.title else ""
            t.draw_text(title_row, col, title, style=DIM, max_width=tc)

            # ── Click zone ──
            self.add_click_zone(
                img_row, img_row + self.thumb_rows - 1,
                col, col + tc,
                "_on_video_click", v.url,
            )

    def _draw_scroll_indicators(self):
        """Draw scroll hints in the gap between content and status bar."""
        if not self.data:
            return
        t = self.app.t
        cats = self.data.categories
        row = self.app.t.rows - 2  # just above status bar

        if self._scroll_offset > 0:
            t.draw_text(row, 2, f"⬆ {self._scroll_offset} more above", style=DIM)
        remaining = len(cats) - self._scroll_offset - self._max_visible_cats
        if remaining > 0:
            t.draw_text(row, 2, f"⬇ {remaining} more below  (scroll / PgUp PgDn)", style=DIM)

    # ═══════════════════════════════════════════════════════════
    # Incremental scroll (uses ANSI scroll regions — CSI S / CSI T)
    # ═══════════════════════════════════════════════════════════

    def _scroll_categories(self, new_offset: int):
        """Scroll to *new_offset*, only redrawing newly-exposed edges.

        Uses terminal scroll regions so that existing Kitty Graphics images
        move with the text — no image re-transmission needed for middle rows.
        Falls back to full redraw when the delta is too large.
        """
        old_offset = self._scroll_offset
        if new_offset == old_offset:
            return

        if not self.data:
            return
        cats = self.data.categories
        max_possible = max(0, len(cats) - self._max_visible_cats)
        new_offset = max(0, min(new_offset, max_possible))

        delta = new_offset - old_offset
        self._scroll_offset = new_offset

        cb = self._last_cat_block or self.cat_block
        content_end = self.app.t.rows - 2

        # Fall back to full redraw if delta is too large or layout changed
        if abs(delta) >= self._max_visible_cats or cb != self.cat_block:
            self.draw()
            return

        self.clear_click_zones()
        self.app.t.set_scroll_region(self.cat_start_row, content_end)

        if delta > 0:
            # Scrolling down — content moves UP
            self.app.t.scroll_up(delta * cb)
            # Draw new categories at the bottom
            for i in range(delta):
                slot = self._max_visible_cats - delta + i
                ci = new_offset + slot
                if slot < self._max_visible_cats and ci < len(cats):
                    self._draw_one_category(ci, slot)
        else:
            # Scrolling up — content moves DOWN
            self.app.t.scroll_down((-delta) * cb)
            # Draw new categories at the top
            for i in range(-delta):
                ci = new_offset + i
                if ci < len(cats):
                    self._draw_one_category(ci, i)

        self.app.t.reset_scroll_region()

        # Rebuild click zones for all visible categories (cheap in-memory op)
        self._rebuild_click_zones()
        self._draw_scroll_indicators()

    def _rebuild_click_zones(self):
        """Re-register click zones for all currently visible categories.

        Called after incremental scroll because scroll regions shift cell
        positions but click-zone data is stale.
        """
        if not self.data:
            return
        cats = self.data.categories
        tc = self.thumb_cols
        cb = self._last_cat_block or self.cat_block

        for slot in range(self._max_visible_cats):
            ci = self._scroll_offset + slot
            if ci >= len(cats):
                continue
            cat = cats[ci]
            cat_row = self.cat_start_row + slot * cb
            img_row = cat_row + 1
            max_thumbs = min(len(cat.videos), (self.app.t.cols - 4) // (tc + 1))
            for ti in range(max_thumbs):
                v = cat.videos[ti]
                col = 2 + ti * (tc + 1)
                self.add_click_zone(
                    img_row, img_row + self.thumb_rows - 1,
                    col, col + tc,
                    "_on_video_click", v.url,
                )

    # ── Event handlers ─────────────────────────────────────────

    def on_key(self, ev: dict) -> bool:
        if ev.get("event") != "press":
            return False
        code = ev["key_code"]
        mod = ev.get("modifiers", 0)

        if code in (13, 47, 115):  # Enter, '/', 's' → search
            self._on_search_click(None)
            return True
        elif code == 27:  # Esc
            self.app._running = False
            return True
        elif code == 65 and (mod & 1):  # Shift+Up (page up)
            self._scroll_categories(self._scroll_offset - self._max_visible_cats)
            return True
        elif code == 66 and (mod & 1):  # Shift+Down (page down)
            self._scroll_categories(self._scroll_offset + self._max_visible_cats)
            return True
        elif code == 65:  # Up arrow (scroll up 1 category)
            self._scroll_categories(self._scroll_offset - 1)
            return True
        elif code == 66:  # Down arrow (scroll down 1 category)
            self._scroll_categories(self._scroll_offset + 1)
            return True
        return False

    def on_mouse(self, ev: dict) -> bool:
        action = ev.get("action", "")
        if action == "press" and ev.get("button") == 0:
            if super().on_mouse(ev):
                return True

        if action == "wheel":
            btn = ev.get("button", 0)  # 0=up, 1=down
            if btn == 0:
                self._scroll_categories(self._scroll_offset - 1)
            elif btn == 1:
                self._scroll_categories(self._scroll_offset + 1)
            return True
        return False

    def on_text(self, ev: dict) -> bool:
        text = ev.get("text", "")
        if text and text.isprintable():
            self._on_search_click(text)
            return True
        return False

    def _on_search_click(self, data):
        initial = data if isinstance(data, str) else ""
        self.app.push_screen(SearchScreen(self.app, initial_query=initial))

    def _on_video_click(self, url: str):
        if url:
            self.app.push_screen(VideoDetailScreen(self.app, url))


# ═══════════════════════════════════════════════════════════════════════
# Section 5: SearchScreen
# ═══════════════════════════════════════════════════════════════════════

class SearchScreen(Screen):
    """Search input + result list with thumbnails.

    Uses dynamic thumbnail sizing and scroll-region-based incremental
    scrolling for smooth result navigation.
    """

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
        self._last_thumb_cols = 0
        self._last_thumb_rows = 0

    # ── Dynamic layout ────────────────────────────────────────

    @property
    def result_thumb_cols(self) -> int:
        """Dynamic thumbnail width for search results."""
        w = self.app.t.cols
        return max(RESULT_THUMB_MIN, min(RESULT_THUMB_MAX, (w - 30) // 2))

    @property
    def result_thumb_rows(self) -> int:
        return self.app.thumb_rows(self.result_thumb_cols)

    @property
    def result_block(self) -> int:
        """Rows per result: image rows + spacer."""
        return self.result_thumb_rows + 1

    # ── Lifecycle ──────────────────────────────────────────────

    def on_enter(self):
        if self.results or self._loading:
            self.draw()
            return
        self.draw()
        if self.query:
            self._perform_search()

    def on_resize(self):
        self.app.t.query_size()
        self.draw()

    # ── Full Draw ──────────────────────────────────────────────

    def draw(self):
        """Full redraw of the search screen."""
        self.clear_click_zones()
        self.app.t.clear_screen()
        self.app.t.reset_scroll_region()
        self.app.ui.draw_title_bar("🔍  搜索")
        self._draw_input()
        self._draw_all_results()

    def _draw_input(self):
        """Draw the search input bar."""
        t = self.app.t
        w = t.cols
        cursor = "▊"
        prefix = "🔍 "
        display = f"{prefix}{self.query}{cursor}"
        max_vis = w - 8
        if len(display) > max_vis:
            display = "…" + display[-(max_vis - 1):]
        c1 = max(2, (w - max_vis) // 2)
        t.draw_text(SEARCH_ROW, c1, display, style=BOLD)
        underline = "─" * min(len(display) + 2, w - c1 - 2)
        t.draw_text(SEARCH_ROW + 1, c1 - 1, underline, style=DIM)

    def _draw_input_only(self):
        """Redraw only the input area + status bar (no results re-render)."""
        t = self.app.t
        w = t.cols
        t.clear_rect(SEARCH_ROW, 0, 3, w)
        self._draw_input()
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


    def _draw_all_results(self):
        """Draw all visible result slots (full redraw path)."""
        if not self.results:
            return
        t = self.app.t
        w = t.cols
        tr = self.result_thumb_rows
        tb = self.result_block
        avail = t.rows - 2 - RESULT_START_ROW
        self._max_visible = max(0, min(
            len(self.results) - self._scroll_offset, avail // tb))
        self._last_thumb_cols = self.result_thumb_cols
        self._last_thumb_rows = tr

        for slot in range(self._max_visible):
            ri = self._scroll_offset + slot
            self._draw_one_result(ri, slot)

        # Scroll indicators
        if self._scroll_offset > 0:
            t.draw_text(RESULT_START_ROW - 1, 2,
                        f"⬆ {self._scroll_offset} more above", style=DIM)
        remaining = len(self.results) - self._scroll_offset - self._max_visible
        if remaining > 0:
            last_row = RESULT_START_ROW + self._max_visible * tb
            if last_row < t.rows - 2:
                t.draw_text(last_row, 2, f"⬇ {remaining} more below", style=DIM)

    def _draw_one_result(self, ri: int, slot: int):
        """Draw a single search result at the given visual slot."""
        if ri < 0 or ri >= len(self.results):
            return
        v = self.results[ri]
        t = self.app.t
        w = t.cols
        tc = self.result_thumb_cols
        tr = self.result_thumb_rows
        tb = self.result_block
        row = RESULT_START_ROW + slot * tb

        is_sel = (ri == self._selected_idx)
        prefix = "▶" if is_sel else " "
        style = b"\x1b[1;33m" if is_sel else b""

        thumb_c1 = 3
        thumb_url = v.thumbnail

        # Clear result area
        t.clear_rect(row, thumb_c1, tr, w - thumb_c1 - 2)

        if thumb_url and self.app.has_thumb_data(thumb_url):
            self.app.display_thumb(
                self.app._thumb_data[thumb_url], row, thumb_c1, tc, url=thumb_url)
        else:
            t.fill_rect(row, thumb_c1, tr, tc, " ")
            if thumb_url:
                self.app.run_in_background(
                    lambda u=thumb_url: download_thumbnail(u),
                    lambda data, r=row, c=thumb_c1, u=thumb_url:
                        self._on_thumb(data, r, c, u),
                )

        # Title + meta (to the right of thumbnail)
        text_c = thumb_c1 + tc + 2
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

        # Click zone
        self.add_click_zone(
            row, row + tr - 1, thumb_c1, w - 2,
            "_on_result_click", ri,
        )

    def _on_thumb(self, img_data: bytes | None, row: int, col: int, url: str):
        if img_data:
            self.app.display_thumb(img_data, row, col, self.result_thumb_cols, url=url)

    def _update_selection(self, old_idx: int, new_idx: int):
        """Redraw only the two rows affected by a selection change."""
        if not self.results:
            return
        old_slot = old_idx - self._scroll_offset
        new_slot = new_idx - self._scroll_offset
        if 0 <= old_slot < self._max_visible:
            self._draw_one_result(old_idx, old_slot)
        if 0 <= new_slot < self._max_visible:
            self._draw_one_result(new_idx, new_slot)

    # ═══════════════════════════════════════════════════════════
    # Incremental scroll
    # ═══════════════════════════════════════════════════════════

    def _scroll_results(self, new_offset: int):
        """Scroll to *new_offset*, only redrawing newly-exposed edges."""
        old_offset = self._scroll_offset
        if new_offset == old_offset:
            return
        if not self.results:
            return

        max_possible = max(0, len(self.results) - self._max_visible)
        new_offset = max(0, min(new_offset, max_possible))
        delta = new_offset - old_offset
        self._scroll_offset = new_offset

        tb = self._last_thumb_rows + 1  # result block
        tc_now = self.result_thumb_cols
        tr_now = self.result_thumb_rows

        # Fall back to full redraw if layout changed or delta too large
        if abs(delta) >= self._max_visible or tc_now != self._last_thumb_cols:
            self.draw()
            return

        self.clear_click_zones()
        content_end = self.app.t.rows - 2
        self.app.t.set_scroll_region(RESULT_START_ROW, content_end)

        if delta > 0:
            self.app.t.scroll_up(delta * tb)
            for i in range(delta):
                slot = self._max_visible - delta + i
                ri = new_offset + slot
                if slot < self._max_visible and ri < len(self.results):
                    self._draw_one_result(ri, slot)
        else:
            self.app.t.scroll_down((-delta) * tb)
            for i in range(-delta):
                ri = new_offset + i
                if ri < len(self.results):
                    self._draw_one_result(ri, i)

        self.app.t.reset_scroll_region()
        self._rebuild_result_click_zones()
        self._draw_scroll_indicators()

    def _rebuild_result_click_zones(self):
        """Re-register click zones after incremental scroll."""
        if not self.results:
            return
        tc = self._last_thumb_cols
        tr = self._last_thumb_rows
        tb = tr + 1
        w = self.app.t.cols
        for slot in range(self._max_visible):
            ri = self._scroll_offset + slot
            if ri >= len(self.results):
                continue
            row = RESULT_START_ROW + slot * tb
            self.add_click_zone(
                row, row + tr - 1, 3, w - 2,
                "_on_result_click", ri,
            )

    def _draw_scroll_indicators(self):
        """Redraw scroll hints after incremental scroll."""
        t = self.app.t
        tb = self._last_thumb_rows + 1
        row = t.rows - 2
        t.draw_text(row, 2, " " * (t.cols - 4))  # clear
        if self._scroll_offset > 0:
            t.draw_text(row, 2, f"⬆ {self._scroll_offset} more above", style=DIM)
        remaining = len(self.results) - self._scroll_offset - self._max_visible
        if remaining > 0:
            t.draw_text(row, 2, f"⬇ {remaining} more below", style=DIM)

    # ── Search ─────────────────────────────────────────────────

    def _perform_search(self):
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
        elif code == 65 and (mod & 1):  # Shift+Up
            self._selected_idx = max(0, self._selected_idx - self._max_visible)
            self._scroll_results(self._scroll_offset - self._max_visible)
            return True
        elif code == 66 and (mod & 1):  # Shift+Down
            if self.results:
                self._selected_idx = min(
                    len(self.results) - 1,
                    self._selected_idx + self._max_visible,
                )
                self._scroll_results(self._scroll_offset + self._max_visible)
            return True
        elif code == 65:  # Up arrow
            if self._selected_idx > 0:
                old_idx = self._selected_idx
                self._selected_idx -= 1
                if self._selected_idx < self._scroll_offset:
                    self._scroll_results(self._selected_idx)
                else:
                    self._update_selection(old_idx, self._selected_idx)
            return True
        elif code == 66:  # Down arrow
            if self.results and self._selected_idx < len(self.results) - 1:
                old_idx = self._selected_idx
                self._selected_idx += 1
                if self._selected_idx >= self._scroll_offset + self._max_visible:
                    self._scroll_results(self._scroll_offset + 1)
                else:
                    self._update_selection(old_idx, self._selected_idx)
            return True
        return False

    def on_mouse(self, ev: dict) -> bool:
        action = ev.get("action", "")
        if action == "press" and ev.get("button") == 0:
            if super().on_mouse(ev):
                return True

        if action == "wheel":
            btn = ev.get("button", 0)
            if btn == 0 and self._scroll_offset > 0:
                self._scroll_results(self._scroll_offset - 1)
            elif btn == 1 and self.results:
                max_off = max(0, len(self.results) - self._max_visible)
                if self._scroll_offset < max_off:
                    self._scroll_results(self._scroll_offset + 1)
            return True
        return False

    def on_text(self, ev: dict) -> bool:
        text = ev.get("text", "")
        if text in ("\x08", "\x7f"):  # Backspace
            if self.query:
                self.query = self.query[:-1]
        elif text == "\t":
            return True
        elif text.isprintable():
            self.query += text
        else:
            return False
        self._draw_input_only()
        return True

    def _on_result_click(self, idx: int):
        if 0 <= idx < len(self.results):
            v = self.results[idx]
            if v.url:
                self.app.push_screen(VideoDetailScreen(self.app, v.url))


# ═══════════════════════════════════════════════════════════════════════
# Section 6: VideoDetailScreen
# ═══════════════════════════════════════════════════════════════════════

class VideoDetailScreen(Screen):
    """Video detail — poster, sources, tags, metadata.

    Poster and layout adapt to terminal size dynamically.
    """

    def __init__(self, app: App, video_url: str):
        super().__init__(app)
        self.video_url = video_url
        self.detail: VideoDetail | None = None
        self._loading = True
        self._error = ""

    # ── Dynamic layout ────────────────────────────────────────

    @property
    def poster_cols(self) -> int:
        """Poster width in cells, based on terminal width."""
        w = self.app.t.cols
        return max(POSTER_COLS_MIN, min(POSTER_COLS_MAX, (w - 30) * 2 // 3))

    @property
    def poster_rows(self) -> int:
        return self.app.thumb_rows(self.poster_cols)

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

        # Title bar
        title_short = d.title[:w - 20] if d.title else "Untitled"
        self.app.ui.draw_title_bar(f"🎬  {title_short}")
        self.app.ui.draw_status_bar(
            "  Esc 返回  ·  点击海报→打开1080p  ·  点击标签→搜索  ·  q 退出"
        )

        # ── Poster ──
        poster_row = 2
        pc = self.poster_cols
        pr = self.poster_rows

        best_url = d.sources[0][1] if d.sources else ""
        self.add_click_zone(
            poster_row, poster_row + pr - 1, 2, 2 + pc,
            "_on_poster_click", best_url,
        )

        poster_url = d.poster
        if poster_url and self.app.has_thumb_data(poster_url):
            self.app.display_thumb(
                self.app._thumb_data[poster_url], poster_row, 2, pc, url=poster_url)
        elif poster_url:
            t.fill_rect(poster_row, 2, pr, pc + 1, "░")
            self.app.run_in_background(
                lambda u=poster_url: download_thumbnail(u),
                lambda data, pr=poster_row, pc=pc, pu=poster_url:
                    self._on_poster(data, pr, pc, pu),
            )
        else:
            t.fill_rect(poster_row, 2, pr, pc + 1, " ")
            t.draw_text(poster_row + pr // 2, 2 + pc // 2 - 5, "No poster", style=DIM)

        # ── Text metadata column (right of poster) ──
        text_col = 2 + pc + 3
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

        # ── Sources ──
        src_row = poster_row + pr + 1
        t.draw_text(src_row, 2, "📥 视频源:", style=BOLD)
        src_row += 1

        for si, (res, url) in enumerate(d.sources):
            is_best = (si == 0)
            style = SOURCE_COLOUR if is_best else DIM
            prefix = f"{'⭐ ' if is_best else '   '}[{res}] "
            remain = w - 6 - len(prefix)
            url_short = url if len(url) <= remain else url[:remain - 1] + "…"
            t.draw_text(src_row, 4, prefix + url_short, style=style, max_width=w - 6)
            self.add_click_zone(src_row, src_row, 4, w - 2,
                               "_on_source_click", url)
            src_row += 1

        # ── Page URL ──
        src_row += 1
        t.draw_text(src_row, 2, "🔗 页面:", style=BOLD)
        page_display = d.page_url
        if len(page_display) > w - 10:
            page_display = page_display[:w - 13] + "…"
        t.draw_text(src_row, 10, page_display, style=DIM, max_width=w - 12)
        self.add_click_zone(src_row, src_row, 10, w - 2,
                           "_on_source_click", d.page_url)
        src_row += 2

        # ── Tags ──
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
                self.add_click_zone(
                    src_row, src_row, tag_col, tag_col + tag_width,
                    "_on_tag_click", search_param,
                )
                tag_col += tag_width + 1

    def _on_poster(self, img_data: bytes | None, row: int, img_cols: int, url: str):
        if not img_data:
            return
        poster_rows = self.app.thumb_rows(img_cols)
        self.app.t.fill_rect(row, 2, poster_rows, img_cols + 1, " ")
        self.app.display_thumb(img_data, row, 2, img_cols, url=url)

    # ── Event handlers ─────────────────────────────────────────

    def on_key(self, ev: dict) -> bool:
        if ev.get("event") != "press":
            return False
        if ev["key_code"] == 27:  # Esc
            self.app.pop_screen()
            return True
        return False

    def _on_poster_click(self, url: str):
        if url:
            self._xdg_open(url)

    def _on_source_click(self, url: str):
        if url:
            self._xdg_open(url)

    def _on_tag_click(self, search_param: str):
        if not search_param:
            return
        if search_param.startswith("genre:"):
            self.app.push_screen(SearchScreen(self.app, initial_query=search_param[6:]))
            return
        if search_param.startswith("http"):
            parsed = urlparse(search_param)
            qs = parse_qs(parsed.query)
            tag_vals = qs.get("tags[]", [])
            if tag_vals:
                self.app.push_screen(SearchScreen(self.app, initial_query=tag_vals[0]))
                return
            self.app.push_screen(SearchScreen(self.app, initial_query=search_param))
            return
        self.app.push_screen(SearchScreen(self.app, initial_query=search_param))

    @staticmethod
    def _xdg_open(url: str):
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
