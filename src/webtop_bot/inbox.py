"""Teachers' messages from the Webtop inbox."""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, List, Optional, Sequence, Tuple

from webtop import WebtopClient

from .homework import Kid, _checked

logger = logging.getLogger(__name__)

MAX_BODY = 3000  # chars of plain text; the rest is on Webtop


@dataclass(frozen=True)
class Header:
    """A message as listed in the inbox."""
    webtop_id: str  # Webtop's messageId; stays the same across logins
    sent_at: dt.datetime
    sender: str
    subject: str
    has_files: bool


@dataclass(frozen=True)
class Body:
    """What opening the message shows."""
    text: str  # plain text
    recipients: str  # "last first, last first, ..."
    files: Tuple[str, ...]  # attachment names
    action: str  # "" | "signature" | "survey": the parent is asked to answer on Webtop


@dataclass(frozen=True)
class Message:
    id: int  # local row id (Webtop's id is too long for button data)
    header: Header
    body: Optional[Body]  # None until fetched
    kid_ids: Tuple[str, ...]  # kids among the recipients; empty = not about a specific kid


BLOCK_TAGS = {"p", "div", "br", "li", "tr", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._href: Optional[str] = None
        self._link_text: List[str] = []
        self._skip = 0  # inside <style> / <script>

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith(("http://", "https://")) else None
            self._link_text = []
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a" and self._href:
            text = "".join(self._link_text).strip()
            if text and text != self._href:
                self.parts.append(f" ({self._href})")
            elif not text:
                self.parts.append(self._href)
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._href:
            self._link_text.append(data)


def html_to_text(html: str) -> str:
    """Webtop's message HTML as plain text: one line per block, at most one blank line between."""
    parser = _TextParser()
    parser.feed(html or "")
    parser.close()
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def kids_in(recipients: str, kids: Sequence[Kid]) -> Tuple[str, ...]:
    """The kids named in a recipients list ("אלנטוך יואב, ..."; any word order)."""
    names = [set(r.split()) for r in recipients.split(",")]
    return tuple(k.id for k in kids if set(k.name.split()) in names)


def _header(r: Any) -> Optional[Header]:
    try:
        sent_at = dt.datetime.fromisoformat(r["sendingDate"])
    except (KeyError, TypeError, ValueError):
        return None
    sender = " ".join(filter(None, [r.get("student_F_name"), r.get("student_L_name")])) or r.get("fromTitle") or ""
    return Header(
        webtop_id=r["messageId"],
        sent_at=sent_at,
        sender=sender.strip(),
        subject=(r.get("subject") or "").strip(),
        has_files=bool(r.get("filesWereAttached")),
    )


async def fetch_headers(client: WebtopClient) -> List[Header]:
    """The inbox's first page, newest first (older pages hold nothing new)."""
    rows = _checked(await client.get_messages_inbox(page_id=1), "inbox") or []
    return [h for h in map(_header, rows) if h and h.webtop_id]


def _file_name(f: Any) -> str:
    if isinstance(f, dict):
        for key, value in f.items():
            if "name" in key.lower() and isinstance(value, str) and value.strip():
                return value.strip()
    return f.strip() if isinstance(f, str) else ""


async def fetch_body(client: WebtopClient, webtop_id: str) -> Body:
    """Open a message (Webtop marks it read)."""
    data = _checked(await client.get_message(message_id=webtop_id), "message") or {}
    msg = data.get("messageData") or {}
    text = html_to_text(msg.get("messageContent") or "")
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY].rstrip() + "…"
    files = tuple(n for n in map(_file_name, msg.get("filesList") or []) if n)
    if not files and msg.get("filesWereAttached"):
        files = ("קובץ מצורף",)
    action = "signature" if data.get("signature") else "survey" if data.get("survey") else ""
    return Body(text=text, recipients=(msg.get("recipientsName") or "").strip(), files=files, action=action)
