from __future__ import annotations
import discord
from discord.ext import commands, tasks
import aiohttp
import xml.etree.ElementTree as ET
import json
import os
import re
import html
import difflib
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

# CONFIG
STATE_FILE = "rss_state.json"
LOG_FILE = "rss_feed_errors.log"
MAX_SEEN_PER_SOURCE = 300
REQUEST_TIMEOUT = 15

# Dedicated file (gitignored, same deployment-runtime-state treatment as
# rss_state.json) so failures - "which source, which URL/entry, what
# exception" - survive a restart and don't get lost in console scrollback.
# Also propagates to the root logger discord.py's bot.run() sets up, so it
# still shows in the console/journal exactly like the old print() calls did.
logger = logging.getLogger("scnewsbot.rss_feed")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(_file_handler)
DESCRIPTION_LIMIT = 4000  # Discord embed description hard cap is 4096
FIELD_VALUE_LIMIT = 1000  # Discord embed field value hard cap is 1024
MAX_SECTION_FIELDS = 10
EMBED_TOTAL_BUDGET = 5000  # stay well under Discord's 6000-char whole-embed cap

# Comm-Link and Announcements both mirror the same RSI news, so the same
# story sometimes shows up in both feeds worded slightly differently. Skip a
# second post if a near-identical title from the other feed was already
# posted recently.
CROSS_POST_SOURCES = {"comm_link", "spectrum_announcements"}
DUPLICATE_WINDOW_HOURS = 72
DUPLICATE_TITLE_SIMILARITY = 0.85

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

PATCH_NOTES_COLOR = discord.Color.gold()
PTU_PATCH_NOTES_COLOR = discord.Color.dark_orange()
ANNOUNCEMENT_COLOR = discord.Color.blurple()

# Comm-Link lumps everything into two Atom categories (Video/Post), which is
# too coarse to tell a weekly recap from a Q&A from a ship reveal at a glance
# in Discord. Classify by title pattern instead so each recurring series gets
# its own sidebar color.
COMM_LINK_CATEGORY_RULES: list[tuple[re.Pattern, discord.Color]] = [
    (re.compile(r"^This Week in Star Citizen", re.I), discord.Color.blue()),
    (re.compile(r"^Roadmap Roundup", re.I), discord.Color.orange()),
    (re.compile(r"Monthly Report", re.I), discord.Color.dark_gold()),
    (re.compile(r"^Q&A", re.I), discord.Color.purple()),
    (re.compile(r"Jump Point", re.I), discord.Color.magenta()),
    (re.compile(r"Alpha \d+\.\d+", re.I), discord.Color.gold()),
]
COMM_LINK_VIDEO_COLOR = discord.Color.red()
COMM_LINK_DEFAULT_COLOR = discord.Color.teal()

TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    return TITLE_NORMALIZE_RE.sub(" ", title.lower()).strip()


TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']')
IFRAME_SRC_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
YOUTUBE_EMBED_ID_RE = re.compile(r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]+)", re.I)

# Block-level tags get converted to Markdown before remaining inline tags are
# stripped, so bullet lists and headings survive instead of collapsing into
# one run-on line.
LIST_ITEM_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I)
HEADING_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.S | re.I)
PARAGRAPH_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
BREAK_RE = re.compile(r"<br\s*/?>", re.I)
BLANK_LINES_RE = re.compile(r"\n{3,}")
LOOSE_BULLET_RE = re.compile(r"\n{2,}(➣ )")
SECTION_SPLIT_RE = re.compile(r"\n\n\*\*(.+?)\*\*\n")

# RSI's own site (Comm-Link, Patch Notes) renders articles through an
# internal CMS ("Alexandria"): the initial page is just a shell with a
# <script> that fetches this S3 URL client-side, and that response holds the
# real content as HTML embedded in <g-article body="..."> attributes. Both
# requests are plain, unauthenticated GETs - verified directly against
# multiple Patch Notes and Comm-Link articles.
ALEXANDRIA_S3_URL_RE = re.compile(r"const s3Url\s*=\s*'([^']+)'")
ALEXANDRIA_ARTICLE_TAG_RE = re.compile(r'<g-article\b((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*/?>', re.S)
ALEXANDRIA_BODY_ATTR_RE = re.compile(r'\bbody="([^"]*)"')

