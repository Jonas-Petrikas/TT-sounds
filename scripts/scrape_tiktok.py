"""
Scrape TikTok trending audios from Tokchart's public chart pages.

Output: data/trending.json

This version parses the current Tokchart sound rows instead of relying on the
old /img/{id}/600 URL and old Tailwind class names. It uses Python's standard
library only (no BeautifulSoup dependency).

Important:
- Tokchart's public chart page may expose only a limited number of rows.
- If Tokchart changes its markup again, the raw HTML and parser diagnostics
  are saved under debug/ so the selectors can be adjusted.
- If every page returns zero sounds, the previous trending.json is preserved.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "trending.json"
DEBUG_DIR = ROOT / "debug"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Keep the same periods and labels so the existing dashboard keeps working.
LENSES = [
    {
        "period_days": 7,
        "label": "Trending in US",
        "url": "https://tokchart.com/trending/US",
    },
    {
        "period_days": 30,
        "label": "Fastest growing",
        "url": "https://tokchart.com/growing",
    },
]


class Node:
    """Small HTML tree node used to inspect rows without external packages."""

    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.parent = parent
        self.children = []


class TreeParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # HTML can contain imperfect nesting; unwind to the matching tag.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data and data.strip():
            self.stack[-1].children.append(data)


def walk(node):
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from walk(child)


def text_content(node):
    parts = []

    def collect(item):
        for child in item.children:
            if isinstance(child, Node):
                collect(child)
            elif isinstance(child, str):
                parts.append(child)

    collect(node)
    return re.sub(r"\s+", " ", unescape(" ".join(parts))).strip()


def classes(node):
    return set((node.attrs.get("class") or "").split())


def has_class(node, *names):
    node_classes = classes(node)
    return any(name in node_classes for name in names)


def descendants_with_class(node, *names):
    return [item for item in walk(node) if has_class(item, *names)]


def first_text_by_class(node, *names):
    matches = descendants_with_class(node, *names)
    for item in matches:
        value = text_content(item)
        if value:
            return value
    return ""


def parse_compact_number(value):
    """Convert '564K', '1.2M', '24,200' to an integer."""
    if not value:
        return 0

    value = value.strip().replace(",", "").replace(" ", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([KMB])?", value, re.I)
    if not match:
        return 0

    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def extract_clip_id(href):
    path = urlparse(href).path
    # Current Tokchart sound URLs look like /sounds/{slug}_{numeric-id}.
    match = re.search(r"(\d{8,})(?:/)?$", path)
    if match:
        return match.group(1)

    # Backward compatibility with the old dashboard/sounds/{id} URL.
    match = re.search(r"/dashboard/sounds/(\d+)", path)
    return match.group(1) if match else ""


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        body = response.read()
        final_url = response.geturl()
        html = body.decode("utf-8", errors="replace")
        return html, final_url


def find_row_for_anchor(anchor):
    """
    Find the smallest useful sound-row container around a Tokchart sound link.
    Prefer semantic row wrappers, then table rows, then the nearest ancestor
    containing the current Tokchart row fields.
    """
    ancestors = []
    current = anchor
    for _ in range(7):
        if current is None:
            break
        ancestors.append(current)
        current = current.parent

    # Explicit row wrappers used by Tokchart's newer chart markup.
    for node in ancestors:
        if has_class(node, "ch-row", "sound-row", "audio-row", "chart-row"):
            return node

    # A sound may be represented as a normal table row.
    for node in ancestors:
        if node.tag in ("tr", "li"):
            return node

    # New chart rows have used these field classes.
    for node in ancestors:
        found_title = bool(descendants_with_class(node, "ch-t"))
        found_artist = bool(descendants_with_class(node, "ui-a"))
        found_growth = bool(descendants_with_class(node, "ch-big"))
        found_image = any(item.tag == "img" for item in walk(node))
        if found_title and (found_artist or found_growth or found_image):
            return node

    # Some versions make the whole row a clickable anchor.
    for node in ancestors:
        if has_class(node, "ch-row-link"):
            return node

    return anchor.parent or anchor


def get_image_url(row, base_url):
    for item in walk(row):
        if item.tag != "img":
            continue

        src = item.attrs.get("src") or item.attrs.get("data-src") or ""
        if not src:
            srcset = item.attrs.get("srcset", "")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]

        if src and not src.startswith("data:"):
            return urljoin(base_url, src)

    return ""


def get_tiktok_url(row, clip_id):
    for item in walk(row):
        if item.tag != "a":
            continue
        href = item.attrs.get("href", "")
        if "tiktok.com/" in href and ("/music/" in href or "/video/" in href):
            return href
    return f"https://www.tiktok.com/music/-{clip_id}"


def get_cells(row):
    return [text_content(item) for item in walk(row) if item.tag in ("td", "th")]


def parse_row(anchor, row, period, base_url):
    href = urljoin(base_url, anchor.attrs.get("href", ""))
    clip_id = extract_clip_id(href)
    if not clip_id:
        return None

    title = first_text_by_class(row, "ch-t")
    if not title:
        title = text_content(anchor)

    artist = first_text_by_class(row, "ui-a")
    growth_text = first_text_by_class(row, "ch-big")

    # The current chart shows "videos/day" and total videos in separate cells.
    # Use semantic classes first, then the last table cells as a fallback.
    usage = 0
    growth = parse_compact_number(growth_text)

    for cls in ("ch-uses", "ch-total", "ch-count", "ch-videos"):
        candidate = first_text_by_class(row, cls)
        if candidate:
            usage = parse_compact_number(candidate)
            if usage:
                break

    cells = get_cells(row)
    if cells:
        numeric_cells = [
            parse_compact_number(cell)
            for cell in cells
            if re.search(r"\d", cell)
        ]
        numeric_cells = [number for number in numeric_cells if number > 0]

        if not growth:
            # Most chart rows begin with rank, then videos/day, then total uses.
            # Skip the first number when it is the rank column.
            if len(numeric_cells) >= 2:
                growth = numeric_cells[1]
            elif numeric_cells:
                growth = numeric_cells[0]

        if not usage and len(numeric_cells) >= 2:
            usage = numeric_cells[-1]

    # Last-resort growth extraction from visible row text.
    row_text = text_content(row)
    if not growth:
        match = re.search(r"\+\s*([\d,.]+\s*[KMB]?)", row_text, re.I)
        if match:
            growth = parse_compact_number(match.group(1))

    if not title:
        return None

    cover = get_image_url(row, base_url)
    tiktok_link = get_tiktok_url(row, clip_id)

    return {
        "rank": 0,
        "title": title,
        "artist": artist,
        "clip_id": clip_id,
        "tiktok_uses": usage,
        "tiktok_uses_24h": growth,
        "tiktok_link": tiktok_link,
        "cover": cover,
        "period_days": period,
        # Useful when inspecting scraper output; existing dashboard can ignore it.
        "tokchart_url": href,
    }


def parse(html, period, base_url="https://tokchart.com/"):
    parser = TreeParser()
    parser.feed(html)

    anchors = [
        node for node in walk(parser.root)
        if node.tag == "a"
        and "/sounds/" in (node.attrs.get("href") or "")
    ]

    rows = []
    seen = set()

    for anchor in anchors:
        row_node = find_row_for_anchor(anchor)
        row = parse_row(anchor, row_node, period, base_url)
        if not row or row["clip_id"] in seen:
            continue
        seen.add(row["clip_id"])
        row["rank"] = len(rows) + 1
        rows.append(row)

    return rows


def main():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    all_rows = []
    notes = []

    for lens in LENSES:
        label = lens["label"]
        period = lens["period_days"]
        print(f"[scrape] {label} <- {lens['url']}", flush=True)

        try:
            html, final_url = fetch(lens["url"])
            raw_path = DEBUG_DIR / f"raw_{period}.html"
            raw_path.write_text(html[:1_000_000], encoding="utf-8")

            rows = parse(html, period, final_url)
            note = {
                "lens": label,
                "requested_url": lens["url"],
                "final_url": final_url,
                "rows": len(rows),
                "html_bytes": len(html.encode("utf-8")),
                "raw_html": str(raw_path.relative_to(ROOT)),
            }

            if not rows:
                # Add lightweight clues to the Actions log for the next fix.
                note["sound_anchor_count"] = html.count("/sounds/")
                note["has_ch_row_link"] = "ch-row-link" in html
                note["has_ch_t"] = "ch-t" in html
                note["has_ui_a"] = "ui-a" in html
                note["diagnostic"] = (
                    "Page downloaded, but no sound rows matched. "
                    "Inspect the saved raw HTML and update parse_row/find_row_for_anchor."
                )

        except Exception as exc:
            print(f"  ERROR: {exc}", flush=True)
            rows = []
            note = {"lens": label, "error": str(exc)}

        print(f"  {len(rows)} sounds", flush=True)
        notes.append(note)
        all_rows.extend(rows)

    summary = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "runs": notes,
        "total_rows": len(all_rows),
    }
    (DEBUG_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Never replace a previously good data file with an empty result.
    if not all_rows:
        print(
            "[error] All Tokchart pages returned zero sounds. "
            "Keeping the existing data/trending.json unchanged.",
            file=sys.stderr,
        )
        return 1

    payload = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "region": "US",
        "source": "tokchart.com",
        "count": len(all_rows),
        "lenses": [
            {"period_days": item["period_days"], "label": item["label"]}
            for item in LENSES
        ],
        "sounds": all_rows,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[done] wrote {len(all_rows)} sounds -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
