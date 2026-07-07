#!/usr/bin/env python3
"""
Kitty-native TUI infrastructure module.

Provides terminal management, Kitty Graphics Protocol, keyboard/mouse input
parsing, UI rendering primitives, and a Screen base class for building
Kitty-native terminal applications.

Requires Kitty terminal for full graphics protocol support.
"""

import sys
import re
import base64
import fcntl
import termios
import tty
import select
import signal
import array
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# Section 1: Constants & Protocol Sequences
# ═══════════════════════════════════════════════════════════════════

# Kitty Graphics Protocol APC (Application Program Command) sequences
APC_START = b"\x1b_G"
APC_END = b"\x1b\\"

# Kitty Keyboard Protocol
KBD_ENABLE = b"\x1b[>1u"       # Basic disambiguation mode
KBD_FULL = b"\x1b[>31u"        # Full enhanced: event types + alternate keys + text
KBD_DISABLE = b"\x1b[<u"       # Restore default keyboard handling

# SGR Mouse tracking (combined: button events 1000h + motion tracking 1003h + SGR encoding 1006h)
MOUSE_ON = b"\x1b[?1000h\x1b[?1003h\x1b[?1006h"
MOUSE_OFF = b"\x1b[?1000l\x1b[?1003l\x1b[?1006l"

# Screen buffer management
ALT_SCREEN = b"\x1b[?1049h"
MAIN_SCREEN = b"\x1b[?1049l"
CLEAR = b"\x1b[2J"
HOME = b"\x1b[H"

# Cursor visibility
CURSOR_HIDE = b"\x1b[?25l"
CURSOR_SHOW = b"\x1b[?25h"

# ANSI SGR style helpers
RESET = b"\x1b[0m"
BOLD = b"\x1b[1m"
DIM = b"\x1b[2m"


def rgb_fg(r: int, g: int, b: int) -> bytes:
    """Return ANSI true-color foreground escape sequence."""
    return f"\x1b[38;2;{r};{g};{b}m".encode()


def rgb_bg(r: int, g: int, b: int) -> bytes:
    """Return ANSI true-color background escape sequence."""
    return f"\x1b[48;2;{r};{g};{b}m".encode()


# ═══════════════════════════════════════════════════════════════════
# Section 2: Terminal class
# ═══════════════════════════════════════════════════════════════════

class Terminal:
    """Manages raw mode, alternate screen, cursor positioning, drawing primitives."""

    def __init__(self):
        self.old_termios = None
        self.rows: int = 24
        self.cols: int = 80
        self.px_w: int = 800
        self.px_h: int = 600
        self.cell_w: int = 1
        self.cell_h: int = 1
        self._buf = sys.stdout.buffer

    # ── Mode management ──────────────────────────────────────

    def enter_raw(self):
        """Enter raw mode + alternate screen + hide cursor + clear."""
        self.old_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        self.send(ALT_SCREEN, CURSOR_HIDE, CLEAR, HOME)
        self.query_size()

    def exit_raw(self):
        """Restore terminal: disable mouse/kbd, show cursor, main screen, restore termios."""
        self.send(MOUSE_OFF, KBD_DISABLE, CURSOR_SHOW, MAIN_SCREEN)
        if self.old_termios:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_termios)

    def query_size(self):
        """Query terminal pixel and cell dimensions using TIOCGWINSZ ioctl."""
        buf = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, buf)
        self.rows, self.cols, self.px_w, self.px_h = buf
        if self.cols > 0:
            self.cell_w = self.px_w // self.cols
            self.cell_h = self.px_h // self.rows

    # ── Output ───────────────────────────────────────────────

    def send(self, *args: bytes):
        """Write byte sequences to stdout.buffer and flush."""
        for a in args:
            self._buf.write(a)
        self._buf.flush()

    def move_to(self, row: int, col: int):
        """Position cursor at (row, col) using CSI sequence (1-based internally)."""
        self._buf.write(f"\x1b[{row + 1};{col + 1}H".encode())
        self._buf.flush()

    # ── Drawing primitives ───────────────────────────────────

    def draw_text(self, row: int, col: int, text: str, *,
                  style: bytes = b"", max_width: int = 0):
        """
        Draw styled text at (row, col).

        If max_width > 0 and text is longer, the text is truncated and suffixed
        with an ellipsis character ('…').
        """
        if max_width > 0 and len(text) > max_width:
            text = text[:max_width - 1] + "…"
        self._buf.write(
            f"\x1b[{row + 1};{col + 1}H".encode()
            + style
            + text.encode()
            + RESET
        )
        self._buf.flush()

    def clear_screen(self):
        """Clear the screen and move cursor home."""
        self.send(CLEAR, HOME)

    def fill_rect(self, row: int, col: int, h: int, w: int, ch: str = " "):
        """Fill a rectangular region with the given character."""
        line = ch * w
        for i in range(h):
            self._buf.write(
                f"\x1b[{row + i + 1};{col + 1}H".encode()
                + line.encode()
            )
        self._buf.flush()


