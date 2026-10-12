"""Chat commands that users can send to the soundboard (Mumble chat, Discord chat and slash commands)."""

from __future__ import annotations

import asyncio
import functools
import html
import inspect
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from .library import Sound, fold
from .media import MediaError, format_time, is_url, parse_time
from .platforms.base import IncomingText

if TYPE_CHECKING:
    from .catalog import Collection
    from .core import Soundboard

log = logging.getLogger("commands")

from .platforms.mumble import strip_html  # noqa: E402,F401 - re-exported for tools and tests


def esc(text: str) -> str:
    return html.escape(text, quote=False)


@dataclass
class Context:
    msg: IncomingText
    sender: str
    args: str
    prefix: str

    @property
    def source(self) -> str:
        return self.msg.source

    @property
    def admin(self) -> bool:
        return self.msg.is_admin


Handler = Callable[[Context], Awaitable[None]]


@dataclass
class Command:
    names: tuple[str, ...]
    usage: str
    help: str
    handler: Handler
    admin: bool = False
    plays: bool = False
    group: str = "Sounds"


class CommandHandler:
    def __init__(self, board: "Soundboard") -> None:
        self.board = board
        self.cfg = board.cfg
        self._last_play: dict[str, float] = {}
        self._pending: set[asyncio.Future] = set()  # replies that are still being sent
        m = "Musik"
        x = "Extras"
        self.commands: list[Command] = [
            Command(("sb",), "&lt;name&gt;", "Sound abspielen (kurz: {p}&lt;name&gt;)", self.cmd_sound, plays=True),
            Command(("random", "r", "zufall"), "[filter]", "Zufälliger Sound", self.cmd_random, plays=True),
            Command(("again", "nochmal"), "", "Letzten Sound wiederholen", self.cmd_again, plays=True),
            Command(("stop", "s"), "", "Sounds stoppen und Musik pausieren", self.cmd_stop),
            Command(("list", "ls", "liste"), "[filter]", "Sounds auflisten", self.cmd_list),
            Command(("search", "find", "suche"), "&lt;text&gt;", "Sounds suchen", self.cmd_search),
            Command(("top",), "", "Meistgespielte Sounds", self.cmd_top),
            Command(("np", "now"), "", "Was läuft gerade?", self.cmd_np),
            Command(("vol", "volume"), "[0-100]", "Gesamtlautstärke anzeigen oder setzen", self.cmd_volume, admin=True),
            Command(("come", "komm"), "", "Bot in deinen Kanal holen", self.cmd_come, admin=True),
            Command(("follow",), "[name]", "Bot folgt dir (oder name)", self.cmd_follow, admin=True),
            Command(("unfollow",), "", "Bot folgt niemandem mehr", self.cmd_unfollow, admin=True),
            Command(("help", "h", "hilfe", "commands"), "", "Diese Hilfe", self.cmd_help),
            Command(("play", "yt", "m"), "&lt;link|suche&gt;", "YouTube-Link, Playlist oder Suche in die Warteschlange",
                    self.cmd_music_play, plays=True, group=m),
            Command(("playnext", "pn"), "&lt;link|suche&gt;", "Als Nächstes spielen", self.cmd_music_playnext,
                    plays=True, group=m),
            Command(("skip", "next", "n"), "", "Aktuellen Song überspringen", self.cmd_skip, group=m),
            Command(("pause",), "", "Musik pausieren", self.cmd_pause, group=m),
            Command(("resume", "weiter", "continue"), "", "Musik fortsetzen", self.cmd_resume, group=m),
            Command(("queue", "q", "warteschlange"), "", "Warteschlange anzeigen", self.cmd_queue, group=m),
            Command(("remove", "rm"), "&lt;nr&gt;", "Titel aus der Warteschlange entfernen", self.cmd_remove, group=m),
            Command(("clear", "leeren"), "", "Warteschlange leeren", self.cmd_clear, group=m),
            Command(("shuffle", "mischen"), "", "Warteschlange mischen", self.cmd_shuffle, group=m),
            Command(("mvol", "musikvol"), "[0-100]", "Musik-Lautstärke", self.cmd_music_volume, admin=True, group=m),
            Command(("clip",), "&lt;link&gt; &lt;start&gt; &lt;ende&gt; [name]", "Ausschnitt als Sound speichern",
                    self.cmd_clip, plays=True, group=m),
            Command(("radio", "sender"), "[name]", "Gespeicherten Livestream starten, ohne Namen alle Sender "
                    "(<code>{p}radio add name link</code>, <code>{p}radio del name</code>)", self.cmd_radio, group=m),
            Command(("tts", "say", "sag"), "&lt;text&gt;", "Text vorlesen", self.cmd_tts, plays=True, group=x),
            Command(("pull",), "[sekunden|stop]", "Pull-Timer mit Countdown (Standard 10 s)", self.cmd_pull, group=x),
            Command(("joinsound", "js"), "[sound|aus]", "Dein Sound, wenn du den Kanal betrittst "
                    "(<code>{p}joinsound @name sound</code> für andere)", self.cmd_joinsound, group=x),
            Command(("alias",), "[name] [ziel]", "Kürzel anzeigen oder anlegen, z. B. <code>{p}alias lr leeroy</code>",
                    self.cmd_alias, group=x),
            Command(("unalias",), "&lt;name&gt;", "Kürzel löschen", self.cmd_unalias, group=x),
        ]
        self.refresh_collections()

    def refresh_collections(self) -> None:
        """(Re)register one command per collection, e.g. after a collection folder appeared."""
        self.commands = [c for c in self.commands if c.group != "Sammlungen"]
        self.board.catalog.assign_commands({n for cmd in self.commands for n in cmd.names})
        for collection in self.board.catalog:
            if collection.commands:
                self.commands.append(Command(
                    collection.commands, "[suche|@name]", f"Zufällige Zeile aus {esc(collection.name)}",
                    functools.partial(self.cmd_collection, collection), plays=True, group="Sammlungen",
                ))
        self._by_name = {name: cmd for cmd in self.commands for name in cmd.names}

    # ------------------------------------------------------------ dispatching

    def _parse(self, text: str) -> tuple[str, str, str] | None:
        for prefix in sorted(self.cfg.command_prefixes, key=len, reverse=True):
            if text.startswith(prefix) and len(text) > len(prefix):
                body = text[len(prefix) :].strip()
                name, _, args = body.partition(" ")
                return prefix, name.casefold(), args.strip()
        return None

    async def handle(self, msg: IncomingText) -> None:
        """A chat message that reached the bot (the adapter decided it is addressed to us)."""
        parsed = self._parse(msg.text)
        if parsed is None:
            return
        prefix, name, args = parsed
        await self.run(msg, prefix, name, args)

    async def run(self, msg: IncomingText, prefix: str, name: str, args: str) -> None:
        """Run one command; also used by Discord's slash commands."""
        if self.cfg.is_blocked(msg.sender):
            return
        ctx = Context(msg, msg.sender, args, prefix)
        log.info("%s: %s%s %s", msg.sender, prefix, name, args)
        await self._dispatch(ctx, name, args)

    async def _dispatch(self, ctx: Context, name: str, args: str, depth: int = 0) -> None:
        ctx.args = args
        cmd = self._by_name.get(name)
        if cmd is None:
            alias = self.board.aliases.get(name)
            if alias is not None and depth < 3:
                parsed = self._parse(alias["target"])
                if parsed:  # alias for a full command, extra arguments are appended
                    _, target_name, target_args = parsed
                    await self._dispatch(ctx, target_name, f"{target_args} {args}".strip(), depth + 1)
                else:  # alias for a sound
                    ctx.args = alias["target"]
                    await self._with_cooldown(ctx, self.cmd_sound)
                return
            # "/leeroy" is a shortcut for "/sb leeroy"
            ctx.args = f"{name} {args}".strip()
            await self._with_cooldown(ctx, self.cmd_sound, shortcut=True)
            return
        if cmd.admin and not ctx.admin:
            self.reply(ctx, "Dieser Befehl ist Admins vorbehalten.")
            return
        if cmd.plays:
            await self._with_cooldown(ctx, cmd.handler)
        else:
            await cmd.handler(ctx)

    async def _with_cooldown(self, ctx: Context, handler: Callable, shortcut: bool = False) -> None:
        now = time.monotonic()
        last = self._last_play.get(ctx.sender.casefold(), 0.0)
        if now - last < self.cfg.command_cooldown:
            return  # silently ignore spam
        self._last_play[ctx.sender.casefold()] = now
        if shortcut:
            await handler(ctx, shortcut=True)
        else:
            await handler(ctx)

    # ---------------------------------------------------------------- replies

    def reply(self, ctx: Context, html_text: str) -> None:
        result = ctx.msg.reply(html_text)
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def flush_replies(self) -> None:
        """Wait until the replies sent so far are delivered."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def reply_chunks(self, ctx: Context, header: str, items: list[str], sep: str = " · ", more: str = "") -> None:
        """A long list in ONE message that respects the server's message length: what does not fit is
        summarised ("… und 40 weitere"). Several messages in a row can trip a server's spam protection."""
        limit = max(500, self.board.platform.message_length - 150)
        current, empty = header, True
        for i, item in enumerate(items):
            piece = item if empty or item.startswith("<br>") else sep + item
            if len(current) + len(piece) > limit:
                current += f"<br><i>… und {len(items) - i} weitere{(' - ' + more) if more else ''}</i>"
                break
            current += piece
            empty = False
        self.reply(ctx, current)

    def _suggest(self, ctx: Context, sounds: list[Sound], query: str) -> None:
        names = ", ".join(f"<i>{esc(s.name)}</i>" for s in sounds[:8])
        more = f" (+{len(sounds) - 8})" if len(sounds) > 8 else ""
        self.reply(ctx, f"Mehrere Treffer für <b>{esc(query)}</b>: {names}{more}")

    async def _play(self, ctx: Context, sound: Sound) -> None:
        from .core import PlayError

        try:
            await self.board.play(sound, requested_by=ctx.sender, source=ctx.source)
        except PlayError as exc:
            self.reply(ctx, esc(str(exc)))

    # ----------------------------------------------------------------- commands

    async def cmd_sound(self, ctx: Context, shortcut: bool = False) -> None:
        query = ctx.args
        if not query:
            self.reply(ctx, f"Welcher Sound? Beispiel: <code>{esc(ctx.prefix)}sb leeroy</code>")
            return
        match = self.board.library.resolve(query)
        if match.unique or (match.sounds and match.exact):
            await self._play(ctx, match.sounds[0])
        elif match.sounds:
            self._suggest(ctx, match.sounds, query)
        else:
            hint = f" <code>{esc(ctx.prefix)}help</code> zeigt alle Befehle." if shortcut else ""
            self.reply(ctx, f"Kein Sound gefunden für <b>{esc(query)}</b>.{hint}")

    async def cmd_random(self, ctx: Context) -> None:
        sound = self.board.random_sound(ctx.args)
        if sound is None:
            self.reply(ctx, "Keine passenden Sounds gefunden.")
            return
        await self._play(ctx, sound)
        if self.board.platform.audio.ready:
            self.reply(ctx, f"&#127922; <b>{esc(sound.name)}</b>")

    async def cmd_again(self, ctx: Context) -> None:
        sound = self.board.last_played
        if sound is not None:  # the library object has the current volume; collection lines stay as they are
            sound = self.board.library.get(sound.id) or sound
        if sound is None:
            self.reply(ctx, "Es wurde noch nichts abgespielt.")
            return
        await self._play(ctx, sound)

    async def cmd_stop(self, ctx: Context) -> None:
        self.board.stop_all(requested_by=ctx.sender, source=ctx.source)

    async def cmd_list(self, ctx: Context) -> None:
        lib = self.board.library
        sounds = lib.search(ctx.args, limit=100000) if ctx.args else list(lib.sounds.values())
        if not sounds:
            self.reply(ctx, "Keine passenden Sounds gefunden.")
            return
        items: list[str] = []
        current_cat = None
        for s in sounds:
            if s.category != current_cat:
                current_cat = s.category
                if current_cat:
                    items.append(f"<br><b>{esc(current_cat)}:</b> ")
            items.append(esc(s.name))
        header = f"<b>{len(sounds)} Sounds</b> - abspielen mit <code>{esc(ctx.prefix)}sb name</code><br>"
        # Category headers must not be joined with separators.
        merged: list[str] = []
        for item in items:
            if merged and merged[-1].endswith(":</b> "):
                merged[-1] += item
            else:
                merged.append(item)
        self.reply_chunks(ctx, header, merged,
                          more=f"alle im Webinterface oder gefiltert mit <code>{esc(ctx.prefix)}list name</code>")

    async def cmd_search(self, ctx: Context) -> None:
        if not ctx.args:
            self.reply(ctx, f"Wonach suchen? Beispiel: <code>{esc(ctx.prefix)}search corona</code>")
            return
        hits = self.board.library.search(ctx.args, limit=40)
        if not hits:
            self.reply(ctx, f"Nichts gefunden für <b>{esc(ctx.args)}</b>.")
            return
        self.reply(ctx, f"<b>{len(hits)} Treffer:</b> " + " · ".join(esc(s.name) for s in hits))

    async def cmd_top(self, ctx: Context) -> None:
        sounds = sorted(self.board.library.sounds.values(), key=lambda s: s.plays, reverse=True)
        sounds = [s for s in sounds if s.plays][:10]
        if not sounds:
            self.reply(ctx, "Noch keine Statistik vorhanden.")
            return
        rows = "".join(f"<br>{i}. {esc(s.name)} <i>({s.plays}x)</i>" for i, s in enumerate(sounds, 1))
        self.reply(ctx, f"<b>Top {len(sounds)}</b>{rows}")

    async def cmd_np(self, ctx: Context) -> None:
        voices = self.board.mixer.voices
        lines = []
        music = self._now_playing()
        if music:
            lines.append(music)
        if voices:
            parts = [f"<b>{esc(v.name)}</b> ({v.elapsed:.0f}/{v.duration:.0f} s, von {esc(v.requested_by)})" for v in voices]
            lines.append("Sounds: " + ", ".join(parts))
        self.reply(ctx, "<br>".join(lines) if lines else "Gerade läuft nichts.")

    async def cmd_volume(self, ctx: Context) -> None:
        if not ctx.args:
            self.reply(ctx, f"Lautstärke: <b>{self.board.mixer.volume} %</b>")
            return
        raw = ctx.args.rstrip("%").strip()
        try:
            if raw[:1] in ("+", "-"):
                value = self.board.mixer.volume + int(raw)  # relative: "+10" / "-10"
            else:
                value = int(raw)
        except ValueError:
            self.reply(ctx, "Bitte eine Zahl von 0 bis 100 angeben.")
            return
        value = self.board.set_volume(value, requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, f"Lautstärke: <b>{value} %</b>")

    async def cmd_come(self, ctx: Context) -> None:
        channel = ctx.msg.sender_channel
        if channel is None:
            self.reply(ctx, "Du bist gerade in keinem Sprachkanal.")
            return
        if self.board.follow_user and self.board.follow_user.casefold() != ctx.sender.casefold():
            self.reply(ctx, f"Ich folge gerade {esc(self.board.follow_user)}. Erst <code>{esc(ctx.prefix)}unfollow</code>.")
            return
        self.board.join_channel(channel)

    async def cmd_follow(self, ctx: Context) -> None:
        name = ctx.args or ctx.sender
        user = self.board.platform.find_user(name)
        if user is None:
            self.reply(ctx, f"{esc(name)} ist nicht online.")
            return
        self.board.set_follow(user.name, requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, f"Ich folge jetzt <b>{esc(user.name)}</b>.")

    async def cmd_unfollow(self, ctx: Context) -> None:
        self.board.set_follow("", requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, "Ich folge niemandem mehr.")

    HELP_GROUPS = (("Sounds", "&#128266; Sounds"), ("Musik", "&#127925; Musik"), ("Extras", "&#10024; Extras"),
                   ("Sammlungen", "&#128218; Sammlungen"))

    def _help_html(self, prefix: str, groups: list[str], *, aliases: bool, details: bool) -> str:
        """All commands in one table: one message instead of one per group (spam protection)."""
        p = esc(prefix)
        muted = '<font color="#8a8f98">'
        rows = []
        for group, title in self.HELP_GROUPS:
            if group not in groups:
                continue
            cmds = [c for c in self.commands if c.group == group]
            if not cmds:
                continue
            top = ' style="padding-top:8px"' if rows else ""
            rows.append(f'<tr><td colspan="2"{top}><b>{title}</b></td></tr>')
            for cmd in cmds:
                usage = f" {cmd.usage}" if cmd.usage and details else ""
                text = cmd.help.format(p=p) if details else re.sub(r"\s*\(.*$", "", strip_html(cmd.help.format(p=p)))
                extra = ""
                if aliases and cmd.names[1:]:
                    extra = f" {muted}· {', '.join(p + a for a in cmd.names[1:])}</font>"
                lock = " &#128274;" if cmd.admin and (self.cfg.admins or self.cfg.discord_admin_role) else ""
                rows.append(f"<tr><td><code>{p}{cmd.names[0]}{usage}</code>&nbsp;&nbsp;</td>"
                            f"<td>{text}{extra}{lock}</td></tr>")
        others = [q for q in self.cfg.command_prefixes if q != prefix]
        hint = f" {muted}· statt {p} geht auch {esc(others[0])}</font>" if others else ""
        return f"<b>Soundboard-Befehle</b>{hint}<table cellspacing=\"0\" cellpadding=\"1\">{''.join(rows)}</table>"

    async def cmd_help(self, ctx: Context) -> None:
        all_groups = [g for g, _ in self.HELP_GROUPS]
        wanted = fold(ctx.args)
        groups = [g for g in all_groups if wanted and fold(g).startswith(wanted)] or all_groups
        platform = self.board.platform
        limit = platform.message_length - 100
        # As much detail as fits into one message; a server's spam protection may kick for floods.
        for aliases, details in ((True, True), (False, True), (False, False)):
            text = self._help_html(ctx.prefix, groups, aliases=aliases, details=details)
            if platform.message_size(text) <= limit:
                break
        else:  # still too long: an overview, details per area with "!help musik"
            p = esc(ctx.prefix)
            lines = [f"<b>{title}</b>: " + " · ".join(f"<code>{p}{c.names[0]}</code>" for c in self.commands
                                                     if c.group == group)
                     for group, title in self.HELP_GROUPS if group in groups]
            text = "<br>".join(lines) + f"<br>Details: <code>{p}help musik</code>, <code>{p}help extras</code> …"
        self.reply(ctx, text)

    # -------------------------------------------------------------- collections

    async def cmd_collection(self, collection: Collection, ctx: Context) -> None:
        from .core import PlayError

        p = esc(ctx.prefix)
        if not collection.loaded:
            self.reply(ctx, f"{esc(collection.name)} wird gerade noch eingelesen, bitte gleich nochmal versuchen.")
            return
        query, groups = ctx.args.strip(), []
        if query.startswith("@"):
            tokens = query[1:].split()
            for n in range(len(tokens), 0, -1):  # "@lady faithful schiff": longest speaker name first
                groups = collection.find_groups(" ".join(tokens[:n]))
                if groups:  # several ("@tauren": male and female, player and NPC): any of them
                    query = " ".join(tokens[n:])
                    break
            if not groups:
                self.reply(ctx, f"Niemanden gefunden für <b>@{esc(' '.join(tokens[:1]))}</b> in "
                                f"{esc(collection.name)}. Alle Namen stehen im Webinterface.")
                return
        item, total = collection.pick(query, groups)
        if item is None:
            where = f" von {esc(', '.join(groups[:3]))}{' …' if len(groups) > 3 else ''}" if groups else ""
            self.reply(ctx, f"Keine Zeile{where} gefunden für <b>{esc(query)}</b>. "
                            f"Beispiel: <code>{p}{collection.commands[0]} {esc(collection.example_query())}</code>")
            return
        try:
            await self.board.play_item(collection, item, requested_by=ctx.sender, source=ctx.source)
        except PlayError as exc:
            self.reply(ctx, esc(str(exc)))
            return
        hits = f" <i>({total} Treffer)</i>" if (query or groups) and total > 1 else ""
        translation = f" <i>({esc(item.text_en)})</i>" if collection.mixed_languages and item.text_en else ""
        number = f" #{esc(item.number)}" if item.number else ""
        self.reply(ctx, f"&#127917; <b>{esc(item.who)}{number}</b>: „{esc(item.title)}“{translation}{hits}")

    # -------------------------------------------------------------------- music

    def _link_and_rest(self, ctx: Context) -> tuple[str | None, str]:
        """Split args into a leading URL and the rest. Prefers the link target the client sent
        (Mumble shortens long links in the visible text)."""
        tokens = ctx.args.split()
        if not tokens or not is_url(tokens[0]):
            return None, ctx.args
        url = next((h for h in ctx.msg.links if is_url(h)), tokens[0])
        return url, " ".join(tokens[1:])

    @staticmethod
    def _track_link(info) -> str:
        title = esc(info.title)
        if info.webpage and is_url(info.webpage):
            return f'<a href="{html.escape(info.webpage)}">{title}</a>'
        return title

    def _now_playing(self) -> str:
        music = self.board.music
        track = music.current
        if track is None:
            if music.queue and music.paused:
                return f"Musik pausiert, {len(music.queue)} Titel in der Warteschlange."
            return ""
        state = {"loading": "Lädt", "paused": "Pausiert", "playing": "Läuft"}.get(music.state, "Läuft")
        if track.info.is_live:
            progress = f"LIVE, seit {format_time(music.position)}"
        else:
            progress = f"{format_time(music.position)}/{format_time(track.info.duration)}"
        return f"&#127925; {state}: {self._track_link(track.info)} ({progress}, von {esc(track.requested_by)})"

    async def _music_add(self, ctx: Context, play_next: bool) -> None:
        url, rest = self._link_and_rest(ctx)
        query = url or rest
        if not query:
            self.reply(ctx, f"Was soll laufen? Beispiel: <code>{esc(ctx.prefix)}play https://youtu.be/...</code> "
                            f"oder <code>{esc(ctx.prefix)}play daft punk around the world</code>")
            return
        try:
            result = await self.board.music_add(query, requested_by=ctx.sender, source=ctx.source, play_next=play_next)
        except MediaError as exc:
            self.reply(ctx, f"Geht nicht: {esc(str(exc))}")
            return
        skipped = f" ({result.skipped} übersprungen: zu lang oder noch nicht gestartet)" if result.skipped else ""
        if len(result.tracks) > 1 or result.playlist_title:
            name = f" <b>{esc(result.playlist_title)}</b>" if result.playlist_title else ""
            self.reply(ctx, f"&#10133; Playlist{name}: {len(result.tracks)} Titel hinzugefügt{skipped}.")
            return
        track = result.tracks[0]
        length = self._length(track)
        if result.position == 0:
            self.reply(ctx, f"&#9654; {self._track_link(track.info)}{length}")
        else:
            self.reply(ctx, f"&#10133; {self._track_link(track.info)}{length} - Platz {result.position} "
                            "in der Warteschlange")

    @staticmethod
    def _length(track) -> str:
        if track.info.is_live:
            return " (LIVE)"
        return f" ({format_time(track.info.duration)})" if track.info.duration else ""

    async def cmd_radio(self, ctx: Context) -> None:
        p = esc(ctx.prefix)
        sub, _, rest = ctx.args.strip().partition(" ")
        stations = self.board.radio
        if not sub:
            if not stations.entries:
                self.reply(ctx, f"Noch keine Sender gespeichert. Beispiel: <code>{p}radio add lofi https://youtu.be/...</code>")
                return
            items = [f"<code>{p}radio {esc(r['name'])}</code> {esc(r['title'])}"
                     for r in sorted(stations.entries.values(), key=lambda r: r["name"])]
            self.reply_chunks(ctx, "<b>Sender:</b><br>", items, sep="<br>")
            return
        if sub.casefold() == "add":
            name, _, link = rest.strip().partition(" ")
            url, _ = self._link_and_rest(Context(ctx.msg, ctx.sender, link.strip(), ctx.prefix))
            if not name or not url:
                self.reply(ctx, f"So geht's: <code>{p}radio add lofi https://youtu.be/...</code>")
                return
            existing = stations.get(name)
            if existing is not None and not self._may_change_alias(ctx, existing):
                self.reply(ctx, f"Den Sender hat {esc(existing['created_by'])} angelegt, ändern dürfen das nur Admins.")
                return
            try:
                station = await self.board.radio_set(name, url, created_by=ctx.sender, source=ctx.source)
            except (ValueError, MediaError) as exc:
                self.reply(ctx, f"Geht nicht: {esc(str(exc))}")
                return
            self.reply(ctx, f"&#128251; Sender <code>{p}radio {esc(station['name'])}</code> gespeichert: "
                            f"{esc(station['title'])}")
            return
        if sub.casefold() in ("del", "rm", "remove"):
            name = rest.strip().casefold()
            existing = stations.get(name) if name else None
            if existing is None:
                self.reply(ctx, f"Einen Sender <b>{esc(name)}</b> gibt es nicht.")
                return
            if not self._may_change_alias(ctx, existing):
                self.reply(ctx, f"Den Sender hat {esc(existing['created_by'])} angelegt, löschen dürfen das nur Admins.")
                return
            self.board.radio_remove(name, removed_by=ctx.sender, source=ctx.source)
            self.reply(ctx, f"Sender <b>{esc(name)}</b> gelöscht.")
            return
        try:
            station, result = await self.board.radio_play(sub, requested_by=ctx.sender, source=ctx.source)
        except ValueError as exc:
            self.reply(ctx, f"{esc(str(exc))}. Alle Sender: <code>{p}radio</code>")
            return
        except MediaError as exc:
            self.reply(ctx, f"Geht nicht: {esc(str(exc))}")
            return
        title = esc(station["title"])
        if result is None:
            self.reply(ctx, f"&#128251; {title} läuft schon.")
        elif self.board.music.current is not None and not self.board.music.current.info.is_live \
                and result.position > 0:
            self.reply(ctx, f"&#128251; {title} kommt nach dem aktuellen Titel.")
        else:
            self.reply(ctx, f"&#128251; {title}")

    async def cmd_music_play(self, ctx: Context) -> None:
        await self._music_add(ctx, play_next=False)

    async def cmd_music_playnext(self, ctx: Context) -> None:
        await self._music_add(ctx, play_next=True)

    async def cmd_skip(self, ctx: Context) -> None:
        track = self.board.music_skip(requested_by=ctx.sender, source=ctx.source)
        if track is None:
            self.reply(ctx, "Es läuft gerade keine Musik.")
            return
        nxt = self.board.music.queue[0].info.title if self.board.music.queue else None
        self.reply(ctx, f"&#9197; {esc(track.info.title)} übersprungen." + (f" Weiter mit {esc(nxt)}." if nxt else ""))

    async def cmd_pause(self, ctx: Context) -> None:
        if not self.board.music_pause(requested_by=ctx.sender, source=ctx.source):
            self.reply(ctx, "Es läuft keine Musik." if not self.board.music.paused else "Ist schon pausiert.")

    async def cmd_resume(self, ctx: Context) -> None:
        if not self.board.music_resume(requested_by=ctx.sender, source=ctx.source):
            music = self.board.music
            self.reply(ctx, "Die Warteschlange ist leer." if not (music.current or music.queue) else "Läuft bereits.")

    async def cmd_queue(self, ctx: Context) -> None:
        music = self.board.music
        lines = []
        now = self._now_playing()
        if now:
            lines.append(now)
        if music.queue:
            known = all(t.info.duration and not t.info.is_live for t in music.queue)
            total = f", {format_time(sum(t.info.duration for t in music.queue))}" if known else ""
            lines.append(f"<b>Warteschlange ({len(music.queue)} Titel{total}):</b>")
            for i, track in enumerate(music.queue[:10], 1):
                length = self._length(track)
                lines.append(f"{i}. {esc(track.info.title)}{length} <i>- {esc(track.requested_by)}</i>")
            if len(music.queue) > 10:
                lines.append(f"... und {len(music.queue) - 10} weitere")
        if not lines:
            self.reply(ctx, f"Die Warteschlange ist leer. Musik mit <code>{esc(ctx.prefix)}play</code> hinzufügen.")
            return
        self.reply(ctx, "<br>".join(lines))

    async def cmd_remove(self, ctx: Context) -> None:
        try:
            position = int(ctx.args.strip().lstrip("#"))
        except ValueError:
            self.reply(ctx, f"Welche Nummer? Siehe <code>{esc(ctx.prefix)}queue</code>, z. B. <code>{esc(ctx.prefix)}remove 2</code>")
            return
        music = self.board.music
        if not 1 <= position <= len(music.queue):
            self.reply(ctx, f"Platz {position} gibt es nicht (Warteschlange: {len(music.queue)} Titel).")
            return
        track = self.board.music_remove(music.queue[position - 1].id, requested_by=ctx.sender, source=ctx.source)
        if track:
            self.reply(ctx, f"&#10006; {esc(track.info.title)} entfernt.")

    async def cmd_clear(self, ctx: Context) -> None:
        count = self.board.music_clear(requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, f"Warteschlange geleert ({count} Titel)." if count else "Die Warteschlange ist schon leer.")

    async def cmd_shuffle(self, ctx: Context) -> None:
        if len(self.board.music.queue) < 2:
            self.reply(ctx, "Zum Mischen braucht es mindestens zwei Titel.")
            return
        self.board.music_shuffle(requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, "&#128256; Warteschlange gemischt.")

    async def cmd_music_volume(self, ctx: Context) -> None:
        music = self.board.music
        if not ctx.args:
            self.reply(ctx, f"Musik-Lautstärke: <b>{music.volume} %</b>")
            return
        raw = ctx.args.rstrip("%").strip()
        try:
            value = music.volume + int(raw) if raw[:1] in ("+", "-") else int(raw)
        except ValueError:
            self.reply(ctx, "Bitte eine Zahl von 0 bis 100 angeben.")
            return
        value = self.board.music_volume(value, requested_by=ctx.sender, source=ctx.source)
        self.reply(ctx, f"Musik-Lautstärke: <b>{value} %</b>")

    async def cmd_clip(self, ctx: Context) -> None:
        p = esc(ctx.prefix)
        usage = f"Beispiel: <code>{p}clip https://youtu.be/... 1:23 1:27 mein sound</code>"
        url, rest = self._link_and_rest(ctx)
        parts = rest.split()
        if not url or len(parts) < 2:
            self.reply(ctx, usage)
            return
        try:
            start, end = parse_time(parts[0]), parse_time(parts[1])
        except ValueError as exc:
            self.reply(ctx, f"{esc(str(exc))}. {usage}")
            return
        name = " ".join(parts[2:])
        self.reply(ctx, f"&#9986; Schneide {format_time(start)} bis {format_time(end)} ...")
        try:
            sound = await self.board.create_clip(url, start, end, name=name, requested_by=ctx.sender, source=ctx.source)
        except MediaError as exc:
            self.reply(ctx, f"Clip fehlgeschlagen: {esc(str(exc))}")
            return
        self.reply(ctx, f"&#9986; Gespeichert als <b>{esc(sound.name)}</b> - abspielen mit <code>{p}sb {esc(sound.name)}</code>")

    # ------------------------------------------------------------------- extras

    async def cmd_tts(self, ctx: Context) -> None:
        from .core import PlayError

        if not ctx.args:
            self.reply(ctx, f"Was soll ich sagen? Beispiel: <code>{esc(ctx.prefix)}tts Pull in 10 Sekunden</code>")
            return
        try:
            await self.board.speak(ctx.args, requested_by=ctx.sender, source=ctx.source)
        except PlayError as exc:
            self.reply(ctx, f"Geht nicht: {esc(str(exc))}")

    async def cmd_pull(self, ctx: Context) -> None:
        from .core import PlayError

        arg = ctx.args.strip().casefold()
        if arg in ("stop", "abbrechen", "cancel", "aus", "off", "0"):
            if not await self.board.cancel_pull(requested_by=ctx.sender, source=ctx.source):
                self.reply(ctx, "Es läuft kein Pull-Timer.")
            return
        try:
            seconds = int(arg.rstrip("s")) if arg else 10
        except ValueError:
            self.reply(ctx, f"Beispiel: <code>{esc(ctx.prefix)}pull 15</code> oder <code>{esc(ctx.prefix)}pull stop</code>")
            return
        if not 3 <= seconds <= self.cfg.pull_max_seconds:
            self.reply(ctx, f"Bitte 3 bis {self.cfg.pull_max_seconds} Sekunden.")
            return
        try:
            await self.board.start_pull(seconds, requested_by=ctx.sender, source=ctx.source)
        except PlayError as exc:
            self.reply(ctx, f"Geht nicht: {esc(str(exc))}")

    def _resolve_sound(self, ctx: Context, query: str) -> Sound | None:
        match = self.board.library.resolve(query)
        if match.unique or (match.sounds and match.exact):
            return match.sounds[0]
        if match.sounds:
            self._suggest(ctx, match.sounds, query)
        else:
            self.reply(ctx, f"Kein Sound gefunden für <b>{esc(query)}</b>.")
        return None

    async def cmd_joinsound(self, ctx: Context) -> None:
        p = esc(ctx.prefix)
        tokens = ctx.args.split()
        target = ctx.sender
        if tokens and tokens[0].startswith("@") and len(tokens[0]) > 1:
            tokens[0] = tokens[0][1:]
            taken = 1
            for n in range(len(tokens), 0, -1):  # "@Lady Faithful leeroy": names may contain spaces
                online = self.board.platform.find_user(" ".join(tokens[:n]))
                if online is not None:
                    target, taken = online.name, n
                    break
            else:
                target = tokens[0]
            tokens = tokens[taken:]
        rest = " ".join(tokens)
        own = target.casefold() == ctx.sender.casefold()
        if not rest:
            sound = self.board.join_sound_for(target)
            who = "Dein" if own else f"{esc(target)}s"
            if sound:
                self.reply(ctx, f"{who} Join-Sound: <b>{esc(sound.name)}</b>")
            else:
                self.reply(ctx, f"{who} Join-Sound ist nicht gesetzt. Beispiel: <code>{p}joinsound leeroy</code>")
            return
        if not own and not ctx.admin:
            self.reply(ctx, "Join-Sounds für andere dürfen nur Admins setzen.")
            return
        if rest.casefold() in ("aus", "off", "-", "none", "keiner", "weg"):
            removed = self.board.remove_join_sound(target, set_by=ctx.sender, source=ctx.source)
            self.reply(ctx, "Join-Sound entfernt." if removed else "Es war kein Join-Sound gesetzt.")
            return
        sound = self._resolve_sound(ctx, rest)
        if sound is None:
            return
        target = self.board.set_join_sound(target, sound, set_by=ctx.sender, source=ctx.source)
        who = "Dein" if own else f"{esc(target)}s"
        self.reply(ctx, f"{who} Join-Sound ist jetzt <b>{esc(sound.name)}</b>.")

    def _may_change_alias(self, ctx: Context, alias: dict | None) -> bool:
        return alias is None or ctx.admin or \
            alias.get("created_by", "").casefold() == ctx.sender.casefold()

    async def cmd_alias(self, ctx: Context) -> None:
        p = esc(ctx.prefix)
        name, _, target = ctx.args.strip().partition(" ")
        name = name.lstrip("/!").casefold()
        target = target.strip()
        aliases = self.board.aliases
        if not name:
            if not aliases.entries:
                self.reply(ctx, f"Noch keine Kürzel. Beispiel: <code>{p}alias lr leeroy</code>")
                return
            items = [f"<code>{p}{esc(a['name'])}</code> → {esc(a['target'])}" for a in
                     sorted(aliases.entries.values(), key=lambda a: a["name"])]
            self.reply_chunks(ctx, f"<b>{len(items)} Kürzel</b><br>", items)
            return
        existing = aliases.get(name)
        if not target:
            if existing:
                self.reply(ctx, f"<code>{p}{esc(name)}</code> → {esc(existing['target'])} "
                                f"<i>(von {esc(existing.get('created_by', '?'))})</i>")
            else:
                self.reply(ctx, f"Kürzel <code>{p}{esc(name)}</code> gibt es nicht.")
            return
        if not self._may_change_alias(ctx, existing):
            self.reply(ctx, f"<code>{p}{esc(name)}</code> gehört {esc(existing['created_by'])}, "
                            "ändern dürfen das nur Admins.")
            return
        if not self._parse(target):
            sound = self._resolve_sound(ctx, target)  # a sound alias must point to an existing sound
            if sound is None:
                return
            target = sound.name
        try:
            key = self.board.set_alias(name, target, created_by=ctx.sender, source=ctx.source)
        except ValueError as exc:
            self.reply(ctx, esc(str(exc)))
            return
        self.reply(ctx, f"Kürzel <code>{p}{esc(key)}</code> → {esc(target)}")

    async def cmd_unalias(self, ctx: Context) -> None:
        name = ctx.args.strip().lstrip("/!").casefold()
        existing = self.board.aliases.get(name) if name else None
        if existing is None:
            self.reply(ctx, f"Kürzel <code>{esc(ctx.prefix)}{esc(name)}</code> gibt es nicht.")
            return
        if not self._may_change_alias(ctx, existing):
            self.reply(ctx, f"Das Kürzel gehört {esc(existing['created_by'])}, löschen dürfen das nur Admins.")
            return
        self.board.remove_alias(name, removed_by=ctx.sender, source=ctx.source)
        self.reply(ctx, f"Kürzel <code>{esc(ctx.prefix)}{esc(name)}</code> gelöscht.")
