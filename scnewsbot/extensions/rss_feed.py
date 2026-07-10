from __future__ import annotations
import discord
from discord.ext import commands, tasks
import aiohttp
import xml.etree.ElementTree as ET
import json
import os
import re
import html
from datetime import datetime, timezone

# CONFIG
STATE_FILE = "rss_state.json"
MAX_SEEN_PER_SOURCE = 300
REQUEST_TIMEOUT = 15
DESCRIPTION_LIMIT = 4000  # Discord embed description hard cap is 4096

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

PATCH_NOTES_COLOR = discord.Color.gold()

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
LOOSE_BULLET_RE = re.compile(r"\n{2,}(• )")

PATCH_NOTES_ENTRY_RE = re.compile(
    r'href="(/comm-link/Patch-Notes/(\d+)-[^"]+)"[^>]*>.*?<div class="title[^"]*">(.*?)</div>',
    re.S,
)

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
    def to_markdown(raw_html: str, limit: int = DESCRIPTION_LIMIT) -> str:
        """Convert RSI's post HTML to Discord markdown, preserving headings and
        bullet lists instead of flattening everything into one line."""
        text = raw_html or ""
        def _heading(m: re.Match) -> str:
            inner = BREAK_RE.sub("", m.group(1))  # e.g. stray <h2><br></h2> separators
            return f"\n\n**{inner}**\n" if TAG_RE.sub("", inner).strip() else ""

        text = LIST_ITEM_RE.sub(lambda m: f"\n• {m.group(1)}", text)
        text = HEADING_RE.sub(_heading, text)
        text = PARAGRAPH_RE.sub(lambda m: f"\n\n{m.group(1)}\n", text)
        text = BREAK_RE.sub("\n", text)
        text = TAG_RE.sub("", text)
        text = html.unescape(text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = BLANK_LINES_RE.sub("\n\n", text)
        text = LOOSE_BULLET_RE.sub(r"\n\1", text).strip()

        if len(text) > limit:
            text = text[:limit].rsplit("\n", 1)[0].rsplit(" ", 1)[0]

        return text or "(no content)"

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

    def _comm_link_embed(self, entry: FeedEntry) -> discord.Embed:
        embed = discord.Embed(
            title=entry.title,
            url=entry.link,
            description=entry.to_markdown(entry.summary_html),
            color=entry.comm_link_color(),
            timestamp=entry.published or datetime.now(timezone.utc),
        )
        image = entry.first_image()
        video_id = entry.youtube_video_id() if not image else None
        if image:
            embed.set_image(url=image)
        elif video_id:
            embed.set_image(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
        embed.set_footer(text=f"RSI Comm-Link{f' - {entry.category}' if entry.category else ''}")
        return embed

    def _patch_notes_embed(self, entry: FeedEntry) -> discord.Embed:
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
        await channel.send(embed=self._patch_notes_embed(entry))

    async def _poll_source(self, name: str, fetcher, channel: discord.abc.Messageable, poster) -> int:
        try:
            entries = await fetcher()
        except Exception as exc:
            print(f"[rss_feed] Failed to fetch '{name}': {exc}")
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
                try:
                    await poster(channel, entry)
                    posted += 1
                except discord.HTTPException as exc:
                    print(f"[rss_feed] Failed to post entry from '{name}': {exc}")
                    continue

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
            print(f"[rss_feed] Configured post channel {channel_id} not found/visible.")
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
