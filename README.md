# SC News Bot

Originally the production version of the SC News Bot used by the r/StarCitizen Discord server for the production of news posts. This fork adapts it for a single custom deployment (server-specific IDs moved out of code and into `config.toml`) and adds an automatic RSS/patch-notes news poller (`extensions/rss_feed.py`) on top of the original manual announcement builder.

- Join https://discord.gg/starcitizen for the best Star Citizen community on the internet

## Version
3.0.2

## Authors
- Ian (hencaric) — https://github.com/hencaric

Existing code is inspired by the 1.0 version produced by mudkip.

![](https://cdn.discordapp.com/attachments/1113146864804573285/1214590384001515560/41bannerEisenlowe.png?ex=65f9aa71&is=65e73571&hm=1cd99b04925a778d05b6d1cd1ae804af083760b0c11cc7ae96d44491b1fcb3ff&)

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Key Features](#key-features)
4. [Project Layout](#project-layout)
5. [Setup & Installation](#setup--installation)
6. [Configuration Reference](#configuration-reference)
7. [Running the Bot](#running-the-bot)
8. [Usage Guide](#usage-guide)
9. [Maintenance](#maintenance)
10. [Known Issues / Gotchas](#known-issues--gotchas)

---

## Overview

SC News Bot is a Discord bot built on [discord.py](https://discordpy.readthedocs.io/) (v2.x) that helps staff of the r/StarCitizen Discord draft, preview, and publish news announcements as rich embeds — without having to hand-craft embed JSON or use a webhook tool. Staff run a command inside Discord, get an interactive "builder" message with buttons/selects/modals, fill in the announcement fields, pick a destination channel and an optional ping role, and post it directly from the UI. A companion leaderboard tracks who has posted the most announcements over time.

It is a **single-guild, staff-facing tool**, not a general-purpose/multi-server bot — most of the configuration (channel IDs, role IDs) is hardcoded for the r/StarCitizen server.

## Architecture

```
scnewsbot/
├── __main__.py         # Entry point: loads .env + config.toml, builds & runs the Bot
├── bot.py              # Bot subclass (discord.ext.commands.Bot) + Core cog
├── utils.py             # Config wrapper around config.toml + permission helper
└── extensions/           # discord.py "cogs" (extensions), loaded dynamically
    ├── announcements.py  # The embed builder: modals, selects, buttons, posting logic
    ├── leaderboard.py    # Tracks + displays who has posted the most announcements
    └── rss_feed.py        # Polls Comm-Link/YouTube/Patch Notes and auto-posts new SC news
```

**Runtime flow:**

1. `__main__.py` loads environment variables from `.env` (via `python-dotenv`) and reads `config.toml` from the current working directory.
2. It wraps the parsed TOML in a `Config` object (`utils.py`) which exposes typed properties (`prefix`, `extensions`, `publish_channels`, permission lists, etc.).
3. A `Bot` instance (`bot.py`, a subclass of `commands.Bot`) is constructed with that config and intents (`message_content`, `members`) enabled.
4. `Bot.setup_hook()` runs on startup and dynamically loads every extension listed in `config.toml`'s `[bot].extensions` array (e.g. `extensions.announcements`, `extensions.leaderboard`, optionally `jishaku` for a debug/eval console), then adds the built-in `CoreCog`.
5. `bot.run(DISCORD_TOKEN)` logs in and starts the gateway connection.
6. Each extension registers its own `commands.Cog` via an async `setup(bot)` function — this is standard discord.py extension loading.

**Data flow for posting an announcement (`extensions/announcements.py`):**

1. Staff run `&embed create` (or `&embed edit <message>`).
2. An `Announcement` object holds the draft's fields (title, description, url, image, video, channel, ping role/preview, whether to auto-publish).
3. A `Builder` wraps that object with a `BuilderView` — a `discord.ui.View` containing:
   - `ChannelSelect` / `PingSelect` dropdowns (hardcoded channel/role option lists at the top of the file),
   - `FieldButton`s that pop a `TextModal` to edit title/description/url/image/video/ping-preview,
   - a `PublishButton` toggle, `CancelButton`, and `PostButton`.
4. Every interaction re-renders the embed preview live (`interaction.response.edit_message`).
5. On `Post`, the bot sends (or edits) the embed in the selected channel, optionally calls `message.publish()` if the channel is an Announcement/News channel and "Published" is toggled on, sends the video URL and ping message as follow-up messages, mirrors a copy to a hardcoded logging channel, and — if the channel is one of the tracked `LEADERBOARD_CHANNEL_IDS` — records the post against the author in the leaderboard.
6. Separately, `CoreCog.on_message` auto-publishes any message posted directly (not via the builder) into a channel listed in `config.toml`'s `publish_channels` if that channel is a news/announcement channel.

**Leaderboard (`extensions/leaderboard.py`):**
- A tiny flat-file "database": `leaderboard.json` at the working directory root, holding per-user post counts, last-post timestamp, and a full history of post timestamps.
- `record_announcement_post(user_id)` is called by the announcements cog and appends to that JSON file (no external DB required).
- `&leaderboard` renders a paginated embed (5 users/page) with Previous/Next/"My Rank" buttons, showing all-time/30-day/yearly counts per user plus server-wide totals.

**RSS auto-posting (`extensions/rss_feed.py`):**
- A `discord.ext.tasks` loop polls three sources on an interval (`config.toml`'s `[rss].poll_interval_minutes`) and posts any entry it hasn't seen before into a single configured channel (`[rss].post_channel`) — fully automatic, no staff interaction required.
- **Official Star Citizen YouTube channel** — posted as a bare link so Discord's native unfurler renders a real video card. Uses YouTube's own official Atom feed.
- **RSI Comm-Link news** — posted as a rich embed. The HTML article body is converted to Discord markdown (headings → bold, `<li>` → `•` bullets, paragraphs preserved) rather than flattened to plain text, up to Discord's ~4096-char embed description limit. **RSI does not have a working official RSS feed** — see [Known Issues](#known-issues--gotchas) — so this source (`comm_link_feed_url`) points at a third-party community mirror (`leonick.se/feeds/rsi/atom`) that crawls robertsspaceindustries.com directly. Note the mirror's Atom entries carry both a short `<summary>` teaser and the full `<content>` body — the parser deliberately prefers `content` so posts show full detail, not just the teaser.
- **RSI Patch Notes** — RSI publishes granular per-version changelogs (Bug Fixes / Feature Updates / Known Issues) at `/comm-link/Patch-Notes/...`, which is a *separate* content type the Comm-Link mirror above doesn't cover at all. This source (`patch_notes_url`) polls RSI's own `/en/patch-notes` listing page directly — it's plain server-rendered HTML, unlike the individual patch-note articles which are JS-hydrated and sit behind reCAPTCHA/Cookiebot (confirmed via direct testing: those return an unrendered template shell to any plain HTTP client). So the bot posts a link-out alert the moment a new version's notes go live, rather than attempting to scrape the full body inline.
- **De-duplication:** a small JSON store (`rss_state.json`, gitignored, same pattern as `leaderboard.json`) remembers each entry's ID (Atom `<id>` or patch version number) per source so nothing posts twice, capped at the 300 most recent IDs per source.
- **First-run safety:** the very first time a source is polled (no existing state), the cog records every current entry as "already seen" **without posting anything** — so turning this on (or adding a new source) never dumps a feed's entire back-catalog into the channel. Only entries published after that baseline get posted.
- `&rsscheck` triggers an immediate poll on demand (useful for testing without waiting for the interval).

## Key Features

- **Interactive embed builder** — no manual embed JSON; a Discord-native UI (selects, buttons, modals) drives announcement creation and editing.
- **Live preview** — the embed re-renders after every field edit, before it's ever posted.
- **Channel + ping-role presets** — dropdowns pre-populated (via `config.toml`'s `[channels]` table) with the server's actual news/announcement channels and their associated ping roles.
- **One-click "Publish"** — optionally cross-post the announcement immediately if posted into a Discord News/Announcement channel.
- **Auto-publish listener** — any message posted directly into a configured `publish_channels` news channel gets auto-published, even outside the builder.
- **Markdown shorthand** — lines starting with `-` become `➣` bullets and lines starting with `+` become indented `✦` sub-bullets in the description, so staff can type plain lists.
- **Edit support** — re-open any previously-posted announcement embed and edit it in place via `&embed edit <message>`.
- **Announcement leaderboard** — automatic, persistent tracking of who's posting news, with a paginated leaderboard UI (`&leaderboard`) and all-time/30-day/yearly breakdowns.
- **Logging channel mirror** — every posted announcement is also echoed to an internal logging channel for audit purposes.
- **Debug mode & permission scaffolding** — `config.toml` supports a `debug` flag and allow-lists for guilds/roles/users (see [Known Issues](#known-issues--gotchas) — these lists are not currently enforced on any command).
- **Jishaku** — optional debug/eval cog (`jishaku` in the extensions list) for live code evaluation and extension reloading during development.
- **Automatic RSS/video news posting** — separate from the manual builder, a background poller auto-posts new Star Citizen dev news (Comm-Link) and videos (official YouTube channel) with zero staff involvement, with built-in de-duplication and a safe first-run baseline (see above).

## Project Layout

| Path | Purpose |
|---|---|
| `scnewsbot/__main__.py` | Process entry point |
| `scnewsbot/bot.py` | `Bot` class, intents, `CoreCog` (`info` command, auto-publish listener) |
| `scnewsbot/utils.py` | `Config` wrapper, `can_publish_announcements` permission helper |
| `scnewsbot/extensions/announcements.py` | Embed builder UI + posting/publishing logic |
| `scnewsbot/extensions/leaderboard.py` | Post-tracking leaderboard cog + JSON store |
| `scnewsbot/extensions/rss_feed.py` | RSS/Atom poller + auto-posting cog + de-dup store |
| `config.toml` | Local runtime config (gitignored — created from `example.config.toml`) |
| `.env` | Local secrets/env vars (gitignored — created from `example.env`) |
| `leaderboard.json` | Persistent leaderboard data store (plain JSON, gitignored — deployment-specific runtime state, same treatment as `rss_state.json`) |
| `rss_state.json` | Persistent RSS de-dup store (plain JSON, gitignored — deployment-specific runtime state) |
| `pyproject.toml` | Poetry project metadata + dependencies |

## Setup & Installation

### Prerequisites
- Python **3.12+** (this environment was verified working on 3.13/3.14; the project's `pyproject.toml` pins `python = "^3.12"`)
- A Discord bot application + token (from the [Discord Developer Portal](https://discord.com/developers/applications)), with the **Server Members Intent** and **Message Content Intent** enabled under *Bot → Privileged Gateway Intents* (the bot requests both — `discord.Intents.members` and `message_content` — and will fail to start if they aren't enabled on the application).

### Install steps

```bash
# 1. Clone (already done)
cd SCNewsBot

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install "discord.py>=2.3.2,<3" "jishaku>=2.5.2" "python-dotenv>=1.0.1" "aiohttp>=3.9"
# (equivalently, if you have Poetry installed: `poetry install`)

# 4. Create your local config files
cp example.env .env
cp example.config.toml config.toml

# 5. Edit .env and put your real bot token in DISCORD_TOKEN
# 6. Edit config.toml as needed (see Configuration Reference below)
```

> This repo ships without a `poetry.lock`, so a plain `pip install` of the three runtime dependencies above is the fastest path if you don't have Poetry set up. `poetry install` works identically if you do.

## Configuration Reference

### `.env`
| Key | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Your bot's token from the Developer Portal. Loaded via `python-dotenv` in `__main__.py`. |
| `JISHAKU_NO_UNDERSCORE` | No | Set to `1` so Jishaku's debug commands don't require the `jsk` prefix underscore convention. |
| `JISHAKU_HIDE` | No | Set to `1` to hide Jishaku's commands from the default help command. |

### `config.toml`
| Key | Type | Description |
|---|---|---|
| `debug` | bool | **Must be `false` in production.** When `true`, the `[permissions.debug]` allow-lists are added on top of the regular ones. |
| `bot.prefix` | string | Text command prefix (the bot also always responds to `@mentions`). |
| `bot.extensions` | list[str] | Dotted extension module paths to load on startup, relative to the `scnewsbot` package (e.g. `extensions.announcements`, `extensions.leaderboard`, `jishaku`). |
| `bot.repost_channels` | list[int] | Present in the config schema but **not currently read by any code** — reserved/vestigial. |
| `bot.publish_channels` | list[int] | Channel IDs where any message sent directly (not via the builder) is auto-published if the channel is a Discord News channel. |
| `permissions.allowed_guilds` | list[int] | Guild IDs intended to gate announcement commands. |
| `permissions.allowed_roles` | list[int] | Role IDs intended to gate announcement commands. |
| `permissions.allowed_users` | list[int] | User IDs intended to always be allowed. |
| `permissions.debug.*` | same as above | Extra allow-lists merged in only when `debug = true`. |
| `channels.options` | list[[str, int]] | `[display name, channel ID]` pairs offered in the builder's "Select Channel" dropdown. |
| `channels.ping_roles` | list[[str, int]] | `[display name, role ID]` pairs offered in the builder's "Select Ping Role" dropdown. |
| `channels.logging_channels` | list[int] | Channel IDs every posted announcement is mirrored to for auditing. |
| `channels.leaderboard_channels` | list[int] | Channel IDs whose posts count toward `&leaderboard`. |
| `rss.enabled` | bool | Turns the auto-posting poller on/off. |
| `rss.poll_interval_minutes` | number | How often (minutes) to check the feeds for new entries. |
| `rss.post_channel` | int | The single channel new news/video posts are sent to. |
| `rss.comm_link_feed_url` | string | Atom feed URL for general Star Citizen dev-update news (defaults to a third-party Comm-Link mirror — see [Known Issues](#known-issues--gotchas)). |
| `rss.youtube_feed_url` | string | Atom feed URL for the official Star Citizen YouTube channel's uploads. |
| `rss.patch_notes_url` | string | RSI's official Patch Notes listing page — the itemized per-version changelog, separate from general Comm-Link posts. Polled directly (no mirror). |

> **Note:** `config.toml` and `.env` are both gitignored — every deployment/clone needs its own copies. Both must live in the **current working directory the bot is launched from** (see [Running the Bot](#running-the-bot)), not inside the `scnewsbot/` package folder.

### Server-specific channel/role IDs
The `[channels]` table in `config.toml` (read via `Config.channel_options`, `.ping_role_options`, `.logging_channel_ids`, `.leaderboard_channel_ids` in `utils.py`) holds every ID specific to the deploying Discord server — dropdown options, the logging channel(s), and which channels count toward the leaderboard. This used to be hardcoded in `extensions/announcements.py`; it now lives entirely in config, so redeploying to a different server (or adding/removing a news channel) only requires editing `config.toml`, not code. `DEFAULT_IMAGE_URL` (fallback embed image) remains a code-level constant in `announcements.py` since it isn't server-specific.

## Running the Bot

Because of how imports and relative file paths are wired (`bot.py`/`utils.py` use flat, package-relative imports, while `config.toml`/`leaderboard.json` are opened as paths relative to the process's working directory), the bot **must be launched from the repository root**, pointing directly at the entry script — not with `python -m scnewsbot`, and not from inside the `scnewsbot/` folder:

```bash
# from the repository root, with the venv activated
python scnewsbot/__main__.py
```

Running `python -m scnewsbot` from the root will fail with `ModuleNotFoundError: No module named 'bot'`, and running `python __main__.py` from inside `scnewsbot/` will fail to find `config.toml`/`leaderboard.json` (they'd need to be duplicated into that folder). The command above is the one verified to work end-to-end in this environment (it successfully loaded `config.toml`, loaded both extensions, and reached Discord's login endpoint).

On success you'll see:
```
[INFO] discord.client: logging in using static token
The News Bot is now ready.
```

## Usage Guide

Default prefix is `&` (configurable), and the bot also responds to `@BotMention <command>`.

| Command | Description |
|---|---|
| `&embed create` | Opens the interactive builder in the current channel to draft a new announcement. |
| `&embed edit <message>` | Re-opens the builder pre-filled from an existing message's embed, for in-place editing (accepts a message link or ID). |
| `&leaderboard` | Shows the paginated announcement leaderboard (Previous / Next / "My Rank" buttons). |
| `&info` (or `/info`) | Shows bot version, library version, author, and a link to the source repo. Available as both a text command and a slash command (hybrid command). |
| `&rsscheck` | Immediately polls the RSS/video feeds instead of waiting for the next scheduled interval — new items post right away. Useful for testing/verifying the auto-poster. |

**Builder walkthrough:**
1. Run `&embed create`.
2. Pick a **Channel** and optionally a **Ping Role** from the dropdowns.
3. Click the field buttons (Title, Description, URL, Image, Video, Ping Preview) — each opens a modal text input. Buttons turn green once filled in.
   - In the Description field, start a line with `-` for a bullet (➣) or `+` for an indented sub-bullet (✦).
4. Toggle **Published** if you want the message auto-published (only takes effect if the destination is a News/Announcement channel).
5. Click **Post** to send it (or **Cancel** to discard). Editing flow uses the same view with a **Post** button relabeled **Edit**.

## Maintenance

- **Adding a channel/role option:** edit the `[channels]` table in `config.toml` (`options`, `ping_roles`, `logging_channels`, `leaderboard_channels`) — no code changes or restart-only-then-edit-code cycle needed, just update the config and restart the bot.
- **Rotating the bot token:** regenerate it in the Developer Portal and update `DISCORD_TOKEN` in `.env`; no code changes needed.
- **Leaderboard data:** lives in `leaderboard.json` at the repo root, keyed by Discord user ID. It's plain JSON, gitignored (deployment-specific, like `rss_state.json`) — back it up before manual edits, and be aware it grows unbounded (every post timestamp is retained forever, not just aggregated counts).
- **Adding a new extension/cog:** drop a new file in `scnewsbot/extensions/` with an async `setup(bot)` function, then add its dotted path (`extensions.<name>`) to `bot.extensions` in `config.toml`.
- **Dependency updates:** managed via `pyproject.toml` (Poetry). Run `black .` before committing — CI (`.github/workflows/black.yml`) enforces `black` formatting on every push/PR.
- **Enabling debug tooling:** add `"jishaku"` to `bot.extensions` in `config.toml` (already included in this local setup) and set the `JISHAKU_*` env vars for a live eval console (`jsk py`, `jsk reload`, etc.) — useful for iterating without restarting the process.
- **Tuning the RSS poller:** edit `[rss]` in `config.toml` — `enabled`, `poll_interval_minutes`, `post_channel`, `comm_link_feed_url`, `youtube_feed_url`. Changes take effect on next bot restart.
- **Resetting RSS de-dup state:** delete `rss_state.json` and restart — the next poll will silently re-baseline (mark everything currently in the feeds as seen, post nothing) rather than dumping the whole feed history. Don't delete it expecting a "replay" of recent posts.
- **If the Comm-Link mirror (`leonick.se`) goes down:** the news half of the poller will just log fetch errors (`[rss_feed] Failed to fetch 'comm_link': ...`) and skip that source each cycle — the YouTube side keeps working independently. Swap in a different Atom/RSS mirror by updating `comm_link_feed_url`, or set `rss.enabled = false` until one is available.

## Known Issues / Gotchas

- **Permission allow-lists are not enforced.** `utils.py` defines `can_publish_announcements()` and `config.toml` ships a full `[permissions]` schema, but no command in `extensions/announcements.py` actually applies it as a `commands.check`. As shipped, **any member who can message the bot can create/edit/post announcements** — there's no role/guild/user gating in effect despite the config surface implying there is. Wire up `@commands.check(can_publish_announcements)` on the `embed` commands if you need this enforced.
- **`repost_channels`** is defined in `Config` and present in `example.config.toml`, but no code reads it — currently a no-op setting.
- **RSI has no working official RSS feed.** `/comm-link/rss` still exists as a URL and is even advertised via a `<link rel="alternate" type="application/rss+xml">` tag on the Comm-Link page, but it 301-redirects to the plain HTML page instead of serving XML — a known, unresolved bug acknowledged on [RSI's own forums](https://forums.robertsspaceindustries.com/discussion/227345/comm-link-rss-feed-not-working). `rss.comm_link_feed_url` defaults to a third-party mirror (`leonick.se/feeds/rsi/atom`) as a workaround — it's a single community maintainer's project, not official infrastructure, so it could go down without notice (see [Maintenance](#maintenance) for what happens if it does).
- **Working directory matters.** `config.toml` and `leaderboard.json` are resolved relative to the process's current working directory, not the package location — always launch with `python scnewsbot/__main__.py` from the repo root (see [Running the Bot](#running-the-bot)).
- **`.gitignore` references `rstarcitizen.py`**, a file that doesn't exist in this repo — leftover from earlier history (an `rstarcitizen`-specific feature set was removed per the git log, commit `f5d629f`).
