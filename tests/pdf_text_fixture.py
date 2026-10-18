"""Tiny text PDFs for ext:pdf_text split tests (no network, no binary fixtures).

The same bytes go to the unchanged crawl() (its own pdftotext command) and to the framework
(crawler.enumeration.pdftext with the plan's args), so the papers can be compared field for field.
"""


def _esc(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages):
    """``pages``: list of pages, each a list of ASCII text lines. Returns valid PDF bytes."""
    if pages and isinstance(pages[0], str):
        pages = [pages]
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    num = 4
    for lines in pages:
        ops = "BT /F1 11 Tf 14 TL 60 760 Td " + " ".join(f"({_esc(line)}) '" for line in lines) + " ET"
        stream = ops.encode("latin-1")
        content, page = num, num + 1
        num += 2
        objects[content] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                         b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content)
        kids.append(page)
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for n in sorted(objects):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objects[n] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        out += b"%010d 00000 n \n" % offsets[n]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)
