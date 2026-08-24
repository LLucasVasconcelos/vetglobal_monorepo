"""Minimal, genuinely valid PDFs, built by hand for the tests.

Built rather than committed as fixture bytes: a blob nobody can read is a blob
nobody can adjust, and the difference between the two files this produces --
a page with a text layer and a page without one -- is the whole point of the
`.pdf` tests. Here that difference is one argument.

Built rather than generated with a library, because the only reason to add
`reportlab` to this project would be to write these ten lines, and the PDF
structure below is short enough to read: a catalog, a page tree, one page, and
optionally a content stream that draws a line of text.
"""

HEADER = b"%PDF-1.4\n"


def _object(number: int, body: bytes) -> bytes:
    return b"%d 0 obj\n%s\nendobj\n" % (number, body)


def pdf(text: str | None) -> bytes:
    """A one-page PDF holding `text`, or a page with no content at all.

    `text=None` is the scanned document: a structurally valid PDF whose page
    carries no text layer, which is what a photograph of a consultation note
    looks like to a parser. Reading it would need OCR.
    """
    # Page and content stream first, because the two differ between the scanned
    # file and the one with a text layer; everything else is the same either way.
    if text is None:
        page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"
        contents = b"<< >>"
    else:
        page = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        )
        # Escaped because `(` and `)` delimit a string literal in PDF syntax.
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % escaped.encode("latin-1")
        contents = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)

    # Object numbers are positions in this list, starting at 1, and the
    # references above (`3 0 R`, `4 0 R`, `5 0 R`) depend on that order.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        page,
        contents,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(HEADER)
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += _object(number, body)

    # The cross-reference table: byte offset of every object, which is what
    # makes this a PDF a parser can open rather than a file that looks like one.
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset

    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


CONSULTATION = (
    "Patient Hank, 6 years old, neutered male labrador. "
    "Owner reports intermittent vomiting for five days, worse after meals. "
    "Alert and hydrated, temperature 38.4. Plan: bland diet and recheck in 72h."
)

WITH_TEXT = pdf(CONSULTATION)
SCANNED = pdf(None)
