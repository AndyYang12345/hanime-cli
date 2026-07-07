#!/usr/bin/env python3
"""
Kitty Native TUI — 概念验证 (Proof of Concept)

演示 Kitty 两大原生能力：
  1. Graphics Protocol  — 在终端内渲染真实像素图片
  2. Keyboard Protocol   — 完整的按键/鼠标事件监听 (press/release/repeat)

无需 Textual、无需 chafa、无需 Pillow。
仅依赖 stdlib + cloudscraper（下载缩略图）。

在 Kitty 终端中运行：
    python kitty_native_poc.py

按 's' 进入搜索模式，输入关键词搜索视频并显示缩略图。
按 'q' 或 Esc 退出。
"""

import sys
import os
import io
import re
import json
import struct
import base64
import fcntl
import termios
import tty
import select
import signal
import array
import argparse
from typing import Optional

# ── cloudscraper 用于绕过 Cloudflare ───────────────────────
try:
    import cloudscraper
    HAS_SCRAPER = True
except ImportError:
    HAS_SCRAPER = False

# ── 常量 ──────────────────────────────────────────────────
# Kitty Graphics Protocol
APC_START = b"\x1b_G"
APC_END = b"\x1b\\"
# Kitty Keyboard Protocol
KBD_ENABLE = b"\x1b[>1u"       # 基础：消除歧义
KBD_FULL = b"\x1b[>31u"        # 全部增强：事件类型 + alternate keys + text
KBD_DISABLE = b"\x1b[<u"
# SGR Mouse tracking
MOUSE_SGR = b"\x1b[?1006h"      # SGR 像素模式
MOUSE_TRACK = b"\x1b[?1003h"    # 跟踪所有移动
MOUSE_BTN = b"\x1b[?1000h"      # 按钮按下/释放
MOUSE_OFF = b"\x1b[?1000l\x1b[?1003l\x1b[?1006l"
# Screen
ALT_SCREEN = b"\x1b[?1049h"
MAIN_SCREEN = b"\x1b[?1049l"
CURSOR_HIDE = b"\x1b[?25l"
CURSOR_SHOW = b"\x1b[?25h"
CLEAR = b"\x1b[2J"
HOME = b"\x1b[H"
# Window size query
SIZE_QUERY = b"\x1b[14t"
# SGR helpers
RESET = b"\x1b[0m"
BOLD = b"\x1b[1m"
DIM = b"\x1b[2m"

# ANSI True Color helper
def rgb_fg(r, g, b):
    return f"\x1b[38;2;{r};{g};{b}m".encode()

def rgb_bg(r, g, b):
    return f"\x1b[48;2;{r};{g};{b}m".encode()

# ── 终端控制 ──────────────────────────────────────────────
class Terminal:
    """管理终端 raw mode、大小查询、screen buffer。"""

    def __init__(self):
        self.old_termios = None
        self.rows = 24
        self.cols = 80
        self.px_w = 800
        self.px_h = 600
        self.cell_w = 1
        self.cell_h = 1
        self._buf = sys.stdout.buffer

    def enter_raw(self):
        """进入 raw 模式 + alternate screen。"""
        self.old_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        self._send(ALT_SCREEN, CURSOR_HIDE, CLEAR, HOME)
        self.query_size()

    def exit_raw(self):
        """恢复终端设置。"""
        self._send(MOUSE_OFF, KBD_DISABLE, CURSOR_SHOW, MAIN_SCREEN)
        if self.old_termios:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_termios)

    def query_size(self):
        """用 TIOCGWINSZ 获取窗口像素尺寸。"""
        buf = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, buf)
        self.rows, self.cols, self.px_w, self.px_h = buf
        if self.cols > 0:
            self.cell_w = self.px_w // self.cols
            self.cell_h = self.px_h // self.rows

    def _send(self, *args: bytes):
        """写入 stdout 并 flush。"""
        for a in args:
            self._buf.write(a)
        self._buf.flush()

    def write(self, data: bytes):
        self._buf.write(data)
        self._buf.flush()

    def draw_text(self, row: int, col: int, text: str, style: bytes = b""):
        """在指定行列绘制文本。"""
        self._buf.write(
            f"\x1b[{row+1};{col+1}H".encode()
            + style
            + text.encode()
            + RESET
        )
        self._buf.flush()

    def clear_screen(self):
        self._send(CLEAR, HOME)

    def fill_rect(self, row: int, col: int, h: int, w: int, ch: str = " "):
        """用字符填充矩形区域。"""
        line = ch * w
        for i in range(h):
            self._buf.write(f"\x1b[{row+i+1};{col+1}H".encode() + line.encode())
        self._buf.flush()


