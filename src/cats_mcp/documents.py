"""Turning an attachment's bytes into text the model can read.

Why this exists
---------------
`find_candidate_resume` returns the document itself and lets the model read it.
That is correct for one candidate and unusable for twenty: each resume costs a
tool call, a model round trip, and a whole document held in context. A review of
21 candidates measured 27 CATS requests - six of them refetches, because a
retried read re-downloads - and three outright failures where the model's own
extractor did not handle the format.

Extracting server-side changes the shape of the work. One tool call returns
twenty resumes as text, the model reads them in one turn, and the formats that
fail do so visibly rather than as an empty answer.

The formats
-----------
PDF is the only one that needs a dependency. DOCX is a zip containing
`word/document.xml`; HTML and plain text need nothing. Handling those three with
the standard library is what keeps the dependency list at one entry.

What must never happen silently
-------------------------------
A scanned resume is a PDF with no text layer. Extraction "succeeds" and returns
nothing, and a candidate with twenty years of experience reads as a blank page.
Every result therefore carries an explicit outcome, and an empty extraction is
reported as `EMPTY`, never as text. The caller decides whether to fall back to
reading the file itself - it cannot make that decision if the failure is
invisible.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from xml.etree import ElementTree

from cats_mcp.http.correlation import get_logger

logger = get_logger(__name__)

#: Below this many characters an extraction is treated as having produced
#: nothing useful. A real resume clears this by an order of magnitude; a scanned
#: PDF typically yields a stray ligature or two from an embedded logo.
MIN_USEFUL_CHARS = 40

#: Hard ceiling on returned text per document. A pathological PDF can hold
#: hundreds of pages; a resume does not. Truncation is reported, never silent.
MAX_TEXT_CHARS = 40_000

#: WordprocessingML namespace. Paragraphs are `w:p`, text runs are `w:t`.
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Three or more blank lines collapse to one. PDF extraction is full of these.
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

#: Runs of spaces and tabs, which column-based PDF layouts produce in quantity.
_EXCESS_SPACES = re.compile(r"[ \t]{2,}")


class Outcome(str, Enum):
    """What happened when the bytes were parsed.

    `EMPTY` is deliberately distinct from `UNSUPPORTED`. Unsupported means this
    module cannot read the format at all; empty means it read the format
    correctly and there was no text in it, which for a PDF almost always means
    a scan. The two want different responses from the caller.
    """

    EXTRACTED = "extracted"
    EMPTY = "empty"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class Extraction:
    """The result of trying to read one document."""

    outcome: Outcome
    text: str = ""
    #: Which parser ran, for the caller to report. Not the same as the format
    #: claimed by the filename, which is frequently wrong.
    parser: str = ""
    #: Present on every non-EXTRACTED outcome, and on a truncated success.
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.EXTRACTED


class _TextHTMLParser(HTMLParser):
    """Collect visible text, dropping script and style content."""

    _SKIP = frozenset({"script", "style", "head", "meta", "link"})
    _BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _tidy(raw: str) -> str:
    """Normalise whitespace without destroying the line structure of a resume.

    Layout carries meaning here - a job title on its own line, dates in a
    column - so lines are preserved and only runs are collapsed.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _EXCESS_SPACES.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _EXCESS_BLANK_LINES.sub("\n\n", text).strip()


def _decode(data: bytes) -> str:
    """Best-effort decode. Resumes arrive in whatever the applicant's machine used."""
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages), f"pypdf ({len(reader.pages)} pages)"


