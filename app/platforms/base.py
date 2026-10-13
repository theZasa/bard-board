"""What the soundboard core needs from a voice chat platform (Mumble, Discord).

The core (library, mixer, music, commands, web interface) never talks to a platform library directly.
An adapter translates between the platform and these small, plain types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from ..core import Soundboard

# Channel ids are strings everywhere outside the adapters: Discord snowflakes do not fit into a
# JavaScript number, and Mumble's small integers are fine as strings too.
ChannelId = str


@dataclass(frozen=True)
class PlatformUser:
    id: str  # stable while the user is connected (Mumble session, Discord user id)
    name: str
    channel_id: ChannelId | None
    muted: bool = False
    deaf: bool = False


@dataclass(frozen=True)
class ChannelInfo:
    id: ChannelId
    path: str  # shown to humans, e.g. "Gaming/Dota"
    users: int


@dataclass
class IncomingText:
    """A chat message that may contain a command, already reduced to what the commands need."""

    sender: str
    sender_id: str
    text: str  # plain text
    reply: Callable[[str], Awaitable[None] | None]  # answer with HTML (the adapter converts it)
    private: bool = False
    links: list[str] = field(default_factory=list)  # link targets as the client sent them
    is_admin: bool = False
    sender_channel: ChannelId | None = None  # where the sender is (for /come)
    source: str = "mumble"  # for the activity log
    # Who wrote, as stable as the platform allows (Mumble user ID when registered, Discord user ID).
    account: str = ""
    # Answer only the sender (e.g. the result of /login); None = reply is private already or impossible.
    reply_private: Callable[[str], Awaitable[None] | None] | None = None


class AudioSink(Protocol):
    """Where the mixer delivers 20 ms frames of 48 kHz mono 16 bit PCM."""

    @property
    def ready(self) -> bool: ...

    def send_frame(self, pcm: bytes) -> None: ...

    def end(self) -> None:
        """The burst of audio is over (the bot stops "talking")."""

    async def drain(self) -> None: ...


class VoicePlatform(Protocol):
    key: str  # "mumble" | "discord"
    name: str  # "Mumble" | "Discord"
    audio: AudioSink
    message_length: int  # longest chat message in characters (HTML for Mumble)

    def bind(self, board: "Soundboard") -> None: ...

    def message_size(self, html: str) -> int:
        """How much of message_length a reply uses once converted for this platform."""

    async def run_forever(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def ready(self) -> bool:
        """Logged in and able to chat."""

    @property
    def me(self) -> PlatformUser | None: ...

    @property
    def channel_id(self) -> ChannelId | None:
        """The voice channel the bot is in."""

    def users_in_channel(self, channel_id: ChannelId | None) -> list[PlatformUser]:
        """Users in that channel, without the bot."""

    def user(self, user_id: str) -> PlatformUser | None: ...

    def find_user(self, name: str) -> PlatformUser | None: ...

    def channels(self) -> list[ChannelInfo]: ...

    def has_channel(self, channel_id: ChannelId) -> bool: ...

    def find_channel(self, name: str) -> ChannelId | None: ...

    def channel_path(self, channel_id: ChannelId | None) -> str: ...

    @property
    def moving(self) -> bool:
        """A channel change is under way."""

    def move_to(self, channel_id: ChannelId) -> None: ...

    def announce(self, html: str) -> None:
        """Message to everybody in the bot's channel.

        An adapter may also offer announce_track(info, requested_by) for a richer "now playing" message."""

    def status(self) -> dict:
        """connected, server, server_release, username, last_error, connected_since, details."""

    def channel_id_from(self, value: object) -> ChannelId:
        """Normalize a channel id coming from the web interface."""