# ═══════════════════════════════════════════════════════════════════
# Section 3: KittyGraphics class
# ═══════════════════════════════════════════════════════════════════

class KittyGraphics:
    """Kitty Graphics Protocol -- display/delete pixel images in terminal."""

    def __init__(self, terminal: Terminal):
        self.t = terminal
        self._counter = 0

    def next_id(self) -> int:
        """Generate and return a unique image ID."""
        self._counter += 1
        return self._counter

    def display_image(self, data: bytes, *,
                      row: int = 0, col: int = 0,
                      image_id: int = 0, z_index: int = -1,
                      quiet: bool = True,
                      img_cols: int = 0, img_rows: int = 0) -> int:
        """
        Display a PNG image at the specified terminal position.

        Uses chunked base64 transmission (4096 bytes per chunk).
        Control data: a=T (transmit+display), f=100 (PNG), C=1 (don't move cursor).

        *img_cols* / *img_rows* control the display size in terminal cells.
        When only one dimension is given the other is auto-calculated to
        preserve aspect ratio.  When both are 0 (the default) the image
        is displayed at its native pixel size.

        Returns the image_id.
        """
        if not image_id:
            image_id = self.next_id()

        # Build control data string (used in first chunk only)
        ctrl_parts = [
            "a=T",
            "f=100",
            f"i={image_id}",
            "q=2" if quiet else "q=1",
            "C=1",
            f"z={z_index}",
        ]
        if img_cols > 0:
            ctrl_parts.append(f"c={img_cols}")
        if img_rows > 0:
            ctrl_parts.append(f"r={img_rows}")
        ctrl_str = ",".join(ctrl_parts)

        # Base64 encode the image data
        b64_data = base64.standard_b64encode(data)

        # Split into <= 4096 byte chunks and transmit
        chunk_size = 4096
        offset = 0
        while offset < len(b64_data):
            chunk = b64_data[offset:offset + chunk_size]
            offset += chunk_size
            is_last = offset >= len(b64_data)
            m = 0 if is_last else 1

            if offset <= chunk_size:
                # First (and possibly only) chunk: include full control data
                payload = f"{ctrl_str},m={m};".encode("ascii") + chunk
            else:
                # Subsequent chunks: only need the m= flag
                payload = f"m={m};".encode("ascii") + chunk

            self.t.send(APC_START + payload + APC_END)

        return image_id

    def delete_image(self, image_id: int):
        """Delete image and all its placements. Uses a=d (delete), d=I (image)."""
        ctrl = f"a=d,d=I,i={image_id},q=2"
        self.t.send(APC_START + ctrl.encode("ascii") + b";" + APC_END)

    def display_image_url(self, url: str, *,
                          row: int = 0, col: int = 0,
                          image_id: int = 0, z_index: int = -1,
                          quiet: bool = True,
                          img_cols: int = 0, img_rows: int = 0) -> Optional[int]:
        """
        Download image from URL using cloudscraper and display it.

        Keyword arguments are forwarded to :meth:`display_image`.

        Returns the image_id on success, or None if the download fails
        or cloudscraper is not available.
        """
        try:
            import cloudscraper
        except ImportError:
            return None

        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        try:
            resp = scraper.get(url, timeout=10)
            if resp.status_code == 200:
                return self.display_image(
                    resp.content, row=row, col=col, image_id=image_id,
                    z_index=z_index, quiet=quiet,
                    img_cols=img_cols, img_rows=img_rows,
                )
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════
# Section 4: KeyboardReader class
# ═══════════════════════════════════════════════════════════════════

