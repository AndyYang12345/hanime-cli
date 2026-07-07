#!/usr/bin/env python3
"""
Enhanced scraping module for hanime1.me — Kitty-native TUI backend.

Provides all data fetching and parsing functions:
  - search_videos(query) → list[VideoResult]
  - extract_video_detail(video_url) → VideoDetail | None
  - fetch_homepage() → HomePageData | None
  - download_thumbnail(url) → bytes | None

Models: VideoResult, VideoDetail, Category, HomePageData

Usage:
    from kitty_scraper import search_videos, extract_video_detail, fetch_homepage
"""

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin

import cloudscraper
from bs4 import BeautifulSoup

# ── Constants ──────────────────────────────────────────────────
BASE_URL = "https://hanime1.me"
SEARCH_URL = f"{BASE_URL}/search"

# Title-cleaning separators (&nbsp; → \\xa0)
_TITLE_SEPARATORS: tuple[str, ...] = (
    "\xa0-\xa0H動漫",
    "\xa0-\xa0Hanime1.me",
    " - H動漫",
    " - Hanime1.me",
)


# ══════════════════════════════════════════════════════════════════
# Data Models
# ══════════════════════════════════════════════════════════════════

@dataclass
class VideoResult:
    """A video from search results or homepage."""
    title: str = ""
    url: str = ""
    duration: str = ""
    views: str = ""
    likes: str = ""
    author: str = ""
    thumbnail: str = ""


@dataclass
class VideoDetail:
    """Full video page info including sources and tags."""
    title: str = ""
    poster: str = ""
    sources: list = field(default_factory=list)   # [(resolution_str, url_str), ...]
    tags: list = field(default_factory=list)       # [(tag_text, search_param), ...]
    page_url: str = ""
    duration: str = ""
    views: str = ""
    likes: str = ""
    author: str = ""


@dataclass
class Category:
    """A category row from the homepage."""
    name: str = ""
    url: str = ""
    videos: list = field(default_factory=list)  # [VideoResult, ...]


@dataclass
class HomePageData:
    """Parsed homepage content."""
    banner: VideoResult | None = None
    banner_tags: list = field(default_factory=list)   # [str, ...] tag names
    categories: list = field(default_factory=list)     # [Category, ...]
    genres: list = field(default_factory=list)         # [(name, url), ...]


# ══════════════════════════════════════════════════════════════════
# HTTP Client
# ══════════════════════════════════════════════════════════════════

def create_scraper() -> cloudscraper.CloudScraper:
    """Create a cloudscraper instance with a Chrome/Windows fingerprint.

    Returns:
        A configured CloudScraper that bypasses Cloudflare JS Challenge.
    """
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        }
    )


# ══════════════════════════════════════════════════════════════════
# Internal Helpers — Element Parsing
# ══════════════════════════════════════════════════════════════════

def _parse_video_item(video_el) -> VideoResult | None:
    """Parse a single ``.video-item-container`` element into a VideoResult.

    Mirrors the logic in ``scrape_hanime.py:parse_video_item`` and
    ``hanime_tui.py:search_videos``.

    Args:
        video_el: A BeautifulSoup Tag with class ``video-item-container``.

    Returns:
        VideoResult, or *None* if parsing fails entirely.
    """
    try:
        # Title from the container's ``title`` attribute (most complete)
        full_title: str = video_el.get("title", "").strip()

        # Link
        link_el = video_el.select_one("a.video-link")
        href: str = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = BASE_URL + href

        # Thumbnail
        thumb_el = video_el.select_one("img.main-thumb")
        thumbnail: str = thumb_el.get("src", "") if thumb_el else ""

        # Duration
        dur_el = video_el.select_one("div.duration")
        duration: str = dur_el.get_text(strip=True) if dur_el else ""

        # Likes & Views — stat-item divs (icons removed before reading text)
        stats = video_el.select("div.stat-item")
        likes = views = ""
        if len(stats) > 0:
            for i_tag in stats[0].select("i"):
                i_tag.decompose()
            likes = stats[0].get_text(strip=True)
        if len(stats) > 1:
            for i_tag in stats[1].select("i"):
                i_tag.decompose()
            views = stats[1].get_text(strip=True)

        # Title text (div.title)
        title_el = video_el.select_one("div.title")
        title_text: str = title_el.get_text(strip=True) if title_el else full_title

        # Author
        sub_el = video_el.select_one("div.subtitle a")
        author: str = sub_el.get_text(strip=True) if sub_el else ""

        return VideoResult(
            title=title_text or full_title,
            url=href,
            duration=duration,
            views=views,
            likes=likes,
            author=author,
            thumbnail=thumbnail,
        )
    except Exception:
        return None