PATCH_NOTES_ENTRY_RE = re.compile(
    r'href="(/comm-link/Patch-Notes/(\d+)-[^"]+)"[^>]*>.*?<div class="title[^"]*">(.*?)</div>',
    re.S,
)

# Spectrum forum listing pages embed the full thread list as JSON-LD
# (CollectionPage -> ItemList) directly in the static HTML - no login or JS
# execution needed, unlike the individual Comm-Link Patch-Notes articles.
LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)

# Spectrum's articleBody is plain text with no HTML/newlines - the only
# structural signal left after flattening is runs of 3+ spaces where a list
# item or section break used to be. Recognized section titles get grouped as
# their own field; everything else becomes a bullet line.
SPECTRUM_SECTION_RE = re.compile(r"\s{3,}")
SPECTRUM_SECTION_HEADERS = {
    "Known Issues", "Features & Updates", "Feature Updates",
    "Additional Updates", "Bug Fixes & Technical", "Bug Fixes",
}
# Headers don't always sit in their own 3+-space-padded run - they're
# sometimes glued directly onto the end of the previous bullet or the start
# of the next one with only a single space. Longest-first so multi-word
# headers ("Bug Fixes & Technical") match before their shorter substrings
# ("Bug Fixes") would.
SPECTRUM_HEADER_RE = re.compile(
    r"(?<!\S)(" + "|".join(re.escape(h) for h in sorted(SPECTRUM_SECTION_HEADERS, key=len, reverse=True)) + r")(?!\S)"
)

# The JSON-LD articleBody above is a last-resort fallback. Spectrum's own
# React app fetches the thread from this API as structured Draft.js content
# (real block types: header-one, unordered-list-item, etc.) - hitting it
# directly gets us actual bullet/heading structure instead of reconstructing
# it from flattened plain text. No auth needed; verified via a plain POST.
SPECTRUM_API_THREAD_URL = "https://robertsspaceindustries.com/api/spectrum/forum/thread/nested"
SPECTRUM_THREAD_URL_RE = re.compile(r"/forum/(\d+)/thread/([^/?#]+)")

DRAFT_HEADER_TYPES = {f"header-{n}" for n in ("one", "two", "three", "four", "five", "six")}
DRAFT_STYLE_MARKERS = {"BOLD": "**", "ITALIC": "*", "UNDERLINE": "__"}

# STATE
class FeedState:
    def __init__(self, filepath=STATE_FILE):
        self.filepath = filepath
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)

    def source(self, name: str) -> dict:
        return self.data.setdefault(name, {"initialized": False, "seen_ids": []})

    def mark_seen(self, name: str, entry_id: str):
        source = self.source(name)
        source["seen_ids"].append(entry_id)
        source["seen_ids"] = source["seen_ids"][-MAX_SEEN_PER_SOURCE:]

    def is_seen(self, name: str, entry_id: str) -> bool:
        return entry_id in self.source(name)["seen_ids"]

    def mark_initialized(self, name: str):
        self.source(name)["initialized"] = True

    def record_post(self, source: str, title: str):
        """Log a posted title with a timestamp so later cross-feed duplicate
        checks have something to compare against."""
        posts = self.data.setdefault("_recent_posts", [])
        now = datetime.now(timezone.utc).timestamp()
        posts.append({"source": source, "title": title, "ts": now})
        cutoff = now - DUPLICATE_WINDOW_HOURS * 3600
        self.data["_recent_posts"] = [p for p in posts if p["ts"] >= cutoff]

    def is_duplicate_title(self, source: str, title: str) -> bool:
        """True if a near-identical title was already posted recently under a
        different feed that's known to overlap with this one."""
        if source not in CROSS_POST_SOURCES:
            return False

        cutoff = datetime.now(timezone.utc).timestamp() - DUPLICATE_WINDOW_HOURS * 3600
        normalized = normalize_title(title)
        for post in self.data.get("_recent_posts", []):
            if post["ts"] < cutoff:
                continue
            if post["source"] == source or post["source"] not in CROSS_POST_SOURCES:
                continue
            other_normalized = normalize_title(post["title"])
            if normalized == other_normalized:
                return True
            ratio = difflib.SequenceMatcher(None, normalized, other_normalized).ratio()
            if ratio >= DUPLICATE_TITLE_SIMILARITY:
                return True

        return False


