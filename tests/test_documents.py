"""Extraction has to be honest about what it could not read.

The failure this suite exists to prevent is a scanned resume coming back as an
empty string and a strong candidate reading as a blank page. Every path that
produces no text must say so distinctly enough for the caller to fall back.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from cats_mcp.documents import (
    MAX_TEXT_CHARS,
    Outcome,
    extract_text,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    """A minimal single-page PDF with a real text layer.

    Built by hand rather than through pypdf's writer: constructing a text stream
    through the writer means reaching into private helpers, and a fixture that
    depends on library internals breaks on upgrade for reasons that have nothing
    to do with the code under test.
    """
    drawn = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    content = f"BT /F1 12 Tf 72 720 Td {drawn} ET".encode()

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_blank_pdf() -> bytes:
    """A page with no text layer - what a scanned resume looks like."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- The three formats that failed in production ------------------------------


def test_docx_extracts_text():
    """DOCX was one of the formats a live review could not read."""
    data = make_docx(["Donald Fraser", "Heavy Duty Mechanic", "Red Seal certified 2011"])
    result = extract_text(data, "donald_fraser_resume.docx")

    assert result.outcome is Outcome.EXTRACTED
    assert "Donald Fraser" in result.text
    assert "Red Seal certified 2011" in result.text


def test_html_extracts_text_and_drops_markup():
    data = b"""<html><head><style>.x{color:red}</style>
    <script>var a=1;</script></head>
    <body><h1>Emile Smith</h1><p>Millwright, 12 years</p></body></html>"""
    result = extract_text(data, "emile_smith.html")

    assert result.outcome is Outcome.EXTRACTED
    assert "Emile Smith" in result.text
    assert "Millwright, 12 years" in result.text
    assert "var a=1" not in result.text
    assert "color:red" not in result.text


def test_plain_text_extracts():
    result = extract_text(b"Ian Adams\nHeavy equipment operator\nAvailable September", "cv.txt")

    assert result.outcome is Outcome.EXTRACTED
    assert "Ian Adams" in result.text


def test_pdf_extracts_text():
    data = make_pdf(["Brad Willows", "Journeyman Heavy Duty Mechanic", "Mining and field service"])
    result = extract_text(data, "brad.pdf")

    assert result.outcome is Outcome.EXTRACTED
    assert "Brad Willows" in result.text
    assert "pypdf" in result.parser


# --- The silent failure this module exists to prevent -------------------------


def test_scanned_pdf_reports_empty_not_success():
    """A PDF with no text layer must never come back as an empty success.

    This is the failure that would make a twenty-year veteran read as a blank
    page. The outcome has to be distinguishable so the caller can fall back to
    reading the file as an image.
    """
    result = extract_text(make_blank_pdf(), "scanned.pdf")

    assert result.outcome is Outcome.EMPTY
    assert result.text == ""
    assert "scan" in result.note.lower()
    assert "download_attachment" in result.note


def test_empty_bytes_report_empty():
    result = extract_text(b"", "nothing.pdf")

    assert result.outcome is Outcome.EMPTY
    assert not result.ok


# --- Photographed resumes -----------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400


@pytest.mark.parametrize(
    "data,filename,expected",
    [
        (JPEG, "resume_scan.jpg", "JPEG"),
        (PNG, "my_cv.png", "PNG"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 200, "cv.webp", "WEBP"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 200, "photo.heic", "HEIC"),
    ],
)
def test_an_image_resume_is_reported_as_an_image(data, filename, expected):
    """People photograph their resume. That is a readable document, not a failure."""
    result = extract_text(data, filename)

    assert result.outcome is Outcome.IMAGE
    assert result.parser == expected
    assert "download_attachment" in result.note


def test_image_is_distinct_from_unsupported():
    """An image can be read - by a multimodal model. RTF cannot be read at all.

    Collapsing these would tell a caller to give up on a resume it could
    actually have looked at.
    """
    image = extract_text(JPEG, "resume.jpg")
    unsupported = extract_text(b"{\\rtf1 whatever}", "resume.rtf")

    assert image.outcome is Outcome.IMAGE
    assert unsupported.outcome is Outcome.UNSUPPORTED


def test_image_detected_by_bytes_when_the_extension_lies():
    result = extract_text(JPEG, "resume.pdf")

    assert result.outcome is Outcome.IMAGE
    assert result.parser == "JPEG"


def test_image_detected_by_extension_when_the_bytes_are_unfamiliar():
    result = extract_text(b"\x00\x01\x02\x03 some encoding we do not know", "scan.tiff")

    assert result.outcome is Outcome.IMAGE


# --- Formats we knowingly do not handle ---------------------------------------


@pytest.mark.parametrize("filename", ["old.doc", "notes.rtf", "archive.7z"])
def test_unsupported_formats_say_so(filename):
    result = extract_text(b"\x00\x01 not a document at all", filename)

    assert result.outcome is Outcome.UNSUPPORTED
    assert "download_attachment" in result.note


def test_unsupported_is_distinct_from_empty():
    """Different problems that need different responses from the caller."""
    unsupported = extract_text(b"\x00\x01\x02 binary", "resume.rtf")
    empty = extract_text(make_blank_pdf(), "resume.pdf")

    assert unsupported.outcome is not empty.outcome


# --- Corrupt input must not take the batch down -------------------------------


def test_corrupt_pdf_fails_without_raising():
    result = extract_text(b"%PDF-1.4\ntruncated garbage", "broken.pdf")

    assert result.outcome is Outcome.FAILED
    assert result.note


def test_zip_that_is_not_a_docx_fails_cleanly():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("something/else.txt", "not word xml")

    result = extract_text(buffer.getvalue(), "renamed.docx")

    assert result.outcome is Outcome.FAILED
    assert "document.xml" in result.note


# --- Content sniffing beats the filename --------------------------------------


def test_docx_mislabelled_as_pdf_is_still_read():
    """Applicants rename files. The bytes are the truth."""
    data = make_docx(["Paulina Kowal", "Site supervisor"])
    result = extract_text(data, "paulina_resume.pdf")

    assert result.outcome is Outcome.EXTRACTED
    assert "Paulina Kowal" in result.text


def test_html_without_an_html_extension_is_still_read():
    result = extract_text(b"<html><body><p>Angelina Ruiz</p></body></html>", "resume.dat")

    assert result.outcome is Outcome.EXTRACTED
    assert "Angelina Ruiz" in result.text


# --- Bounds -------------------------------------------------------------------


def test_long_document_is_truncated_and_says_so():
    data = ("Welding ticket renewal history. " * 4000).encode()
    result = extract_text(data, "long.txt")

    assert result.outcome is Outcome.EXTRACTED
    assert len(result.text) == MAX_TEXT_CHARS
    assert "Truncated" in result.note


def test_line_structure_survives_tidying():
    """Layout carries meaning in a resume - a title on its own line, dates in a column."""
    result = extract_text(b"Foreman\n2019-2024\n\n\n\nSafety lead\n2015-2019", "r.txt")

    assert result.outcome is Outcome.EXTRACTED
    assert "Foreman\n2019-2024" in result.text
    assert "\n\n\n" not in result.text