def _clean_title(raw_title: str) -> str:
    """Strip site-name suffixes from a raw video title.

    Args:
        raw_title: Raw title from ``<h1>`` or ``<title>``.

    Returns:
        Cleaned title without a trailing site-name separator.
    """
    for sep in _TITLE_SEPARATORS:
        if sep in raw_title:
            return raw_title.split(sep)[0].strip()
    return raw_title.strip()


def _clean_tag_text(text: str) -> str:
    """Remove parenthetical counts from tag text.

    Examples:
        ``"乳交(2)"`` → ``"乳交"``
        ``"#絕區零"`` → ``"#絕區零"``
        ``"2.5D"`` → ``"2.5D"``

    Args:
        text: Raw inner text of a tag ``<a>`` element.

    Returns:
        Cleaned tag text.
    """
    return re.sub(r"\s*\(\d+\)\s*$", "", text.strip()).strip()


def _find_content_root(soup: BeautifulSoup):
    """Locate the main content container that holds the video player and title.

    Walks up from ``<h1>`` or ``<video#player>`` to find a reasonable
    ancestor that encompasses the video-page content area.

    Args:
        soup: Parsed BeautifulSoup of a video detail page.

    Returns:
        A Tag element representing the content area, or *None*.
    """
    h1 = soup.select_one("h1")
    player = soup.select_one("video#player")

    # Try to find the lowest common ancestor of h1 and player
    if h1 is not None and player is not None:
        ancestor = h1.parent
        while ancestor is not None:
            if ancestor.select_one("video#player") is not None:
                return ancestor
            ancestor = ancestor.parent

    # Fallback: walk up from whichever element we have
    target = h1 or player
    if target is not None:
        container = target.parent
        for _ in range(5):
            if container is None:
                break
            container = container.parent
        return container

    return None


def _is_navigation_element(element) -> bool:
    """Check whether a BeautifulSoup Tag lives inside a navigation context.

    Args:
        element: A Tag to inspect.

    Returns:
        *True* if the element is inside a ``<nav>``, ``<header>``,
        or an element with a recognised navigation id/class.
    """
    for parent in element.parents:
        parent_id: str = parent.get("id", "")
        parent_classes: str = " ".join(parent.get("class", []))

        # Id-based
        if parent_id in ("main-nav-home", "sub-nav-home-mobile", "navbar"):
            return True

        # Tag-name-based
        if parent.name in ("nav", "header"):
            return True

        # Class-based heuristics
        nav_class_markers = (
            "navbar", "nav-menu", "sidebar", "side-nav",
            "main-nav", "sub-nav", "footer-nav", "header-nav",
        )
        if any(marker in parent_classes for marker in nav_class_markers):
            return True

    return False


def _extract_tags_from_page(
    soup: BeautifulSoup,
    content_root=None,
) -> list[tuple[str, str]]:
    """Extract hashtags, category tags, and genre links from a video detail page.

    Only tags in the content area (near the video player / title) are returned;
    global-navigation links are excluded.

    Tag types discovered:
        * **Hashtags** — ``href="/search?query=..."``, text starts with ``#``.
        * **Category tags** — ``href="/search?tags%5B%5D=..."``.
        * **Genre links** — ``href="/search?genre=..."`` in the content area.

    Args:
        soup: Parsed BeautifulSoup of the video detail page.
        content_root: Optional ancestor Tag to scope the search.

    Returns:
        List of ``(tag_text, search_param)`` tuples, deduplicated by
        case-insensitive text.
    """
    tags: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Collect every search link on the page
    search_links = soup.select('a[href*="/search"]')

    for a in search_links:
        href: str = a.get("href", "").strip()
        text: str = a.get_text(strip=True)

        if not href or not text:
            continue

        # ── Skip links inside navigation / header elements ──
        if _is_navigation_element(a):
            continue

        # ── Optionally restrict to content_root descendants ──
        if content_root is not None:
            if not _is_descendant_of(a, content_root):
                continue

        # ── Classify by href pattern ──
        entry: tuple[str, str] | None = None

        # 1. Hashtag:  /search?query=...  and text starts with #
        if "/search?query=" in href and text.startswith("#"):
            m = re.search(r"[?&]query=([^&]+)", href)
            query_value = unquote(m.group(1)) if m else text.lstrip("#")
            entry = (text, query_value)

        # 2. Category tag:  /search?tags%5B%5D=...  (URL-encoded [])
        elif "tags%5B%5D=" in href:
            cleaned = _clean_tag_text(text)
            abs_href = href if href.startswith("http") else urljoin(BASE_URL, href)
            entry = (cleaned, abs_href)

        # 3. Genre link:  /search?genre=...
        elif "/search?genre=" in href:
            m = re.search(r"[?&]genre=([^&]+)", href)
            genre_value = unquote(m.group(1)) if m else text
            entry = (text, f"genre:{genre_value}")

        # ── Deduplicate by case-insensitive text ──
        if entry is not None:
            norm_key = entry[0].strip().lower()
            if norm_key and norm_key not in seen:
                seen.add(norm_key)
                tags.append(entry)

    return tags


