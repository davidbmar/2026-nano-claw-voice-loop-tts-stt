"""Extraction, and the refusals that matter more than the happy path.

A tax document arrives as a PDF, and the common bad case is a scan: parseable,
zero text. Ingesting that produces a document that exists, lists fine, and
answers nothing — so the empty extraction is the case with the most tests.
"""

from __future__ import annotations

import pytest

from voice.documents import (
    DocumentTooLarge,
    MAX_DOCUMENT_BYTES,
    NoTextLayer,
    UnsupportedDocument,
    classify,
    extract,
    tidy,
)

SAMPLE = "Form W-2 Wage and Tax Statement for the year 2025. Employer copy."


def test_supported_types_are_classified_by_extension_not_media_type():
    # Browsers routinely send application/octet-stream for .docx; the
    # extension is both more reliable and what the customer sees.
    assert classify("w2.pdf") == "pdf"
    assert classify("Notes.MD") == "text"
    assert classify("handbook.docx") == "docx"


def test_an_unsupported_type_names_what_is_accepted():
    with pytest.raises(UnsupportedDocument) as excinfo:
        classify("scan.tiff")
    assert excinfo.value.status == 415
    assert "PDF" in excinfo.value.message


def test_a_file_with_no_extension_is_refused_rather_than_guessed():
    with pytest.raises(UnsupportedDocument):
        classify("receipts")


def test_plain_text_round_trips():
    kind, text = extract("notes.txt", SAMPLE.encode("utf-8"))
    assert kind == "text"
    assert text == SAMPLE


def test_undecodable_bytes_do_not_crash_the_upload():
    kind, text = extract("notes.txt", b"caf\xe9 wages and tax " + SAMPLE.encode())
    assert kind == "text"
    assert "wages" in text


def test_an_oversize_file_is_refused_before_it_is_parsed():
    with pytest.raises(DocumentTooLarge) as excinfo:
        extract("big.txt", b"x" * (MAX_DOCUMENT_BYTES + 1))
    assert excinfo.value.status == 413


def test_an_empty_file_is_refused():
    with pytest.raises(NoTextLayer):
        extract("empty.txt", b"")


def test_a_file_of_whitespace_is_refused():
    with pytest.raises(NoTextLayer):
        extract("blank.txt", b"   \n\n   \t  \n")


def test_tidy_collapses_padding_but_keeps_paragraphs():
    # The platform segments in windows that never cross a section boundary, so
    # blank lines carry meaning; runs of spaces and page padding do not.
    assert tidy("a  \t  b\r\n\r\n\r\n\r\nc   \n") == "a b\n\nc"


def test_tidy_strips_nulls_that_would_break_sqlite_text():
    assert "\x00" not in tidy("wages\x00 and tax")


# ---- PDF -----------------------------------------------------------------
#
# pypdf ships in the container image (voice/requirements.txt) but is not
# required to run the rest of this suite on a bare checkout, so the skip is
# per-test rather than module-wide — otherwise a missing optional dependency
# would silently take the extraction tests above with it.

def _pdf(page_count: int) -> bytes:
    """Build a real PDF; a hand-rolled byte string would not exercise pypdf."""

    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_a_scanned_pdf_is_refused_with_an_explanation_not_ingested_empty():
    pytest.importorskip("pypdf")
    # Blank pages are exactly what a scan looks like to a text extractor: the
    # file parses, and every page yields "".
    with pytest.raises(NoTextLayer) as excinfo:
        extract("scan.pdf", _pdf(3))
    message = excinfo.value.message
    assert excinfo.value.status == 422
    assert "scan" in message.lower(), "the customer needs to know why, not just no"


def test_a_corrupt_pdf_fails_as_a_document_error_not_a_stack_trace():
    pytest.importorskip("pypdf")
    from voice.documents import DocumentError

    with pytest.raises(DocumentError) as excinfo:
        extract("broken.pdf", b"%PDF-1.7\nnot really a pdf at all")
    assert excinfo.value.status in (422, 502)


def _pdf_with_text(body: str) -> bytes:
    """A minimal single-page PDF carrying a real text-drawing operator."""

    content = f"BT /F1 24 Tf 72 720 Td ({body}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body_bytes in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body_bytes + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)


def test_text_actually_comes_out_of_a_real_pdf():
    # The refusal tests above would all pass if extraction returned "" for
    # every PDF ever. This is the one that says the reader reads.
    pytest.importorskip("pypdf")
    kind, text = extract("w2.pdf", _pdf_with_text("Form W-2 Wage and Tax Statement 2025"))
    assert kind == "pdf"
    assert "Wage and Tax Statement" in text


def test_docx_paragraphs_and_table_cells_are_both_extracted():
    docx = pytest.importorskip("docx")
    import io

    document = docx.Document()
    document.add_paragraph("Deductions summary for tax year 2025")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Mortgage interest"
    table.rows[0].cells[1].text = "4210.00"
    buffer = io.BytesIO()
    document.save(buffer)

    kind, text = extract("summary.docx", buffer.getvalue())
    assert kind == "docx"
    assert "Deductions summary" in text
    # Tables carry the numbers in a tax document — dropping them would lose
    # exactly the part a customer asks about.
    assert "Mortgage interest | 4210.00" in text


def test_a_pdf_with_real_metadata_gets_its_own_title():
    # An IRS form arrives named f8655.pdf, which tells the list's reader
    # nothing. The PDF metadata says what it is; prefer it.
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    from voice.documents import derive_title

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "Form 8655 (Rev. September 2024)"})
    buffer = io.BytesIO()
    writer.write(buffer)

    assert derive_title("f8655.pdf", buffer.getvalue()) == "Form 8655 (Rev. September 2024)"


def test_junk_or_missing_metadata_falls_back_to_the_filename():
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    from voice.documents import derive_title

    for junk in (None, "untitled", "pdf", "x", "w2-final.indd"):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        if junk is not None:
            writer.add_metadata({"/Title": junk})
        buffer = io.BytesIO()
        writer.write(buffer)
        assert derive_title("w2-final.pdf", buffer.getvalue()) == "w2-final", junk


def test_non_pdfs_and_broken_pdfs_never_fail_titling():
    from voice.documents import derive_title

    assert derive_title("notes.txt", b"hello") == "notes"
    assert derive_title("broken.pdf", b"%PDF-not really") == "broken"
    assert derive_title("", b"") == "document"
