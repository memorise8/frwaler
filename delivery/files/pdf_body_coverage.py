"""Detect a dominant raster body behind a small, otherwise clean text layer.

This is a review signal, not a semantic or full-text completeness certificate.
Run in the bounded file-stage child process, with OCR disabled.
"""


def inspect_pdf(path):
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from crawler.converter import PDFTextAccessError

    sparse_image_pages = []
    pages = []
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.doc.is_extractable:
                raise PDFTextAccessError('pdf_copy_restricted')
            for number, page in enumerate(pdf.pages, 1):
                characters = sum(len(c.get('text', '').strip()) for c in page.chars)
                images = page.images
                page_area = page.width * page.height
                # Use the largest clipped image, not a sum of overlapping images.
                largest = max((max(0, min(page.bbox[2], v['x1']) - max(page.bbox[0], v['x0']))
                               * max(0, min(page.height, v['y1']) - max(0, v['y0']))
                               for v in images), default=0)
                fraction = min(1, largest / page_area) if page_area else 0
                sparse = characters < 80 and (len(images) >= 10 or fraction >= 0.25)
                if sparse:
                    sparse_image_pages.append(number)
                pages.append(dict(page=number, text_characters=characters, images=len(images),
                                  largest_image_page_fraction=round(fraction, 6),
                                  sparse_image_page=sparse))
                page.close()
    except Exception as exc:
        cause = exc.__cause__ or exc.__context__
        if isinstance(exc, PDFPasswordIncorrect) or isinstance(cause, PDFPasswordIncorrect):
            raise PDFTextAccessError('pdf_password_required') from exc
        raise
    # A cover, blank page or isolated figure must not invalidate a text report.
    requires_review = len(sparse_image_pages) >= 3 and len(sparse_image_pages) * 2 > len(pages)
    return dict(version=1, state='review_required' if requires_review else 'no_dominant_raster_signal',
                pages=len(pages), sparse_image_pages=sparse_image_pages,
                page_diagnostics=pages, OCR_enabled=False, fidelity_verified=False)