def _is_descendant_of(element, ancestor) -> bool:
    """Return *True* if *element* is a descendant of *ancestor* in the DOM tree.

    Args:
        element: The (potential) descendant Tag.
        ancestor: The (potential) ancestor Tag.

    Returns:
        *True* if *ancestor* is in the parents chain of *element*.
    """
    for p in element.parents:
        if p is ancestor:
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════

def search_videos(query: str) -> list[VideoResult]:
    """Search hanime1.me for videos matching *query*.

    Args:
        query: Search keyword(s).

    Returns:
        Matching VideoResult objects, or an empty list on failure.
    """
    scraper = create_scraper()
    url = f"{SEARCH_URL}?query={query}&type="

    try:
        resp = scraper.get(url, timeout=20)
    except Exception:
        return []

    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[VideoResult] = []

    for el in soup.select(".video-item-container"):
        link = el.select_one("a.video-link")
        href = link.get("href", "") if link else ""

        # Filter out erolabs / erodalabs ads
        if "erolabs" in href.lower() or "erodalabs" in href.lower():
            continue

        video = _parse_video_item(el)
        if video is not None:
            results.append(video)

    return results


def extract_video_detail(video_url: str) -> VideoDetail | None:
    """Scrape a video page for sources, tags, and metadata.

    Args:
        video_url: Full URL of the video detail page.

    Returns:
        VideoDetail with title, poster, sources (sorted by resolution
        high→low), content-area tags, and page_url, or *None* if the
        request fails.
    """
    scraper = create_scraper()

    try:
        resp = scraper.get(video_url, timeout=20)
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Title ──
    h1_el = soup.select_one("h1")
    title_el = soup.select_one("title")
    title = ""
    if h1_el:
        title = h1_el.get_text(strip=True)
    elif title_el:
        title = title_el.get_text(strip=True)
    title = _clean_title(title)

    # ── Video player & sources ──
    player = soup.select_one("video#player")
    poster: str = player.get("poster", "") if player else ""

    sources: list[tuple[str, str]] = []
    if player:
        for s in player.select("source"):
            size = s.get("size", "?")
            src = s.get("src", "")
            if src:
                sources.append((f"{size}p", src))

    # Sort by resolution (high to low)
    sources.sort(key=lambda x: int(x[0].replace("p", "")), reverse=True)

    # ── Tags (content area only) ──
    content_root = _find_content_root(soup)
    tags = _extract_tags_from_page(soup, content_root=content_root)

    return VideoDetail(
        title=title,
        poster=poster,
        sources=sources,
        tags=tags,
        page_url=video_url,
    )


def fetch_homepage() -> HomePageData | None:
    """Scrape the hanime1.me homepage and parse every section.

    Extracts:
        * Banner (featured video + tags)
        * Category rows (name, url, and up to ~12 videos each)
        * Genre quick links from the navigation bar

    Returns:
        HomePageData, or *None* if the homepage request fails.
    """
    scraper = create_scraper()

    try:
        resp = scraper.get(BASE_URL, timeout=30)
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1. Banner
    banner_video, banner_tags = _parse_banner(soup)

    # 2. Category rows
    categories = _parse_category_rows(soup)

    # 3. Genre quick links
    genres = _parse_nav_genres(soup)

    return HomePageData(
        banner=banner_video,
        banner_tags=banner_tags,
        categories=categories,
        genres=genres,
    )