class KeyboardReader:
    """Reads Kitty enhanced keyboard protocol + SGR Mouse events."""

    def __init__(self, terminal: Terminal):
        self.t = terminal
        self.stdin = sys.stdin.buffer

    def enable(self):
        """Enable Kitty full keyboard protocol and SGR mouse tracking."""
        self.t.send(KBD_FULL, MOUSE_ON)

    def disable(self):
        """Restore default keyboard and mouse modes."""
        self.t.send(MOUSE_OFF, KBD_DISABLE)

    def read_event(self, timeout: float = 0.05) -> Optional[dict]:
        """
        Read ONE input event.

        Blocks up to *timeout* seconds via select.select on stdin.
        Returns a parsed event dict, or None if nothing was read.
        """
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None

        data = self.stdin.read1(256)
        if not data:
            return None

        return self._parse(data)

    def read_events(self, timeout: float = 0.01) -> list[dict]:
        """Read all pending events (loops until no more data, max 10 iterations)."""
        events: list[dict] = []
        for _ in range(10):
            ev = self.read_event(timeout=timeout)
            if ev:
                events.append(ev)
            else:
                break
        return events

    def _parse(self, data: bytes) -> Optional[dict]:
        """
        Parse raw input bytes into an event dict.

        Event types returned:
        - key:   {'type': 'key',   'key_code': int, 'modifiers': int,
                  'event': 'press'|'repeat'|'release'}
        - mouse: {'type': 'mouse', 'action': 'press'|'release'|'move'|'wheel',
                  'button': int, 'col': int, 'row': int}
        - text:  {'type': 'text',  'text': str}
        - resize:{'type': 'resize'}

        Returns None for unrecognized or unparseable input.
        """
        if not data:
            return None

        # ── CSI sequences (ESC [ ...) ────────────────────────
        if data.startswith(b"\x1b["):
            rest = data[2:]

            # SGR Mouse: CSI < btn ; col ; row M/m
            mouse_match = re.match(rb"<(\d+);(\d+);(\d+)([Mm])", rest)
            if mouse_match:
                btn_raw = int(mouse_match.group(1))
                col = int(mouse_match.group(2)) - 1
                row = int(mouse_match.group(3)) - 1
                action_char = mouse_match.group(4)

                btn = btn_raw & 0x3F  # lower 6 bits = button number

                # Wheel events (button code & 64 != 0): 64 = up, 65 = down
                if btn_raw >= 64:
                    wheel = "up" if btn_raw == 64 else "down"
                    return {
                        "type": "mouse",
                        "action": "wheel",
                        "button": btn,
                        "col": col,
                        "row": row,
                    }

                # Motion with button pressed (button code >= 32)
                if btn_raw >= 32:
                    return {
                        "type": "mouse",
                        "action": "move",
                        "button": btn + 1,  # 0-based -> 1-based
                        "col": col,
                        "row": row,
                    }

                # Press / Release
                return {
                    "type": "mouse",
                    "action": "press" if action_char == b"M" else "release",
                    "button": btn,
                    "col": col,
                    "row": row,
                }

            # Kitty Keyboard Protocol: CSI number ; modifiers [u~]
            kbd_match = re.match(rb"([0-9:;]+)\s*([u~])", rest)
            if kbd_match:
                codes_str = kbd_match.group(1).decode("ascii")

                # Parse fields: unicode_codepoint[:shifted[:base]] ; modifiers[:event_type]
                parts = codes_str.split(";")
                kp = parts[0].split(":")
                key_code = int(kp[0]) if kp[0] else 0

                # Parse modifiers (subtract 1 to get bitfield) and embedded event type
                modifiers_raw = 1
                ev_type = 1  # default: press
                if len(parts) > 1 and parts[1]:
                    mp = parts[1].split(":")
                    modifiers_raw = int(mp[0]) if mp[0] else 1
                    ev_type = int(mp[1]) if len(mp) > 1 and mp[1] else 1

                mod_bits = modifiers_raw - 1
                event_map = {1: "press", 2: "repeat", 3: "release"}

                return {
                    "type": "key",
                    "key_code": key_code,
                    "modifiers": mod_bits,
                    "event": event_map.get(ev_type, "press"),
                }

            # Unrecognized CSI -- ignore
            return None

        # ── ESC alone ────────────────────────────────────────
        if data == b"\x1b":
            return {"type": "key", "key_code": 27, "modifiers": 0, "event": "press"}

        # ── Plain UTF-8 text ──────────────────────────────────
        try:
            text = data.decode("utf-8")
            if text.isprintable() or text in ("\n", "\r", "\t", "\x08", "\x7f"):
                return {"type": "text", "text": text}
        except UnicodeDecodeError:
            pass

        return None