# ── Kitty Graphics Protocol ────────────────────────────────
class KittyGraphics:
    """Kitty 原生图形协议：发送 PNG 图片并在终端任意位置渲染。"""

    def __init__(self, terminal: Terminal):
        self.t = terminal
        self._image_counter = 0

    def next_id(self) -> int:
        self._image_counter += 1
        return self._image_counter

    def display_image(
        self,
        data: bytes,
        row: int = 0,
        col: int = 0,
        width_cells: Optional[int] = None,
        height_cells: Optional[int] = None,
        image_id: int = 0,
        z_index: int = -1,  # 负值 = 文本下方（滚动）
    ) -> int:
        """
        发送 PNG 图片数据并在指定终端位置显示。

        返回 image_id（可用于后续删除/更新）。
        """
        if not image_id:
            image_id = self.next_id()

        # 控制参数
        controls = {
            "a": "T",           # action: Transmit + display
            "f": 100,           # format: PNG
            "i": image_id,
            "q": 2,             # quiet: suppress responses
            "z": z_index,       # z-index: negative = behind text
            "C": 1,             # cursor move: don't move cursor
        }

        # 传输方式：直接 (d) + 分块
        base64_data = standard_b64encode(data)

        # 构建控制字符串（仅首块包含）
        ctrl_parts = [f"{k}={v}" for k, v in controls.items()]
        ctrl_str = ",".join(ctrl_parts)

        # 分块发送（每块 ≤ 4096 bytes）
        chunk_size = 4096
        offset = 0
        while offset < len(base64_data):
            chunk = base64_data[offset:offset + chunk_size]
            offset += chunk_size
            if offset >= len(base64_data):
                m = 0  # 最后一块
            else:
                m = 1  # 还有后续块

            if m == 1:
                header = f"{ctrl_str},m={m}"
            else:
                header = f"{ctrl_str},m={m}"

            self.t._send(
                APC_START + header.encode("ascii") + b";" + chunk + APC_END
            )

        return image_id

    def delete_image(self, image_id: int):
        """删除指定 image_id 的图像及其所有 placements。"""
        controls = f"a=d,d=I,i={image_id},q=2"
        self.t._send(
            APC_START + controls.encode("ascii") + b";" + APC_END
        )

    def display_image_file(self, path: str, **kwargs) -> int:
        """从文件读取 PNG 并显示。"""
        with open(path, "rb") as f:
            return self.display_image(f.read(), **kwargs)

    def display_image_url(self, url: str, **kwargs) -> Optional[int]:
        """下载 URL 指向的图片并显示。"""
        if not HAS_SCRAPER:
            return None
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        try:
            resp = scraper.get(url, timeout=10)
            if resp.status_code == 200:
                return self.display_image(resp.content, **kwargs)
        except Exception as e:
            pass
        return None