def _extract_docx(data: bytes) -> tuple[str, str]:
    """Read WordprocessingML without a dependency.

    A .docx is a zip whose `word/document.xml` holds the body. Walking `w:p`
    elements and joining their `w:t` runs recovers the text with paragraph
    breaks intact, which is all a resume needs.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise ValueError(
                "zip archive has no word/document.xml - not a .docx, possibly a "
                "legacy .doc renamed"
            ) from exc

    root = ElementTree.fromstring(xml)
    paragraphs = []
    for para in root.iter(f"{_W_NS}p"):
        runs = [node.text for node in para.iter(f"{_W_NS}t") if node.text]
        if runs:
            paragraphs.append("".join(runs))
    return "\n".join(paragraphs), "zipfile+ElementTree"


def _extract_html(data: bytes) -> tuple[str, str]:
    parser = _TextHTMLParser()
    parser.feed(_decode(data))
    parser.close()
    return parser.text(), "html.parser"


def _extract_text(data: bytes) -> tuple[str, str]:
    return _decode(data), "decode"


#: Magic bytes, checked before the filename. A resume named `.pdf` that is
#: actually a Word document is common enough to be worth handling, and sniffing
#: costs nothing.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

#: Image signatures. A photographed or screenshotted resume is common - people
#: send what their phone produced - and it is a document this server cannot turn
#: into text but a multimodal model can read directly. That distinction is why
#: IMAGE exists as an outcome separate from UNSUPPORTED.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)

#: Extensions that mean an image even when the bytes are unavailable or odd.
IMAGE_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp", "heic", "heif"}
)


def _image_format(data: bytes, extension: str) -> str | None:
    """The image format this looks like, or None if it is not an image."""
    for signature, label in _IMAGE_MAGIC:
        if data.startswith(signature):
            return label
    # RIFF....WEBP - the size field sits between the two markers.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    # HEIC and friends put the brand at offset 4, after the box length.
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"heif", b"mif1"):
        return "HEIC"
    if extension in IMAGE_EXTENSIONS:
        return extension.upper()
    return None

_BY_EXTENSION = {
    "pdf": (_extract_pdf, "PDF"),
    "docx": (_extract_docx, "DOCX"),
    "html": (_extract_html, "HTML"),
    "htm": (_extract_html, "HTML"),
    "txt": (_extract_text, "text"),
    "text": (_extract_text, "text"),
    "md": (_extract_text, "text"),
    "rtf": (None, "RTF"),
    "doc": (None, "legacy .doc"),
}


def _looks_like_html(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<body" in head


def extract_text(data: bytes, filename: str | None = None) -> Extraction:
    """Read `data` as text, choosing a parser by content and then by filename.

    Never raises for an unreadable document - an unreadable resume is an
    expected outcome in a batch of twenty, and failing the batch over one of
    them would be worse than reporting it.
    """
    if not data:
        return Extraction(Outcome.EMPTY, note="The attachment was zero bytes.")

    name = (filename or "").lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""

    # An image is not a failure to read, it is a document in a form this server
    # cannot turn into text but a multimodal model can read as it is. Checked
    # before the text parsers so a photographed resume never lands in
    # UNSUPPORTED alongside formats nothing can read.
    image = _image_format(data, extension)
    if image is not None:
        return Extraction(
            Outcome.IMAGE,
            parser=image,
            note=(
                f"This resume is a {image} image - a photograph or screenshot "
                f"rather than a text document. There is no text to extract. "
                f"Fetch it with download_attachment to read it as an image."
            ),
        )

    # Content wins over the filename, which is routinely wrong.
    if data.startswith(_PDF_MAGIC):
        handler, label = _extract_pdf, "PDF"
    elif data.startswith(_ZIP_MAGIC):
        # No guard on the extension here. A zip is never a PDF whatever the file
        # is called, and applicants rename resumes constantly.
        handler, label = _extract_docx, "DOCX"
    elif _looks_like_html(data):
        handler, label = _extract_html, "HTML"
    else:
        handler, label = _BY_EXTENSION.get(extension, (None, extension or "unknown"))

    if handler is None:
        return Extraction(
            Outcome.UNSUPPORTED,
            parser="",
            note=(
                f"{label} is not a format this server extracts. Fetch the file "
                f"with download_attachment and read it directly."
            ),
        )

    try:
        raw, parser = handler(data)
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not fail a batch
        logger.info("extraction failed for %s: %s", filename or "<unnamed>", exc)
        return Extraction(
            Outcome.FAILED,
            parser=label,
            note=f"Could not parse this {label} file: {exc}",
        )

    text = _tidy(raw)

    # The near-empty threshold is a scan detector, and only PDFs get scanned.
    # Applying it to the other formats would discard a genuinely short document -
    # a one-line HTML note is not a photograph of a resume.
    if label == "PDF" and len(text) < MIN_USEFUL_CHARS:
        return Extraction(
            Outcome.EMPTY,
            parser=parser,
            note=(
                f"The PDF parsed correctly but contained essentially no text "
                f"({len(text)} characters). This usually means a scan or "
                f"photographed document with no text layer. Fetch it with "
                f"download_attachment to read it as an image."
            ),
        )

    if not text:
        return Extraction(
            Outcome.EMPTY,
            parser=parser,
            note=(
                f"The {label} parsed correctly and contained no text at all. "
                f"Fetch it with download_attachment if you need to inspect it."
            ),
        )

    if len(text) > MAX_TEXT_CHARS:
        return Extraction(
            Outcome.EXTRACTED,
            text=text[:MAX_TEXT_CHARS],
            parser=parser,
            note=(
                f"Truncated to {MAX_TEXT_CHARS} characters from {len(text)}. "
                f"Fetch the file with download_attachment if the tail matters."
            ),
        )

    return Extraction(Outcome.EXTRACTED, text=text, parser=parser)


__all__ = ["Extraction", "Outcome", "extract_text", "MAX_TEXT_CHARS", "MIN_USEFUL_CHARS"]