# ═══════════════════════════════════════════════════════════════════
# Section 5: UIRenderer class
# ═══════════════════════════════════════════════════════════════════

class UIRenderer:
    """High-level UI drawing on top of Terminal."""

    def __init__(self, terminal: Terminal):
        self.t = terminal

    # ── Bars ─────────────────────────────────────────────────

    def draw_title_bar(self, text: str):
        """Row 0: full-width reverse-video title bar."""
        w = self.t.cols
        bar = f" {text} ".ljust(w)[:w]
        self.t.draw_text(0, 0, bar, style=b"\x1b[7m")

    def draw_status_bar(self, text: str):
        """Last row: full-width reverse-video status bar."""
        w = self.t.cols
        bar = f" {text} ".ljust(w)[:w]
        self.t.draw_text(self.t.rows - 1, 0, bar, style=b"\x1b[7m")

    # ── Text helpers ─────────────────────────────────────────

    def draw_centered_text(self, text: str, offset_row: int = 0, *,
                           style: bytes = b""):
        """Draw text centered horizontally on screen."""
        row = self.t.rows // 2 + offset_row
        col = max(0, (self.t.cols - len(text)) // 2)
        self.t.draw_text(row, col, text, style=style or BOLD)

    # ── Drawing shapes ───────────────────────────────────────

    def draw_box(self, r1: int, c1: int, r2: int, c2: int,
                 title: str = "", style: bytes = b""):
        """
        Draw a box with border characters.

        Corners: top-left, top-right, bottom-left, bottom-right
        Edges: horizontal dash, vertical bar.
        Optional title is embedded in the top border.
        """
        tl, tr, bl, br = "┌", "┐", "└", "┘"
        hz, vt = "─", "│"

        h_width = max(0, c2 - c1 - 1)

        # Top edge
        self.t.draw_text(r1, c1, tl + hz * h_width + tr, style=style)
        if title:
            self.t.draw_text(r1, c1 + 2, f" {title} ", style=style or BOLD)

        # Bottom edge
        self.t.draw_text(r2, c1, bl + hz * h_width + br, style=style)

        # Side edges
        for r in range(r1 + 1, r2):
            self.t.draw_text(r, c1, vt, style=style)
            self.t.draw_text(r, c2, vt, style=style)

    def draw_horizontal_separator(self, row: int, col_start: int, col_end: int):
        """Draw a horizontal line across the given column span."""
        width = max(0, col_end - col_start)
        self.t.draw_text(row, col_start, "─" * width)

    # ── Widget helpers ───────────────────────────────────────

    def draw_search_bar(self, row: int, query: str, *,
                        is_active: bool = True):
        """
        Draw a search input bar at the given row.

        Shows a magnifying glass + query text + blinking cursor indicator,
        wrapped in a box.
        """
        w = self.t.cols
        cursor = "▊" if is_active else ""  # left-quarter block cursor
        display = f"\U0001f50d {query}{cursor}"

        # Clamp to reasonable width
        if len(display) > w - 4:
            display = display[:w - 5] + (cursor if is_active else "")

        box_w = min(w - 4, 60)
        c1 = max(0, (w - box_w) // 2)
        self.draw_box(row - 1, c1 - 1, row + 1, c1 + box_w)
        self.t.draw_text(row, c1, display, style=BOLD if is_active else DIM)

    def draw_result_item(self, row: int, col: int, title: str,
                          meta: str, is_selected: bool = False,
                          max_width: int = 80):
        """
        Draw a single search result item.

        If selected: prefix with a play symbol, use bold yellow styling.
        First line: title.  Second line: meta info (dimmed).
        """
        if is_selected:
            prefix = "▶ "
            style = b"\x1b[1;33m"  # bold yellow
        else:
            prefix = "  "
            style = b""

        # Title line
        title_line = f"{prefix}{title}"
        self.t.draw_text(row, col, title_line, style=style, max_width=max_width)

        # Meta line (indented, dimmed)
        self.t.draw_text(row + 1, col + 2, meta, style=DIM, max_width=max_width - 2)


# ═══════════════════════════════════════════════════════════════════
# Section 6: Screen base class
# ═══════════════════════════════════════════════════════════════════

class Screen:
    """Base class for all screens. Implements event dispatch and click zones."""

    def __init__(self, app: 'App'):
        self.app = app
        self._active: bool = False
        self._click_zones: list[tuple] = []  # (r1, r2, c1, c2, action, data)

    # ── Lifecycle ────────────────────────────────────────────

    def on_enter(self):
        """Called when screen becomes active. Override to draw initial state."""

    def on_leave(self):
        """Called when screen is being replaced. Override to clean up."""

    # ── Event dispatch ───────────────────────────────────────

    def handle_event(self, event: dict) -> bool:
        """
        Handle an input event. Returns True if the event was consumed.

        Default behaviour dispatches to on_key / on_mouse / on_text
        based on the event type.
        """
        if event is None:
            return False

        ev_type = event.get("type", "")

        if ev_type == "key":
            return self.on_key(event)
        elif ev_type == "mouse":
            return self.on_mouse(event)
        elif ev_type == "text":
            return self.on_text(event)
        elif ev_type == "resize":
            self.on_resize()
            return True
        return False

    def on_key(self, ev: dict) -> bool:
        """Handle key events. Override in subclasses."""
        return False

    def on_mouse(self, ev: dict) -> bool:
        """
        Handle mouse events.

        Default behaviour: checks _click_zones for a matching region.
        Only triggers on left-click press events.
        If a zone matches, calls the handler method named *action* on this screen.
        """
        if ev.get("action") != "press" or ev.get("button") != 0:
            return False

        row = ev.get("row", -1)
        col = ev.get("col", -1)

        for r1, r2, c1, c2, action, data in self._click_zones:
            if r1 <= row <= r2 and c1 <= col <= c2:
                handler = getattr(self, action, None)
                if handler:
                    handler(data)
                    return True
        return False

    def on_text(self, ev: dict) -> bool:
        """Handle text input. Override in subclasses."""
        return False

    def on_resize(self):
        """Called when terminal is resized. Re-queries size; override to redraw."""
        self.app.t.query_size()

    # ── Click zones ──────────────────────────────────────────

    def add_click_zone(self, r1: int, r2: int, c1: int, c2: int,
                       action: str, data=None):
        """Register a clickable region. *action* is a method name on this screen."""
        self._click_zones.append((r1, r2, c1, c2, action, data))

    def clear_click_zones(self):
        """Remove all registered click zones."""
        self._click_zones.clear()


# ═══════════════════════════════════════════════════════════════════
# Section 7: Signal handling
# ═══════════════════════════════════════════════════════════════════

# Module-level flag set by the SIGWINCH signal handler.
# The application's main loop should check this flag each iteration
# and call the current screen's on_resize() when it is set.
resize_pending: bool = False


def _on_sigwinch(signum, frame):
    """SIGWINCH handler: sets the module-level resize_pending flag."""
    global resize_pending
    resize_pending = True


def install_resize_handler():
    """Register the SIGWINCH handler that sets resize_pending."""
    signal.signal(signal.SIGWINCH, _on_sigwinch)