# ── Kitty Keyboard Protocol ───────────────────────────────
class KeyboardReader:
    """
    读取 Kitty 增强键盘协议 + SGR Mouse 事件。

    事件类型:
      - 'key': 按键事件 (press/repeat/release)
      - 'mouse': 鼠标事件
      - 'text': 纯文本输入
      - 'resize': 终端大小变化
    """

    def __init__(self, terminal: Terminal):
        self.t = terminal
        self.stdin = sys.stdin.buffer

    def enable(self):
        """启用 Kitty 键盘协议 + SGR 鼠标。"""
        self.t._send(KBD_FULL, MOUSE_SGR, MOUSE_BTN, MOUSE_TRACK)

    def disable(self):
        """恢复默认。"""
        self.t._send(MOUSE_OFF, KBD_DISABLE)

    def read_event(self, timeout: float = 0.05) -> Optional[dict]:
        """
        阻塞读取一个终端输入事件。

        返回 dict 或 None（无输入时）。
        """
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None

        data = self.stdin.read1(256)
        if not data:
            return None

        return self._parse(data)

    def read_events(self, timeout: float = 0.01) -> list[dict]:
        """读取所有待处理的事件。"""
        events = []
        # 尝试读取多轮直到无数据
        for _ in range(10):
            ev = self.read_event(timeout=timeout)
            if ev:
                events.append(ev)
            else:
                break
        return events

    def _parse(self, data: bytes) -> Optional[dict]:
        if not data:
            return None

        # ── CSI 序列（ESC [ ...） ──────────────────────────
        if data.startswith(b"\x1b["):
            rest = data[2:]

            # Kitty 键盘协议：CSI number ; modifiers [u~]
            m = re.match(
                rb"([0-9:;]+)\s*([u~])",
                rest,
            )
            if m:
                codes_str = m.group(1).decode("ascii")
                terminator = m.group(2).decode("ascii")

                # 解析字段
                parts = codes_str.split(";")
                kp = parts[0].split(":")  # key parts: unicode[:shifted[:base]]
                key_code = int(kp[0]) if kp[0] else 0
                shifted = int(kp[1]) if len(kp) > 1 and kp[1] else 0
                base_key = int(kp[2]) if len(kp) > 2 and kp[2] else 0

                modifiers_raw = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                # modifiers = 1 + bitfield
                ev_type = 1  # default press
                if ":" in parts[1] if len(parts) > 1 else False:
                    mp = parts[1].split(":")
                    modifiers_raw = int(mp[0]) if mp[0] else 1
                    ev_type = int(mp[1]) if len(mp) > 1 and mp[1] else 1

                mod_bits = modifiers_raw - 1

                return {
                    "type": "key",
                    "key_code": key_code,
                    "shifted": shifted,
                    "base_key": base_key,
                    "modifiers": mod_bits,
                    "event": {1: "press", 2: "repeat", 3: "release"}.get(ev_type, "press"),
                    "terminator": terminator,
                    "raw": data,
                }

            # ── SGR Mouse：CSI < button ; col ; row M/m ─────
            mouse_match = re.match(
                rb"<(\d+);(\d+);(\d+)([Mm])",
                rest,
            )
            if mouse_match:
                btn_raw = int(mouse_match.group(1))
                col = int(mouse_match.group(2)) - 1
                row = int(mouse_match.group(3)) - 1
                action = "press" if mouse_match.group(4) == b"M" else "release"

                # SGR 按钮编码
                # 0=左, 1=中, 2=右, 32+ = 带修饰键的移动
                btn = btn_raw & 0x3F
                mod = (btn_raw >> 6) & 0x1F  # shift/alt/ctrl etc

                if btn_raw >= 64:
                    # 滚轮
                    wheel = "up" if btn_raw == 64 else "down"
                    return {
                        "type": "mouse",
                        "action": "wheel",
                        "wheel": wheel,
                        "button": btn,
                        "col": col,
                        "row": row,
                        "raw": data,
                    }
                elif btn_raw >= 32:
                    # 带按钮按下的移动
                    return {
                        "type": "mouse",
                        "action": "move",
                        "button": btn + 1,  # 0-based → 1-based
                        "col": col,
                        "row": row,
                        "raw": data,
                    }

                return {
                    "type": "mouse",
                    "action": action,
                    "button": btn,  # 0=左, 1=中, 2=右
                    "col": col,
                    "row": row,
                    "raw": data,
                }

            # ── 其他 CSI（如窗口大小回复） ─────────────────
            return {"type": "csi", "raw": data}

        # ── ESC 单独按下 ──────────────────────────────────
        if data == b"\x1b":
            return {"type": "key", "key_code": 27, "modifiers": 0, "event": "press", "raw": data}

        # ── 普通 UTF-8 文本 ──────────────────────────────
        try:
            text = data.decode("utf-8")
            # 过滤控制字符（除了常用的）
            if text.isprintable() or text in ("\n", "\r", "\t", "\x08", "\x7f"):
                return {"type": "text", "text": text, "raw": data}
        except UnicodeDecodeError:
            pass

        return {"type": "unknown", "raw": data}


