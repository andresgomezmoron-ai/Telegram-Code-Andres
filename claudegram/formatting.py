"""Turn Claude's Markdown into something Telegram renders on a phone.

Two jobs:

1. Split a long answer into pieces that fit Telegram's 4096-character limit,
   without ever cutting a fenced code block in half.
2. Convert Markdown into the small HTML subset Telegram accepts, so code blocks
   arrive as real code blocks instead of literal backticks.

Every chunk also carries its plain-text source: if Telegram rejects our HTML we
resend the plain version instead of losing the answer, which matters more than
formatting when the link is a plane's satellite connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Telegram's hard cap for a single message.
TELEGRAM_LIMIT = 4096
# Markdown budget per chunk. Lower than the cap because escaping and tags make
# the HTML longer than the source.
SOURCE_BUDGET = 2800

_SENTINEL = "\x00"
_FENCE_RE = re.compile(r"^(\s{0,3})```(.*)$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+|tg://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")


@dataclass(frozen=True)
class Chunk:
    """One Telegram message: HTML first, plain Markdown as the fallback."""

    html: str
    plain: str


def telegram_length(text: str) -> int:
    """Telegram counts UTF-16 code units, so an emoji costs 2, not 1."""
    return len(text.encode("utf-16-le", errors="replace")) // 2


def render(text: str, *, limit: int = TELEGRAM_LIMIT, budget: int = SOURCE_BUDGET) -> list[Chunk]:
    """Split `text` into Telegram-sized chunks and convert each one to HTML."""
    text = text.strip("\n")
    if not text.strip():
        return []
    chunks: list[Chunk] = []
    for source in split_markdown(text, budget):
        chunks.extend(_render_one(source, limit=limit, budget=budget))
    return chunks


def _render_one(source: str, *, limit: int, budget: int) -> list[Chunk]:
    html = md_to_html(source)
    if telegram_length(html) <= limit:
        return [Chunk(html=html, plain=source)]
    # Escaping blew past the cap (lots of <, > or &). Re-split more finely.
    if budget <= 200:
        return [Chunk(html=escape_html(source[:limit]), plain=source[:limit])]
    smaller = max(200, budget // 2)
    out: list[Chunk] = []
    for piece in split_markdown(source, smaller):
        out.extend(_render_one(piece, limit=limit, budget=smaller))
    return out


def split_markdown(text: str, budget: int) -> list[str]:
    """Split Markdown at line boundaries, keeping code fences balanced.

    A chunk that ends inside a ``` block gets the fence closed, and the next
    chunk reopens it with the same language, so every message is valid on its
    own.
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence_lang: str | None = None  # language of the fence we are inside, if any

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        body = "\n".join(current)
        if fence_lang is not None:
            body += "\n```"
        chunks.append(body.strip("\n"))
        current = []
        size = 0

    for line in lines:
        fence = _FENCE_RE.match(line)
        # Reserve room for the closing fence we may have to add.
        room = budget - (4 if fence_lang is not None else 0)
        for piece in _hard_wrap(line, max(40, room)):
            cost = len(piece) + 1
            if current and size + cost > room:
                flush()
                if fence_lang is not None:
                    current = ["```" + fence_lang]
                    size = len(current[0]) + 1
            current.append(piece)
            size += cost
        if fence is not None:
            fence_lang = None if fence_lang is not None else fence.group(2).strip()

    flush()
    return [chunk for chunk in chunks if chunk.strip()]


def _hard_wrap(line: str, width: int) -> list[str]:
    """Break a single over-long line, preferring the last space before the cut."""
    if len(line) <= width:
        return [line]
    pieces: list[str] = []
    rest = line
    while len(rest) > width:
        cut = rest.rfind(" ", width // 2, width)
        if cut <= 0:
            cut = width
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def split_plain(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split ready-made text (ours, not Claude's) at line boundaries."""
    if telegram_length(text) <= limit:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        for part in _hard_wrap(line, limit - 1):
            cost = telegram_length(part) + 1
            if current and size + cost > limit:
                pieces.append("\n".join(current))
                current, size = [], 0
            current.append(part)
            size += cost
    if current:
        pieces.append("\n".join(current))
    return [piece for piece in pieces if piece.strip()]


def strip_tags(html: str) -> str:
    """Last-resort plain text if Telegram rejects our markup."""
    text = re.sub(r"<[^>]+>", "", html)
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(text: str) -> str:
    """Convert Markdown to the tag subset Telegram supports.

    Deliberately conservative: anything not recognised is escaped and passed
    through. Underscore emphasis is ignored on purpose, because snake_case
    identifiers are far more common in these conversations than _italics_.
    """
    text = text.replace(_SENTINEL, "")
    out: list[str] = []
    buffer: list[str] = []
    fence_lang: str | None = None

    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence is not None:
            if fence_lang is None:
                out.append(_inline_block("\n".join(buffer)))
                buffer = []
                fence_lang = fence.group(2).strip()
            else:
                out.append(_code_block("\n".join(buffer), fence_lang))
                buffer = []
                fence_lang = None
            continue
        buffer.append(line)

    if fence_lang is not None:  # unterminated fence: still render it as code
        out.append(_code_block("\n".join(buffer), fence_lang))
    else:
        out.append(_inline_block("\n".join(buffer)))
    return "\n".join(part for part in out if part != "").strip("\n")


def _code_block(body: str, lang: str) -> str:
    body = escape_html(body.strip("\n"))
    language = re.sub(r"[^A-Za-z0-9+#._-]", "", lang).lower()
    if language:
        return f'<pre><code class="language-{language}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _inline_block(body: str) -> str:
    if not body.strip():
        return ""

    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"{_SENTINEL}{len(slots) - 1}{_SENTINEL}"

    body = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{escape_html(m.group(1))}</code>"), body)
    body = _LINK_RE.sub(
        lambda m: stash(
            '<a href="{}">{}</a>'.format(
                escape_html(m.group(2)).replace('"', "%22"), escape_html(m.group(1))
            )
        ),
        body,
    )

    # Structure is detected on the raw line (so '>' is still '>'), and only the
    # remaining content gets escaped.
    rendered: list[str] = []
    quote: list[str] = []

    def close_quote() -> None:
        if quote:
            rendered.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in body.split("\n"):
        if _QUOTE_RE.match(line):
            quote.append(_markup(_QUOTE_RE.sub("", line)))
            continue
        close_quote()
        heading = _HEADING_RE.match(line)
        if heading is not None:
            rendered.append("<b>" + _markup(heading.group(2).strip()) + "</b>")
            continue
        if _HR_RE.match(line):
            rendered.append("——————————")
            continue
        rendered.append(_markup(_BULLET_RE.sub(r"\1• ", line)))
    close_quote()

    html = "\n".join(rendered)
    for index, replacement in enumerate(slots):
        html = html.replace(f"{_SENTINEL}{index}{_SENTINEL}", replacement)
    return html


def _markup(line: str) -> str:
    """Escape a line's content, then apply the inline emphasis rules."""
    line = escape_html(line)
    line = _BOLD_RE.sub(r"<b>\1</b>", line)
    line = _STRIKE_RE.sub(r"<s>\1</s>", line)
    line = _ITALIC_RE.sub(r"<i>\1</i>", line)
    return line
