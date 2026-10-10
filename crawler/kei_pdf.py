"""Recover missing KEI HTML bodies from their validated source PDF attachment."""
import hashlib
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.document_identity import kei_document_id
from crawler.kei_source import SCOPES
from delivery.files.text_quality import inspect_text


MAX_BYTES = 200 * 1024 * 1024


def validate_source_pdf(item, detail):
    key = ':'.join(kei_document_id(item['url']))
    candidate = detail.get('primary_PDF_candidate')
    if (key != item['source_key'] or key != detail.get('source_key')
            or detail.get('source_url') != item['url'] or not candidate
            or candidate not in detail['attachments'] or candidate['extension'] != '.pdf'):
        raise ValueError('KEI source PDF identity not established')
    url = candidate['url']
    p = urlsplit(url)
    if p.scheme != 'https' or p.fragment:
        raise ValueError('KEI invalid source PDF destination')
    if item['scope'].startswith('board-'):
        q = parse_qs(p.query, keep_blank_values=True)
        if (p.netloc != 'www.kei.re.kr' or p.path != '/boardDownload.es'
                or set(q) != {'bid', 'list_no', 'seq'}
                or q.get('bid') != [SCOPES[item['scope']][2]]
                or q.get('list_no') != [item['source_id']]
                or len(q.get('seq', [])) != 1 or not re.fullmatch('[1-9][0-9]*', q['seq'][0])):
            raise ValueError('KEI source PDF board identity mismatch')
    elif (p.netloc != 'library.kei.re.kr' or p.query
          or not re.fullmatch('/pyxis-api/1/digital-files/[0-9a-f]{8}-[0-9a-f]{4}-'
                              '[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', p.path)):
        raise ValueError('KEI invalid catalogue PDF destination')
    return url


def source_pdf_excerpt(crawler, item, detail):
    from delivery.files.worker import run_stage

    url = validate_source_pdf(item, detail)
    crawler._check_cancelled()
    started, size, digest = time.monotonic(), 0, hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix='kei-source-pdf-') as directory:
        root = Path(directory)
        with crawler._session.get(url, stream=True, timeout=(30, 45), allow_redirects=False,
                                  headers={'Referer': item['url']}) as response:
            if response.status_code in (401, 403, 429, 503):
                raise PermissionError(f'HTTP {response.status_code}')
            if response.status_code != 200:
                raise ConnectionError(f'KEI source PDF HTTP {response.status_code}')
            declared = int(response.headers.get('Content-Length', '0'))
            if not 0 <= declared <= MAX_BYTES:
                raise ValueError('KEI source PDF size exceeds budget')
            with (root / 'source.pdf').open('xb') as handle:
                for chunk in response.iter_content(65536):
                    crawler._check_cancelled()
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() - started > 240:
                        raise ValueError('KEI source PDF byte or time budget exceeded')
                    handle.write(chunk)
                    digest.update(chunk)
        if declared and declared != size:
            raise ValueError('KEI source PDF length mismatch')
        with (root / 'source.pdf').open('rb') as handle:
            if handle.read(5) != b'%PDF-':
                raise ValueError('KEI source response is not PDF')
        crawler._check_cancelled()
        stage = run_stage('text', root, timeout=120)
        crawler._check_cancelled()
        if not stage.get('success'):
            raise ValueError('KEI source PDF text unavailable: ' + str(stage.get('error', 'unknown')))
        if (stage.get('quality') or {}).get('state') == 'review_required':
            raise ValueError('KEI source PDF text requires quality review')
        coverage = run_stage('pdf_body_coverage', root, timeout=120)
        crawler._check_cancelled()
        if not coverage.get('success'):
            raise ValueError('KEI source PDF body coverage unavailable: '
                             + str(coverage.get('error', 'unknown')))
        textfile = root / 'output.txt'
        with textfile.open(encoding='utf-8') as handle:
            excerpt = ' '.join(handle.read(12000).split())[:3000]
        with textfile.open('rb') as handle:
            text_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
    quality = inspect_text(excerpt, engine='source_PDF_excerpt')
    if len(excerpt) < crawler._MIN_ABSTRACT or quality['state'] == 'review_required':
        raise ValueError('KEI source PDF excerpt insufficient or requires quality review')
    return excerpt, dict(abstract_source='publisher_pdf_text_excerpt',
                         abstract_pdf_url=url, abstract_pdf_sha256=digest.hexdigest(),
                         abstract_pdf_bytes=size, abstract_pdf_text_sha256=text_hash,
                         abstract_pdf_text_characters=stage.get('chars'),
                         abstract_pdf_text_quality=stage.get('quality'),
                         abstract_pdf_body_coverage=coverage['coverage'],
                         abstract_excerpt_quality=quality, full_text_fidelity_verified=False)