# ── 视频数据 ──────────────────────────────────────────────
SEARCH_URL = "https://hanime1.me/search"
BASE_URL = "https://hanime1.me"

def search_videos(query: str) -> list[dict]:
    """搜索视频（复用现有逻辑）。"""
    if not HAS_SCRAPER:
        return []

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    url = f"{SEARCH_URL}?query={query}&type="
    resp = scraper.get(url, timeout=20)

    if resp.status_code != 200:
        return []

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for el in soup.select(".video-item-container"):
        link = el.select_one("a.video-link")
        href = link.get("href", "") if link else ""

        if "erolabs" in href.lower() or "erodalabs" in href.lower():
            continue
        if not href.startswith("http"):
            href = BASE_URL + href

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
        title_text = title_el.get_text(strip=True) if title_el else el.get("title", "").strip()

        # 作者
        sub_el = el.select_one("div.subtitle a")
        author = sub_el.get_text(strip=True) if sub_el else ""

        # 缩略图
        thumb_el = el.select_one("img.main-thumb")
        thumb = thumb_el.get("src", "") if thumb_el else ""

        results.append({
            "title": title_text,
            "url": href,
            "duration": duration,
            "likes": likes,
            "views": views,
            "author": author,
            "thumbnail": thumb,
        })

    return results


def download_thumbnail(url: str) -> Optional[bytes]:
    """下载缩略图。"""
    if not HAS_SCRAPER or not url:
        return None
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    try:
        resp = scraper.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