def download_thumbnail(url: str) -> bytes | None:
    """Download an image URL and return the raw bytes.

    Args:
        url: Image URL (thumbnail, poster, etc.).

    Returns:
        Raw image *bytes*, or *None* on any failure.
    """
    if not url:
        return None

    scraper = create_scraper()

    try:
        resp = scraper.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.content
        return None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# Internal — Homepage Section Parsers
# ══════════════════════════════════════════════════════════════════

def _parse_banner(soup: BeautifulSoup) -> tuple[VideoResult | None, list[str]]:
    """Parse the hero banner (``#home-banner-wrapper``).

    Args:
        soup: Parsed BeautifulSoup of the homepage.

    Returns:
        ``(banner_video, banner_tags)`` — a VideoResult for the
        featured video and a list of tag-name strings.
    """
    banner = soup.select_one("#home-banner-wrapper")
    if not banner:
        return None, []

    # Title
    title_el = banner.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    # Subtitle (author / views / time)
    subtitle_el = banner.select_one("h4.hidden-xs")
    subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""

    # Tag spans
    tags: list[str] = []
    tag_container = banner.select_one("div.hidden-sm.hidden-md")
    if tag_container:
        for span in tag_container.select("span"):
            tag_text = span.get_text(strip=True)
            if tag_text:
                tags.append(tag_text)

    # Play link
    play_btn = banner.select_one("a.home-banner-play-btn")
    play_url = play_btn.get("href", "") if play_btn else ""
    if play_url and not play_url.startswith("http"):
        play_url = BASE_URL + play_url

    # Thumbnail — try <img> first, then background-image in style
    thumbnail = ""
    banner_img = banner.select_one("img")
    if banner_img:
        thumbnail = banner_img.get("src", "")
    if not thumbnail:
        style = banner.get("style", "")
        bg_match = re.search(r'url\(["\']?([^"\')\s]+)["\']?\)', style)
        if bg_match:
            thumbnail = bg_match.group(1)
            if thumbnail and not thumbnail.startswith("http"):
                thumbnail = BASE_URL + thumbnail

    video = VideoResult(
        title=title,
        url=play_url,
        thumbnail=thumbnail,
        author=subtitle,
    )

    return video, tags


def _parse_category_rows(soup: BeautifulSoup) -> list[Category]:
    """Parse the video category rows (``#home-rows-wrapper``).

    Each row consists of an ``<a class="horizontal-row-title">``
    (category header) followed by a ``<div>`` of ``.video-item-container``
    elements.

    Args:
        soup: Parsed BeautifulSoup of the homepage.

    Returns:
        List of Category objects (one per category row).
    """
    categories: list[Category] = []

    home_rows = soup.select_one("#home-rows-wrapper")
    if not home_rows:
        return categories

    current_category: Category | None = None

    for child in home_rows.children:
        # Skip text / whitespace nodes
        if not hasattr(child, "name") or child.name is None:
            continue

        if child.name == "a" and "horizontal-row-title" in child.get("class", []):
            # Category header link
            raw_text = child.get_text(strip=True)
            # Strip "查看更多arrow_forward_ios" suffix
            category_name = raw_text.replace("查看更多arrow_forward_ios", "").strip()
            category_url = child.get("href", "")
            if category_url and not category_url.startswith("http"):
                category_url = BASE_URL + category_url

            current_category = Category(
                name=category_name,
                url=category_url,
                videos=[],
            )
            categories.append(current_category)

        elif child.name == "div" and current_category is not None:
            # Video items container
            for vi in child.select(".video-item-container"):
                info = _parse_video_item(vi)
                if info is not None:
                    current_category.videos.append(info)

    return categories


def _parse_nav_genres(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Extract genre quick links from the desktop and mobile navigation.

    Sources: ``#main-nav-home a[href]`` and ``#sub-nav-home-mobile a[href]``.

    Args:
        soup: Parsed BeautifulSoup of the homepage.

    Returns:
        ``[(genre_name, absolute_url), ...]`` — deduplicated, without
        "查看更多" placeholder links.
    """
    genres: list[tuple[str, str]] = []
    seen: set[str] = set()

    selector = "#main-nav-home a[href], #sub-nav-home-mobile a[href]"
    for a in soup.select(selector):
        href = a.get("href", "")
        if "/search?genre=" not in href:
            continue

        text = a.get_text(strip=True)
        # Skip "查看更多" (show more) placeholder links and duplicates
        if not text or "查看更多" in text or text in seen:
            continue

        seen.add(text)
        abs_url = href if href.startswith("http") else BASE_URL + href
        genres.append((text, abs_url))

    return genres
