from discord.ext import commands


class Config:
    def __init__(self, config: dict):
        self.config = config

    @property
    def embed_color(self) -> int:
        return 0x0504AA

    @property
    def debug(self) -> bool:
        return self.config.get("debug", False)

    @property
    def prefix(self) -> str:
        return self.config["bot"].get("prefix", "sc ")

    @property
    def extensions(self) -> list:
        return self.config["bot"].get("extensions", ["jishaku"])

    @property
    def repost_channels(self) -> list[int]:
        return self.config["bot"].get("repost_channels", [])

    @property
    def publish_channels(self) -> list[int]:
        return self.config["bot"].get("publish_channels", [])

    @property
    def channel_options(self) -> list[tuple[str, int]]:
        return [(name, cid) for name, cid in self.config.get("channels", {}).get("options", [])]

    @property
    def ping_role_options(self) -> list[tuple[str, int]]:
        return [(name, rid) for name, rid in self.config.get("channels", {}).get("ping_roles", [])]

    @property
    def logging_channel_ids(self) -> list[int]:
        return self.config.get("channels", {}).get("logging_channels", [])

    @property
    def leaderboard_channel_ids(self) -> set[int]:
        return set(self.config.get("channels", {}).get("leaderboard_channels", []))

    @property
    def rss_enabled(self) -> bool:
        return self.config.get("rss", {}).get("enabled", False)

    @property
    def rss_poll_interval_minutes(self) -> float:
        return self.config.get("rss", {}).get("poll_interval_minutes", 10)

    @property
    def rss_post_channel(self) -> int | None:
        return self.config.get("rss", {}).get("post_channel")

    @property
    def rss_comm_link_feed_url(self) -> str | None:
        return self.config.get("rss", {}).get("comm_link_feed_url")

    @property
    def rss_youtube_feed_url(self) -> str | None:
        return self.config.get("rss", {}).get("youtube_feed_url")

    @property
    def rss_patch_notes_url(self) -> str | None:
        return self.config.get("rss", {}).get("patch_notes_url")

    @property
    def rss_ptu_patch_notes_url(self) -> str | None:
        return self.config.get("rss", {}).get("ptu_patch_notes_url")

    @property
    def rss_announcements_url(self) -> str | None:
        return self.config.get("rss", {}).get("announcements_url")

    @property
    def allowed_guilds(self) -> list:
        return self._get_allowed_objects("allowed_guilds")

    @property
    def allowed_roles(self) -> list:
        return self._get_allowed_objects("allowed_roles")

    @property
    def allowed_users(self) -> list:
        return self._get_allowed_objects("allowed_users")

    def _get_allowed_objects(self, object_name, /) -> list:
        allowed_objects = self.config["permissions"].get(object_name, [])
        if self.debug:
            allowed_objects += self.config["permissions"]["debug"].get(object_name, [])

        return allowed_objects


def can_publish_announcements(ctx: commands.Context) -> bool:
    if not ctx.guild:
        return False

    if ctx.author.id in ctx.bot.config.allowed_users:
        return True

    if ctx.guild.id in ctx.bot.config.allowed_guilds:
        for allowed_role in ctx.bot.config.allowed_roles:
            if allowed_role in ctx.author._roles:
                return True

    return False