# ── UI 渲染器 ─────────────────────────────────────────────
class UIRenderer:
    """管理终端上的文本 UI 渲染（标题、列表、状态栏）。"""

    def __init__(self, t: Terminal):
        self.t = t

    def draw_title_bar(self, text: str):
        """顶部标题栏 (行 0)。"""
        w = self.t.cols
        bar = f" {text} ".ljust(w)[:w]
        self.t.draw_text(0, 0, bar, style=b"\x1b[7m")  # 反色

    def draw_status_bar(self, text: str):
        """底部状态栏 (最后一行)。"""
        w = self.t.cols
        bar = f" {text} ".ljust(w)[:w]
        self.t.draw_text(self.t.rows - 1, 0, bar, style=b"\x1b[7m")

    def draw_centered_text(self, text: str, offset_row: int = 0):
        """屏幕中央绘制文本。"""
        row = self.t.rows // 2 + offset_row
        col = max(0, (self.t.cols - len(text)) // 2)
        self.t.draw_text(row, col, text, style=BOLD)

    def draw_box(self, r1: int, c1: int, r2: int, c2: int, title: str = ""):
        """绘制边框。"""
        tl, tr, bl, br = "┌", "┐", "└", "┘"
        hz, vt = "─", "│"

        # 顶边
        self.t.draw_text(r1, c1, tl + hz * max(0, c2 - c1 - 1) + tr)
        if title:
            self.t.draw_text(r1, c1 + 2, f" {title} ", style=BOLD)
        # 底边
        self.t.draw_text(r2, c1, bl + hz * max(0, c2 - c1 - 1) + br)
        # 侧边
        for r in range(r1 + 1, r2):
            self.t.draw_text(r, c1, vt)
            self.t.draw_text(r, c2, vt)


# ── App 主循环 ────────────────────────────────────────────
class POCApp:
    """概念验证：Kitty 图形 + 键盘 + 鼠标。"""

    def __init__(self):
        self.t = Terminal()
        self.gfx = KittyGraphics(self.t)
        self.kbd = KeyboardReader(self.t)
        self.ui = UIRenderer(self.t)
        self.running = False
        self.mode = "home"  # home | search | playing
        self.cursor_col = 0
        self.results = []
        self.selected_idx = 0
        self.search_query = ""
        self.thumb_ids = []  # 已显示的缩略图 image IDs
        self.banner_id = 0
        self.mouse_hover = (-1, -1)

    def run(self):
        """主循环。"""
        self.running = True
        self.t.enter_raw()
        self.kbd.enable()

        # 注册 SIGWINCH 处理
        signal.signal(signal.SIGWINCH, lambda s, f: self._handle_resize())

        try:
            self._draw_home()
            while self.running:
                events = self.kbd.read_events(timeout=0.05)
                for ev in events:
                    self._handle_event(ev)
        finally:
            self._cleanup()

    def _cleanup(self):
        """清理所有图像。"""
        for img_id in self.thumb_ids:
            self.gfx.delete_image(img_id)
        if self.banner_id:
            self.gfx.delete_image(self.banner_id)
        self.kbd.disable()
        self.t.exit_raw()

    def _handle_resize(self):
        self.t.query_size()
        if self.mode == "home":
            self._draw_home()
        elif self.mode == "search":
            self._draw_search_ui()
            self._render_results()

    def _handle_event(self, ev: dict):
        if ev["type"] == "key":
            self._on_key(ev)
        elif ev["type"] == "mouse":
            self._on_mouse(ev)
        elif ev["type"] == "text":
            self._on_text(ev)

    def _on_key(self, ev: dict):
        """按键处理。"""
        if ev["event"] == "release":
            return  # 忽略 release

        kc = ev["key_code"]

        # ESC / q → 退出
        if kc == 27 or kc == 113:  # ESC or 'q'
            if self.mode == "search":
                self.mode = "home"
                self._draw_home()
            else:
                self.running = False
            return

        # Enter
        if kc == 13:
            if self.mode == "search" and self.results:
                self._open_video(self.selected_idx)
            elif self.mode == "home":
                self.mode = "search"
                self._draw_search_ui()
            return

        # 's' → 搜索模式
        if kc == 115 and self.mode == "home":
            self.mode = "search"
            self._draw_search_ui()
            return

        # 上下导航
        if kc == 65 or kc == 107:  # Up or 'k'
            if self.mode == "search":
                self.selected_idx = max(0, self.selected_idx - 1)
                self._render_results()
            return
        if kc == 66 or kc == 106:  # Down or 'j'
            if self.mode == "search":
                self.selected_idx = min(len(self.results) - 1, self.selected_idx + 1) if self.results else 0
                self._render_results()
            return

        # Backspace (在搜索中删除)
        if kc == 127 or kc == 8:
            if self.mode == "search":
                self.search_query = self.search_query[:-1]
                self._draw_search_ui()
            return

    def _on_text(self, ev: dict):
        """文本输入处理。"""
        if self.mode == "search":
            text = ev["text"]
            if text.isprintable():
                self.search_query += text
                self._draw_search_ui()
            elif text == "\r":
                self._do_search()

    def _on_mouse(self, ev: dict):
        """鼠标事件处理。"""
        if ev.get("action") == "wheel":
            if ev["wheel"] == "up":
                self.selected_idx = max(0, self.selected_idx - 1)
            else:
                self.selected_idx = min(len(self.results) - 1, self.selected_idx + 1) if self.results else 0
            if self.mode == "search":
                self._render_results()
            return

        if ev.get("action") == "press" and ev["button"] == 0:  # 左键点击
            row, col = ev["row"], ev["col"]
            self.mouse_hover = (row, col)
            if self.mode == "home":
                # 点击搜索区域 → 进入搜索
                if row == 1 and 10 <= col <= 40:
                    self.mode = "search"
                    self._draw_search_ui()
            elif self.mode == "search":
                # 点击结果项
                header_h = 3  # 标题栏 + 搜索栏
                idx = row - header_h
                if 0 <= idx < len(self.results):
                    self.selected_idx = idx
                    self._render_results()
                    self._open_video(idx)

    # ── 绘制 ──────────────────────────────────────────
    def _draw_home(self):
        """首页：标题 + 快捷入口。"""
        self.t.clear_screen()
        w = self.t.cols
        h = self.t.rows

        self.ui.draw_title_bar("🏠 Hanime1.me — Kitty Native TUI")
        self.ui.draw_status_bar("s:搜索  ↑↓:选择  Enter:确认  q:退出")

        # 中央欢迎区
        self.ui.draw_centered_text("🎬 Hanime1.me Native TUI", -3)
        self.ui.draw_centered_text("Kitty Graphics Protocol + Keyboard Protocol", -1)
        self.ui.draw_centered_text("", 0)

        # 搜索入口
        search_row = h // 2 + 2
        search_col = (w - 40) // 2
        self.ui.draw_box(search_row - 1, search_col - 2, search_row + 1, search_col + 42)
        self.t.draw_text(search_row, search_col, "🔍  按 s 或点击此处搜索视频…", style=BOLD)

        # 提示
        self.ui.draw_centered_text("在终端内显示真实像素缩略图，无需 Textual", 4)
        self.ui.draw_centered_text(f"终端: {self.t.cols}×{self.t.rows} cells · {self.t.px_w}×{self.t.px_h} px", 6)

    def _draw_search_ui(self):
        """绘制搜索 UI 框架。"""
        self.t.clear_screen()
        w = self.t.cols

        self.ui.draw_title_bar("🔍 搜索 Hanime1.me")
        self.ui.draw_status_bar(
            f"结果: {len(self.results)} · 选中: {self.selected_idx + 1 if self.results else 0}"
            + f" · 输入关键词后按 Enter · Esc 返回"
        )

        # 搜索输入区
        prompt = f"🔍 {self.search_query}"
        display = prompt[:w - 4] + ("▊" if len(prompt) < w - 4 else "")
        self.t.draw_text(2, 2, display, style=BOLD)

        # 结果区域从第 4 行开始
        if not self.results:
            self.t.draw_text(5, 2, "输入关键词后按 Enter 搜索…", style=DIM)
        else:
            self._render_results()

    def _render_results(self):
        """渲染搜索结果列表（文本）。"""
        if not self.results:
            return

        # 清除旧图像
        for img_id in self.thumb_ids:
            self.gfx.delete_image(img_id)
        self.thumb_ids.clear()

        h = self.t.rows
        w = self.t.cols
        header_end = 3
        content_h = h - header_end - 1  # 减去底栏

        # 每项的单元格高度（缩略图行高）
        thumb_h = 6  # 单元格高度
        items_per_view = min(len(self.results), content_h // thumb_h)

        # 计算可见范围
        start_idx = max(0, self.selected_idx - items_per_view // 2)
        start_idx = min(start_idx, max(0, len(self.results) - items_per_view))

        for i in range(items_per_view):
            idx = start_idx + i
            if idx >= len(self.results):
                break

            v = self.results[idx]
            row = header_end + i * thumb_h + 1

            # 清空行区域
            for rr in range(row, min(row + thumb_h, h - 1)):
                self.t._buf.write(
                    f"\x1b[{rr+1};{1+1}H".encode()
                    + b" " * (w - 2)
                )

            # 是否选中
            is_selected = idx == self.selected_idx
            prefix = "▶" if is_selected else " "
            style = b"\x1b[1;33m" if is_selected else b""

            # 显示缩略图 (Kitty Graphics Protocol)
            if v["thumbnail"]:
                thumb_data = download_thumbnail(v["thumbnail"])
                if thumb_data:
                    img_w = 12  # 缩略图宽度（cell）
                    try:
                        img_id = self.gfx.display_image(
                            thumb_data,
                            row=row,
                            col=2,
                        )
                        self.thumb_ids.append(img_id)
                    except Exception:
                        pass

            # 文本信息（缩略图右侧）
            text_col = 2 + 14  # 缩略图后面
            title = v["title"][:w - text_col - 2]
            meta = f"by {v['author']}  ·  ⏱ {v['duration']}  ·  👁 {v['views']}  ·  ♥ {v['likes']}"

            self.t.draw_text(row, text_col, title, style=BOLD if is_selected else b"")
            self.t.draw_text(row + 1, text_col, meta[:w - text_col - 2], style=DIM)

        self.t._buf.flush()

    def _do_search(self):
        """执行搜索。"""
        if not self.search_query.strip():
            return

        self.t.draw_text(3, 2, "⏳ 搜索中…", style=DIM)

        self.results = search_videos(self.search_query)
        self.selected_idx = 0
        self._draw_search_ui()

    def _open_video(self, idx: int):
        """打开视频详情（简化版：仅显示信息）。"""
        if idx < 0 or idx >= len(self.results):
            return
        v = self.results[idx]
        self.t.clear_screen()
        w = self.t.cols

        self.ui.draw_title_bar(f"🎬 {v['title'][:w-5]}")
        self.ui.draw_status_bar("按 Esc 返回搜索 · q 退出")

        # 显示海报
        if v["thumbnail"]:
            # 尝试用更大的尺寸显示缩略图
            thumb_data = download_thumbnail(v["thumbnail"])
            if thumb_data:
                try:
                    poster_id = self.gfx.display_image(
                        thumb_data,
                        row=2,
                        col=2,
                    )
                    self.thumb_ids.append(poster_id)
                except Exception:
                    pass

        self.t.draw_text(self.t.rows // 2 + 2, 2, f"标题: {v['title']}", style=BOLD)
        self.t.draw_text(self.t.rows // 2 + 3, 2, f"作者: {v['author']}")
        self.t.draw_text(self.t.rows // 2 + 4, 2, f"时长: {v['duration']}  ·  👁 {v['views']}  ·  ♥ {v['likes']}")
        self.t.draw_text(self.t.rows // 2 + 5, 2, f"URL: {v['url']}", style=DIM)
        self.t.draw_text(self.t.rows // 2 + 7, 2, "⚠ 完整视频提取功能待实现", style=DIM)

        # 等待用户按 Esc/Enter 返回
        old_mode = self.mode
        self.mode = "playing"

        # 事件循环：等待返回
        while self.mode == "playing" and self.running:
            events = self.kbd.read_events(timeout=0.05)
            for ev in events:
                if ev["type"] == "key" and ev["event"] == "press":
                    kc = ev["key_code"]
                    if kc == 27:  # Esc → 返回搜索
                        self.mode = old_mode
                        if self.mode == "search":
                            self._draw_search_ui()
                            self._render_results()
                        else:
                            self._draw_home()
                        return
                    elif kc == 113:  # q → 退出
                        self.running = False
                        return


# ── 入口 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Kitty Native TUI — PoC")
    parser.add_argument("query", nargs="?", default="", help="初始搜索词")
    args = parser.parse_args()

    if not os.environ.get("KITTY_WINDOW_ID"):
        print("⚠ 此程序需要在 Kitty 终端中运行才能使用图形协议。")
        print("  启动方式: kitty python kitty_native_poc.py")
        print("  当前会以降级模式运行（仅文本）…\n")
        # 不退出，允许在非 Kitty 终端中测试文本部分

    app = POCApp()
    if args.query:
        app.search_query = args.query
        app.mode = "search"

    app.run()


if __name__ == "__main__":
    main()
