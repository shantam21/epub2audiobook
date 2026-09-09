"""EPUB parsing: spine order, chapter titles, cover art, metadata.

Chapter order comes from the spine (reading order), not the table of contents,
because the TOC often skips or nests things. Titles come from the TOC when we
can match an href to it, and fall back to the first heading in the document.
"""

from __future__ import annotations

import posixpath
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# ebooklib is noisy about future defaults on every read_epub call, and bs4
# warns on every XHTML document because we deliberately use the lenient HTML
# parser -- real EPUBs are too malformed for the strict one.
warnings.filterwarnings("ignore", category=UserWarning, module="ebooklib")
warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib")
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

import ebooklib  # noqa: E402
from ebooklib import epub  # noqa: E402

# Structural pages that are noise in an audiobook.
_SKIP_TITLE_RE = re.compile(
    r"^\s*(table of )?contents\s*$|^\s*copyright\s*$|^\s*index\s*$|^\s*colophon\s*$",
    re.IGNORECASE,
)
_BLOCK_TAGS = ("p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr")


@dataclass
class RawChapter:
    idx: int
    title: str
    href: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Book:
    title: str
    author: str
    language: str
    description: str = ""
    cover: bytes | None = None
    cover_media_type: str = "image/jpeg"
    chapters: list[RawChapter] = field(default_factory=list)


def load(path: Path, min_chars: int = 250, skip_front_matter: bool = True) -> Book:
    """Read an EPUB into ordered chapters of plain text."""
    book = epub.read_epub(str(path))

    title = _meta(book, "title") or path.stem
    author = _meta(book, "creator") or "Unknown"
    language = _meta(book, "language") or "en"
    description = _meta(book, "description") or ""

    toc_titles = _toc_titles(book)
    cover, cover_type = _cover(book)

    chapters: list[RawChapter] = []
    for item in _spine_items(book):
        html = item.get_content().decode("utf-8", errors="replace")
        text = html_to_text(html)
        if not text.strip():
            continue

        href = item.get_name()
        heading = _first_heading(html)
        name = toc_titles.get(_norm_href(href)) or heading or ""

        if skip_front_matter:
            if len(text) < min_chars:
                continue
            if name and _SKIP_TITLE_RE.match(name):
                continue

        chapters.append(
            RawChapter(
                idx=len(chapters),
                title=name.strip() or f"Chapter {len(chapters) + 1}",
                href=href,
                text=text,
            )
        )

    return Book(
        title=title,
        author=author,
        language=language,
        description=description,
        cover=cover,
        cover_media_type=cover_type,
        chapters=chapters,
    )


def html_to_text(html: str) -> str:
    """Flatten XHTML into paragraph-separated plain text."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "head", "nav", "svg", "figure", "table"]):
        tag.decompose()

    # Footnote/endnote back-references read as gibberish out loud.
    for a in soup.find_all("a"):
        cls = " ".join(a.get("class") or [])
        if re.search(r"note|footnote|endnote|ref", cls, re.IGNORECASE) and len(a.get_text()) <= 4:
            a.decompose()
    for sup in soup.find_all("sup"):
        if re.fullmatch(r"[\d\s.,\[\]()*†‡]{0,8}", sup.get_text() or ""):
            sup.decompose()

    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n\n")
        tag.insert_after("\n\n")

    text = soup.get_text()
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


# -- internals -------------------------------------------------------------


def _meta(book: epub.EpubBook, name: str) -> str:
    values = book.get_metadata("DC", name)
    if not values:
        return ""
    return str(values[0][0]).strip()


def _spine_items(book: epub.EpubBook):
    """Documents in reading order, skipping the nav document."""
    seen: set[str] = set()
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if "nav" in (getattr(item, "properties", None) or []):
            continue
        if item.get_name() in seen:
            continue
        seen.add(item.get_name())
        yield item


def _norm_href(href: str) -> str:
    return posixpath.normpath(href.split("#", 1)[0]).lstrip("./").lower()


def _toc_titles(book: epub.EpubBook) -> dict[str, str]:
    """Map normalised href -> TOC label, flattening nested sections."""
    titles: dict[str, str] = {}

    def walk(nodes) -> None:
        for node in nodes:
            if isinstance(node, (list, tuple)):
                walk(node)
            elif isinstance(node, epub.Section):
                if node.href:
                    titles.setdefault(_norm_href(node.href), node.title)
            elif isinstance(node, epub.Link):
                titles.setdefault(_norm_href(node.href), node.title)

    try:
        walk(book.toc)
    except Exception:  # a malformed TOC should never fail the conversion
        pass
    return titles


def _first_heading(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for level in ("h1", "h2", "h3", "title"):
        tag = soup.find(level)
        if tag:
            text = re.sub(r"\s+", " ", tag.get_text()).strip()
            if text:
                return text[:120]
    return ""


def _cover(book: epub.EpubBook) -> tuple[bytes | None, str]:
    candidates = list(book.get_items_of_type(ebooklib.ITEM_COVER))
    if not candidates:
        # Most EPUB3 files mark the cover with a manifest property instead.
        candidates = [
            item
            for item in book.get_items_of_type(ebooklib.ITEM_IMAGE)
            if "cover-image" in (getattr(item, "properties", None) or [])
            or "cover" in item.get_name().lower()
            or "cover" in (item.get_id() or "").lower()
        ]
    if not candidates:
        return None, "image/jpeg"
    item = candidates[0]
    media_type = getattr(item, "media_type", "") or "image/jpeg"
    return item.get_content(), media_type