# ENTRY MODEL
class FeedEntry:
    def __init__(self, entry_id: str, title: str, link: str, published: datetime | None, summary_html: str, category: str | None):
        self.id = entry_id
        self.title = title
        self.link = link
        self.published = published
        self.summary_html = summary_html
        self.category = category

    @staticmethod
    def clean_text(raw_html: str, limit: int = 350) -> str:
        text = html.unescape(TAG_RE.sub(" ", raw_html or ""))
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0] + "..."
        return text

    @staticmethod
    def to_markdown(raw_html: str, limit: int | None = DESCRIPTION_LIMIT) -> str:
        """Convert RSI's post HTML to Discord markdown, preserving headings and
        bullet lists instead of flattening everything into one line. Pass
        limit=None to skip truncation (e.g. when the caller is about to split
        the result into sections and truncate each one individually)."""
        text = raw_html or ""
        def _heading(m: re.Match) -> str:
            inner = BREAK_RE.sub("", m.group(1))  # e.g. stray <h2><br></h2> separators
            return f"\n\n**{inner}**\n" if TAG_RE.sub("", inner).strip() else ""

        text = LIST_ITEM_RE.sub(lambda m: f"\n➣ {m.group(1)}", text)
        text = HEADING_RE.sub(_heading, text)
        text = PARAGRAPH_RE.sub(lambda m: f"\n\n{m.group(1)}\n", text)
        text = BREAK_RE.sub("\n", text)
        text = TAG_RE.sub("", text)
        text = html.unescape(text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = BLANK_LINES_RE.sub("\n\n", text)
        text = LOOSE_BULLET_RE.sub(r"\n\1", text).strip()

        if limit is not None and len(text) > limit:
            text = text[:limit].rsplit("\n", 1)[0].rsplit(" ", 1)[0]

        return text or "(no content)"

    @staticmethod
    def html_to_sections(raw_html: str) -> list[tuple[str | None, str]]:
        """Split HTML into (header, body) sections at heading boundaries, by
        reusing to_markdown()'s conversion and then splitting its **Header**
        markers back out - lets long articles (Patch Notes, Comm-Link posts)
        render as scannable embed fields instead of one giant description."""
        text = FeedEntry.to_markdown(raw_html, limit=None)
        if text == "(no content)":
            return []

        # to_markdown() strips leading whitespace, so an article that opens
        # directly with a heading (no intro paragraph) would otherwise lose
        # the "\n\n" the split pattern needs and leave that first heading
        # stuck as literal **bold** text inside the body instead of becoming
        # its own section.
        parts = SECTION_SPLIT_RE.split("\n\n" + text)
        sections: list[tuple[str | None, str]] = []
        if parts[0].strip():
            sections.append((None, parts[0].strip()))
        for i in range(1, len(parts), 2):
            header = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if body:
                sections.append((header, body))
        return sections

    @staticmethod
    def spectrum_to_sections(article_body: str) -> list[tuple[str | None, str]]:
        """Spectrum's articleBody is plain text with the original list/section
        structure collapsed into runs of whitespace. Group the recovered
        segments under their section headers (Known Issues, Bug Fixes, etc.)
        instead of flattening everything into one long bullet dump - the
        caller renders each group as its own embed field so the post reads as
        a scannable card rather than a wall of text."""
        # Force a break around every recognized header regardless of how much
        # whitespace surrounds it in the source, so a glued-on header doesn't
        # get swallowed into (and truncate) the previous bullet's text.
        padded = SPECTRUM_HEADER_RE.sub(lambda m: f"   {m.group(1)}   ", article_body or "")
        segments = [s.strip() for s in SPECTRUM_SECTION_RE.split(padded) if s.strip()]

        sections: list[tuple[str | None, list[str]]] = []
        for seg in segments:
            if seg in SPECTRUM_SECTION_HEADERS:
                sections.append((seg, []))
            elif sections:
                sections[-1][1].append(seg)
            else:
                sections.append((None, [seg]))

        # Left untruncated here - the caller knows whether a given section
        # will end up as the embed description (4000-char budget) or a field
        # (1024-char budget) and truncates accordingly.
        return [(header, "\n".join(f"➣ {b}" for b in bullets)) for header, bullets in sections if bullets]

    def first_image(self) -> str | None:
        match = IMG_SRC_RE.search(self.summary_html or "")
        return match.group(1) if match else None

    def youtube_video_id(self) -> str | None:
        iframe_src = IFRAME_SRC_RE.search(self.summary_html or "")
        if not iframe_src:
            return None
        match = YOUTUBE_EMBED_ID_RE.search(iframe_src.group(1))
        return match.group(1) if match else None

    def comm_link_color(self) -> discord.Color:
        if self.category == "Video":
            return COMM_LINK_VIDEO_COLOR
        for pattern, color in COMM_LINK_CATEGORY_RULES:
            if pattern.search(self.title):
                return color
        return COMM_LINK_DEFAULT_COLOR


def parse_atom_feed(raw_xml: str) -> list[FeedEntry]:
    root = ET.fromstring(raw_xml)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", default="(untitled)", namespaces=ATOM_NS)

        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else None

        published_raw = entry.findtext("atom:published", default=None, namespaces=ATOM_NS)
        published = None
        if published_raw:
            try:
                published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            except ValueError:
                published = None

        summary = entry.findtext("atom:summary", default=None, namespaces=ATOM_NS)
        content = entry.findtext("atom:content", default=None, namespaces=ATOM_NS)
        # <content> carries the full article body; <summary> is often just a
        # one-line teaser, so prefer content when both are present.
        summary_html = content or summary or ""

        category_el = entry.find("atom:category", ATOM_NS)
        category = category_el.get("term") if category_el is not None else None

        if entry_id and link:
            entries.append(FeedEntry(entry_id, title.strip(), link, published, summary_html, category))

    return entries


def parse_patch_notes_page(raw_html: str) -> list[FeedEntry]:
    """Parse RSI's official /patch-notes listing page (plain server-rendered
    HTML - no JS/API needed). Returns entries oldest-first, matching the
    ordering the atom-based sources use."""
    entries = []
    for href, entry_id, title in PATCH_NOTES_ENTRY_RE.findall(raw_html):
        link = f"https://robertsspaceindustries.com{href}"
        clean_title = html.unescape(TAG_RE.sub("", title)).strip()
        entries.append(FeedEntry(entry_id, clean_title, link, None, "", "Patch Notes"))

    entries.reverse()  # page lists newest first; we want oldest-first
    return entries


def extract_alexandria_s3_url(raw_html: str) -> str | None:
    match = ALEXANDRIA_S3_URL_RE.search(raw_html)
    return match.group(1) if match else None


def parse_alexandria_article_html(raw_html: str) -> str:
    """Concatenate every <g-article body="..."> block's HTML, in document
    order, to reconstruct the full article body from an Alexandria S3
    response. Attribute parsing (not a naive regex up to the next '>') is
    required here since the body HTML itself contains unescaped '>'
    characters from tags like <p>."""
    bodies = []
    for tag_match in ALEXANDRIA_ARTICLE_TAG_RE.finditer(raw_html):
        body_match = ALEXANDRIA_BODY_ATTR_RE.search(tag_match.group(1))
        if body_match:
            bodies.append(html.unescape(body_match.group(1)))
    return "".join(bodies)


def parse_spectrum_forum_listing(raw_html: str, category: str | None = None) -> list[FeedEntry]:
    """Parse a Spectrum forum listing page (e.g. the Patch Notes or
    Announcements subforums). The thread list is embedded as JSON-LD
    (CollectionPage -> ItemList), so no auth/JS is needed - just the
    per-thread title, URL, and publish date (body text isn't included here,
    only on the individual thread page)."""
    entries = []
    for block in LD_JSON_RE.findall(raw_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if data.get("@type") != "CollectionPage":
            continue

        items = data.get("mainEntity", {}).get("itemListElement", [])
        for list_item in items:
            item = list_item.get("item", {})
            link = item.get("url")
            title = item.get("name")
            if not link or not title:
                continue

            published = None
            published_raw = item.get("datePublished")
            if published_raw:
                try:
                    published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                except ValueError:
                    published = None

            entries.append(FeedEntry(link, title.strip(), link, published, "", category))
        break

    return entries


def parse_spectrum_thread_body(raw_html: str) -> str | None:
    """Parse an individual Spectrum thread page for its full post text - also
    embedded as JSON-LD (DiscussionForumPosting.articleBody), same as the
    listing page. Returns None if the post's JSON-LD block isn't found."""
    for block in LD_JSON_RE.findall(raw_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if data.get("@type") == "DiscussionForumPosting" and data.get("articleBody"):
            return data["articleBody"]
    return None


def _apply_draft_inline_styles(text: str, ranges: list[dict] | None) -> str:
    """Insert Discord markdown markers for a Draft.js block's
    inlineStyleRanges (character offsets into the plain text). Insert from
    the rightmost position first so earlier offsets stay valid as the string
    grows."""
    markers = []
    for r in ranges or []:
        marker = DRAFT_STYLE_MARKERS.get(r.get("style"))
        if not marker:
            continue
        start = r.get("offset", 0)
        end = start + r.get("length", 0)
        if 0 <= start <= end <= len(text):
            markers.append((start, marker))
            markers.append((end, marker))

    if not markers:
        return text

    markers.sort(key=lambda m: m[0], reverse=True)
    chars = list(text)
    for pos, marker in markers:
        chars.insert(pos, marker)
    return "".join(chars)


def _draft_block_is_header(block: dict) -> bool:
    """A block is a section boundary if it's an explicit Draft.js heading, or
    a plain paragraph whose *entire* text is bold+underlined - Spectrum's
    editor uses that combo for sub-headers (Known Issues, Additional
    Updates, ASOP Terminal, etc.) that never got real heading markup."""
    block_type = block.get("type", "")
    if block_type in DRAFT_HEADER_TYPES:
        return True
    if block_type != "unstyled":
        return False

    text = block.get("text", "")
    if not text.strip():
        return False

    styles = {
        r.get("style") for r in block.get("inlineStyleRanges", [])
        if r.get("offset") == 0 and r.get("length") == len(text)
    }
    return {"BOLD", "UNDERLINE"}.issubset(styles)


def parse_spectrum_thread_api(payload: dict) -> tuple[list[tuple[str | None, str]], str | None]:
    """Parse the JSON payload from Spectrum's own thread API - the same
    structured Draft.js content its React app renders client-side, so
    headings and bullet lists come through as real block types instead of
    being guessed back out of flattened plain text."""
    content_blocks = (payload or {}).get("content_blocks", [])

    image_url = None
    lines: list[tuple[str, str]] = []  # (kind, text), kind in {heading, bullet, para}
    code_buffer: list[str] = []

    def flush_code():
        if code_buffer:
            lines.append(("para", "```\n" + "\n".join(code_buffer) + "\n```"))
            code_buffer.clear()

    for cb in content_blocks:
        if cb.get("type") == "image":
            if image_url is None:
                for item in cb.get("data") or []:
                    url = (item.get("data") or {}).get("url")
                    if url:
                        image_url = url
                        break
            continue

        if cb.get("type") != "text":
            continue

        for block in cb.get("data", {}).get("blocks", []):
            block_type = block.get("type", "unstyled")
            text = block.get("text", "")

            if block_type == "code-block":
                if text.strip():
                    code_buffer.append(text.strip())
                continue
            flush_code()

            if not text.strip():
                continue

            if _draft_block_is_header(block):
                lines.append(("heading", " ".join(text.split())))
                continue

            styled = " ".join(_apply_draft_inline_styles(text, block.get("inlineStyleRanges")).split())

            if block_type in ("unordered-list-item", "ordered-list-item"):
                # Draft.js nests sub-list items via "depth" rather than a
                # different block type - use the indented ✦ marker (matching
                # the announcements builder's own bullet convention) for
                # anything nested under a top-level ➣ item.
                depth = block.get("depth") or 0
                marker = "    ✦" if depth > 0 else "➣"
                lines.append(("bullet", f"{marker} {styled}"))
            elif block_type == "blockquote":
                lines.append(("bullet", f"> {styled}"))
            else:
                lines.append(("para", styled))

    flush_code()

    sections: list[list] = [[None, []]]
    for kind, text in lines:
        if kind == "heading":
            sections.append([text, []])
        else:
            sections[-1][1].append(text)

    # A header with nothing under it (e.g. two headings back to back) would
    # otherwise render as an empty field - fold its text into the previous
    # section instead of silently dropping it.
    folded: list[list] = []
    for header, body in sections:
        if header is not None and not body:
            if folded:
                folded[-1][1].append(f"**{header}**")
            else:
                folded.append([None, [f"**{header}**"]])
            continue
        folded.append([header, body])

    # Left untruncated here - the caller knows whether a given section will
    # end up as the embed description (4000-char budget) or a field
    # (1024-char budget) and truncates accordingly.
    result: list[tuple[str | None, str]] = [
        (header, "\n".join(body)) for header, body in folded if body
    ]

    return result, image_url


def _apply_section_fields(embed: discord.Embed, sections: list[tuple[str | None, str]]) -> None:
    """Add up to MAX_SECTION_FIELDS sections as separate embed fields, keeping
    each field under Discord's 1024-char field cap and the whole embed under
    its total-character budget."""
    used = len(embed.title or "") + len(embed.description or "")
    for header, text in sections[:MAX_SECTION_FIELDS]:
        remaining = min(EMBED_TOTAL_BUDGET - used, FIELD_VALUE_LIMIT)
        if remaining <= 100:
            break
        if len(text) > remaining:
            text = text[:remaining].rsplit(" ", 1)[0] + "…"
        name = f"__**{header}**__" if header else "Details"
        embed.add_field(name=name, value=text, inline=False)
        used += len(name) + len(text)


def _apply_sections(embed: discord.Embed, sections: list[tuple[str | None, str]]) -> None:
    """Render (header, text) sections onto an embed: the first reads as
    flowing intro text in the description (4000-char budget), the rest each
    become their own field so long articles stay scannable."""
    sections = list(sections or [])
    if sections:
        first_header, first_text = sections[0]
        lead = f"__**{first_header}**__\n{first_text}" if first_header else first_text
        embed.description = lead[:DESCRIPTION_LIMIT]
        sections = sections[1:]
    _apply_section_fields(embed, sections)


# COG
class RssFeed(commands.Cog, name="RSS Feed"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = FeedState()
        self.session: aiohttp.ClientSession | None = None

        interval = max(float(bot.config.rss_poll_interval_minutes), 1.0)
        self.poll_feeds.change_interval(minutes=interval)

        if bot.config.rss_enabled:
            self.poll_feeds.start()

    def cog_unload(self):
        self.poll_feeds.cancel()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (SCNewsBot RSS poller)"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
        return self.session

    async def _fetch_atom_entries(self, url: str) -> list[FeedEntry]:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw_xml = await resp.text()
        return parse_atom_feed(raw_xml)

    async def _fetch_patch_notes_entries(self, url: str) -> list[FeedEntry]:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw_html = await resp.text()
        return parse_patch_notes_page(raw_html)

    async def _fetch_alexandria_sections(self, url: str) -> list[tuple[str | None, str]]:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            page_html = await resp.text()

        s3_url = extract_alexandria_s3_url(page_html)
        if not s3_url:
            return []

        async with session.get(s3_url) as resp:
            resp.raise_for_status()
            body_html = await resp.text()

        combined = parse_alexandria_article_html(body_html)
        return FeedEntry.html_to_sections(combined) if combined else []

    async def _safe_fetch_alexandria_sections(self, url: str) -> list[tuple[str | None, str]]:
        try:
            return await self._fetch_alexandria_sections(url)
        except Exception as exc:
            logger.error("Failed to fetch Alexandria content from %r: %s", url, exc, exc_info=True)
            return []

    async def _fetch_spectrum_entries(self, url: str, category: str | None = None) -> list[FeedEntry]:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw_html = await resp.text()
        entries = parse_spectrum_forum_listing(raw_html, category)
        entries.reverse()  # listing is newest-first; we want oldest-first
        return entries

    async def _fetch_spectrum_thread_body(self, url: str) -> str | None:
        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw_html = await resp.text()
        return parse_spectrum_thread_body(raw_html)

    async def _fetch_spectrum_thread_api(self, channel_id: str, slug: str) -> dict:
        session = await self._get_session()
        payload = {"slug": slug, "channel_id": channel_id}
        async with session.post(SPECTRUM_API_THREAD_URL, json=payload) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
        if not result.get("success"):
            raise RuntimeError(f"Spectrum API returned failure: {result.get('msg')}")
        return result.get("data", {})

    async def _fetch_spectrum_thread_sections(self, url: str) -> tuple[list[tuple[str | None, str]], str | None]:
        match = SPECTRUM_THREAD_URL_RE.search(url)
        if match:
            channel_id, slug = match.group(1), match.group(2)
            try:
                payload = await self._fetch_spectrum_thread_api(channel_id, slug)
                sections, image_url = parse_spectrum_thread_api(payload)
                if sections:
                    return sections, image_url
            except Exception as exc:
                logger.warning("Spectrum API fetch failed for %r, falling back to page scrape: %s", url, exc, exc_info=True)

        # Fallback: the flattened JSON-LD text embedded in the static page.
        body = await self._fetch_spectrum_thread_body(url)
        return (FeedEntry.spectrum_to_sections(body) if body else []), None

    def _comm_link_embed(self, entry: FeedEntry) -> discord.Embed:
        embed = discord.Embed(
            title=entry.title,
            url=entry.link,
            color=entry.comm_link_color(),
            timestamp=entry.published or datetime.now(timezone.utc),
        )
        _apply_sections(embed, entry.html_to_sections(entry.summary_html))
        if not embed.description:
            embed.description = "(no content)"

        image = entry.first_image()
        video_id = entry.youtube_video_id() if not image else None
        if image:
            embed.set_image(url=image)
        elif video_id:
            embed.set_image(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
        embed.set_footer(text=f"RSI Comm-Link{f' - {entry.category}' if entry.category else ''}")
        return embed

    def _patch_notes_embed(self, entry: FeedEntry, sections: list[tuple[str | None, str]] | None = None) -> discord.Embed:
        if sections:
            embed = discord.Embed(
                title=f"🚀 {entry.title}",
                url=entry.link,
                color=PATCH_NOTES_COLOR,
                timestamp=datetime.now(timezone.utc),
            )
            _apply_sections(embed, sections)
            embed.set_footer(text="RSI Patch Notes")
            return embed

        embed = discord.Embed(
            title=f"🚀 {entry.title}",
            url=entry.link,
            description=(
                "Official patch notes are live on RSI. Click the title above for the "
                "full breakdown of feature updates, bug fixes, and known issues."
            ),
            color=PATCH_NOTES_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="RSI Patch Notes")
        return embed

    def _sectioned_embed(
        self,
        entry: FeedEntry,
        sections: list[tuple[str | None, str]] | None,
        image_url: str | None,
        emoji: str,
        color: discord.Color,
        footer_text: str,
        fallback_text: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{emoji} {entry.title}",
            url=entry.link,
            color=color,
            timestamp=entry.published or datetime.now(timezone.utc),
        )

        _apply_sections(embed, sections)
        if not embed.description and not embed.fields:
            embed.description = fallback_text

        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text=footer_text)
        return embed

    def _ptu_patch_notes_embed(
        self, entry: FeedEntry, sections: list[tuple[str | None, str]] | None = None, image_url: str | None = None
    ) -> discord.Embed:
        return self._sectioned_embed(
            entry, sections, image_url, "🧪", PTU_PATCH_NOTES_COLOR, "RSI PTU Patch Notes",
            "New PTU (test build) patch notes are up on Spectrum. Click the title above for the full breakdown.",
        )

    def _announcement_embed(
        self, entry: FeedEntry, sections: list[tuple[str | None, str]] | None = None, image_url: str | None = None
    ) -> discord.Embed:
        return self._sectioned_embed(
            entry, sections, image_url, "📢", ANNOUNCEMENT_COLOR, "RSI Announcements",
            "A new official announcement is up on Spectrum. Click the title above to read it.",
        )

    async def _post_comm_link_entry(self, channel: discord.abc.Messageable, entry: FeedEntry):
        await channel.send(embed=self._comm_link_embed(entry))
        video_id = None if entry.first_image() else entry.youtube_video_id()
        if video_id:
            # Rich embeds can't host a playable video, so drop the bare watch
            # URL as a follow-up - Discord's own unfurler turns it into one.
            await channel.send(f"https://www.youtube.com/watch?v={video_id}")

    async def _post_youtube_entry(self, channel: discord.abc.Messageable, entry: FeedEntry):
        await channel.send(f"New video from Star Citizen: **{entry.title}**\n{entry.link}")

    async def _post_patch_notes_entry(self, channel: discord.abc.Messageable, entry: FeedEntry):
        sections = await self._safe_fetch_alexandria_sections(entry.link)
        await channel.send(embed=self._patch_notes_embed(entry, sections))

    async def _post_ptu_patch_notes_entry(self, channel: discord.abc.Messageable, entry: FeedEntry):
        sections, image_url = await self._safe_fetch_thread_sections(entry.link)
        await channel.send(embed=self._ptu_patch_notes_embed(entry, sections, image_url))

    async def _post_announcement_entry(self, channel: discord.abc.Messageable, entry: FeedEntry):
        sections, image_url = await self._safe_fetch_thread_sections(entry.link)
        await channel.send(embed=self._announcement_embed(entry, sections, image_url))

    async def _safe_fetch_thread_sections(self, url: str) -> tuple[list[tuple[str | None, str]], str | None]:
        try:
            return await self._fetch_spectrum_thread_sections(url)
        except Exception as exc:
            logger.error("Failed to fetch thread sections from %r: %s", url, exc, exc_info=True)
            return [], None

    async def _poll_source(self, name: str, fetcher, channel: discord.abc.Messageable, poster) -> int:
        try:
            entries = await fetcher()
        except Exception as exc:
            logger.error("Failed to fetch source %r: %s", name, exc, exc_info=True)
            return 0

        source_state = self.state.source(name)
        first_run = not source_state["initialized"]

        # Oldest first, so if several are new they post in chronological order.
        entries.sort(key=lambda e: e.published or datetime.min.replace(tzinfo=timezone.utc))

        posted = 0
        for entry in entries:
            if self.state.is_seen(name, entry.id):
                continue

            if not first_run:
                if self.state.is_duplicate_title(name, entry.title):
                    logger.info("Skipping likely duplicate cross-post from %r: %r", name, entry.title)
                    self.state.mark_seen(name, entry.id)
                    continue

                try:
                    await poster(channel, entry)
                    posted += 1
                except Exception as exc:
                    logger.error(
                        "Failed to post entry from source %r (entry id=%r, title=%r, link=%r): %s",
                        name, entry.id, entry.title, entry.link, exc, exc_info=True,
                    )
                    continue

                self.state.record_post(name, entry.title)

            self.state.mark_seen(name, entry.id)

        self.state.mark_initialized(name)
        self.state.save()
        return posted

    @tasks.loop(minutes=10)
    async def poll_feeds(self):
        config = self.bot.config
        channel_id = config.rss_post_channel
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.error("Configured post channel %s not found/visible.", channel_id)
            return

        if config.rss_comm_link_feed_url:
            url = config.rss_comm_link_feed_url
            await self._poll_source("comm_link", lambda: self._fetch_atom_entries(url), channel, self._post_comm_link_entry)

        if config.rss_youtube_feed_url:
            url = config.rss_youtube_feed_url
            await self._poll_source("youtube", lambda: self._fetch_atom_entries(url), channel, self._post_youtube_entry)

        if config.rss_patch_notes_url:
            url = config.rss_patch_notes_url
            await self._poll_source("patch_notes", lambda: self._fetch_patch_notes_entries(url), channel, self._post_patch_notes_entry)

        if config.rss_ptu_patch_notes_url:
            url = config.rss_ptu_patch_notes_url
            await self._poll_source("ptu_patch_notes", lambda: self._fetch_spectrum_entries(url), channel, self._post_ptu_patch_notes_entry)

        if config.rss_announcements_url:
            url = config.rss_announcements_url
            await self._poll_source("spectrum_announcements", lambda: self._fetch_spectrum_entries(url), channel, self._post_announcement_entry)

    @poll_feeds.before_loop
    async def before_poll_feeds(self):
        await self.bot.wait_until_ready()

    @commands.command(name="rsscheck")
    async def rss_check(self, ctx: commands.Context):
        """Manually trigger an immediate RSS poll, useful for testing without waiting for the interval."""
        if not self.bot.config.rss_enabled:
            await ctx.reply("RSS polling is disabled in config.toml (`[rss].enabled = false`).", mention_author=False)
            return

        await ctx.reply("Checking feeds now...", mention_author=False)
        await self.poll_feeds()
        await ctx.send("Done.")


# SETUP
async def setup(bot: commands.Bot):
    await bot.add_cog(RssFeed(bot))
