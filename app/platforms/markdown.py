"""Turn the commands' Mumble-style HTML replies into Discord Markdown."""

from __future__ import annotations

import re
from html.parser import HTMLParser

DISCORD_LIMIT = 2000
EMBED_LIMIT = 4096  # description of one embed
_SPECIAL = re.compile(r"([\\*_~`|\[\]])")
_LINE_START = re.compile(r"^(#|>|-# )", re.MULTILINE)  # headings, quotes, subtext


def escape_markdown(text: str) -> str:
    return _SPECIAL.sub(r"\\\1", text)


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.code = 0
        self.link: str | None = None
        self.link_text: list[str] = []
        self.cells = 0

    def _emit(self, text: str) -> None:
        (self.link_text if self.link is not None else self.out).append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._emit("\n")
        elif tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag == "code":
            self.code += 1
            self._emit("`")
        elif tag == "a":
            self.link = dict(attrs).get("href") or ""
            self.link_text = []
        elif tag == "table":
            self._emit("\n")
        elif tag == "tr":
            self.cells = 0
        elif tag in ("td", "th"):
            if dict(attrs).get("colspan") and "".join(self.out).strip():
                self._emit("\n")  # a full-width cell is a section heading: blank line before it
            if self.cells:
                self._emit(" – ")
            self.cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag == "code":
            self.code = max(0, self.code - 1)
            self._emit("`")
        elif tag == "a" and self.link is not None:
            text, url = "".join(self.link_text).strip(), self.link
            self.link = None
            if not url or text == url or not url.startswith(("http://", "https://")):
                self._emit(text or url)
            else:
                # Masked link; Discord does not render the "(<url>)" form. Messages go out as embeds,
                # so the link gets no preview anyway.
                safe = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
                self._emit(f"[{text}]({safe})")
        elif tag in ("tr", "p", "table"):
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        data = data.replace("\xa0", " ")
        if self.code:
            self._emit(data.replace("`", "ˋ"))
        else:
            self._emit(escape_markdown(data))


def html_to_markdown(html_text: str) -> str:
    conv = _Converter()
    conv.feed(html_text)
    conv.close()
    text = "".join(conv.out)
    text = text.replace("****", "")  # <b></b> leftovers
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = _LINE_START.sub(r"\\\1", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split at line breaks into messages Discord accepts."""
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single overlong line
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
