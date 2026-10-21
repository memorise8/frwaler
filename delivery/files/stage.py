"""Isolated file parser/downloader. Receives a temporary work directory only."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .text_quality import MAX_TEXT_BYTES, inspect_text


def _fallback_text(root):
    from crawler.converter import PDFTextAccessError
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    # Check the original reader permissions before invoking another engine.
    # No supplied/owner password or permission override is passed to Poppler.
    try:
        with pdfplumber.open(root / 'source.pdf') as pdf:
            if not pdf.doc.is_extractable:
                raise PDFTextAccessError('pdf_copy_restricted')
    except Exception as exc:
        cause = exc.__cause__ or exc.__context__
        if isinstance(exc, PDFPasswordIncorrect) or isinstance(cause, PDFPasswordIncorrect):
            raise PDFTextAccessError('pdf_password_required') from exc
        raise
    if not shutil.which('pdftotext'):
        return None, 'text_extractor_unavailable'
    candidate = root / 'fallback.txt'
    candidate.unlink(missing_ok=True)
    result = subprocess.run([sys.executable, '-m', 'delivery.files.poppler',
                             str(root / 'source.pdf'), str(candidate)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=60)
    if result.returncode or not candidate.is_file():
        return None, 'parse_failed'
    if candidate.stat().st_size > MAX_TEXT_BYTES:
        return None, 'text_size_limit'
    return candidate.read_text(encoding='utf-8'), None


PDF_MAX_BYTES = 200 * 1024 * 1024
# Same politeness as collection: one request per host per gap, shared by every stage process.
PDF_REQUEST_INTERVAL_S = 2.0
FETCH_ERRORS = (('RobotsDisallowed', 'robots_disallowed'), ('RobotsUnavailable', 'robots_unavailable'),
                ('RateLimited', 'rate_limited'), ('PrivateDestination', 'download_failed'),
                ('AccessBlocked', 'access_blocked'), ('BudgetExhausted', 'download_failed'))


def _transport():
    from crawler.enumeration.http import RequestsTransport
    return RequestsTransport(max_bytes=PDF_MAX_BYTES)


def download_pdf(root, url, transport=None):
    """Fetch one PDF through the collection Fetcher (crawler.enumeration.http): robots.txt is
    honoured, the honest collector User-Agent is sent, every hop must resolve to a public
    address, and a host gate under the shared work directory keeps PDF_REQUEST_INTERVAL_S
    between requests to one host across stage processes. A block, robots refusal or rate limit
    is returned as its own error code and never retried around."""
    from crawler.enumeration import http
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return {'success': False, 'error': 'download_failed'}
    # root is <blob>/.file-work/task-XXXX; the gates sit beside the task directories and outlive them.
    fetcher = http.Fetcher('file-worker', transport or _transport(), root.parent / '.gates',
                           interval=PDF_REQUEST_INTERVAL_S, retries=2, rate_limit_stop=1)
    try:
        response = fetcher.get(url)
    except http.PermanentFailure as exc:
        return {'success': False, 'error': 'file_too_large' if exc.reason == 'body_too_large' else 'download_failed',
                'reason': exc.reason, 'status': exc.status}
    except (http.FetchError, http.EnumerationStop) as exc:
        code = next((c for name, c in FETCH_ERRORS if isinstance(exc, getattr(http, name))), 'download_failed')
        return {'success': False, 'error': code, 'reason': getattr(exc, 'reason', None), 'status': getattr(exc, 'status', None)}
    body = response.body
    if not body[:1024].lstrip(b'\xef\xbb\xbf \t\r\n\x00').startswith((b'%PDF-1.', b'%PDF-2.')):
        return {'success': False, 'error': 'invalid_pdf'}
    (root / 'download.pdf').write_bytes(body)
    return {'success': True, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}


def execute(stage, directory, url=None):
    root = Path(directory)
    if stage == 'pdf_body_coverage':
        from crawler.converter import PDFTextAccessError
        from .pdf_body_coverage import inspect_pdf
        try:
            coverage = inspect_pdf(root / 'source.pdf')
        except PDFTextAccessError as exc:
            return {'success': False, 'error': exc.code}
        except Exception:
            return {'success': False, 'error': 'pdf_body_coverage_unavailable'}
        if coverage['state'] == 'review_required':
            return {'success': False, 'error': 'pdf_body_coverage_review_required', 'coverage': coverage}
        return {'success': True, 'coverage': coverage}
    if stage == 'pdf':
        return download_pdf(root, url)
    if stage in ('text', 'text_fallback'):
        from crawler.converter import PDFTextAccessError, extract_text_pdf
        (root / 'output.txt').unlink(missing_ok=True)
        try:
            if stage == 'text':
                text = extract_text_pdf(root / 'source.pdf')
            else:
                text, error = _fallback_text(root)
                if error:
                    return {'success': False, 'error': error}
        except PDFTextAccessError as exc:
            return {'success': False, 'error': exc.code}
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'stage_timeout'}
        except Exception:
            return {'success': False, 'error': 'parse_failed'}
        if not text or not text.strip():
            return {'success': False, 'error': 'no_text_layer'}
        quality = inspect_text(text, engine='pdftotext' if stage == 'text_fallback' else 'pdfplumber_or_pypdf')
        if quality['state'] == 'review_required':
            return {'success': False, 'error': 'text_quality_review_required', 'quality': quality}
        if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            return {'success': False, 'error': 'text_size_limit'}
        (root / 'output.txt').write_text(text, encoding='utf-8')
        return {'success': True, 'chars': len(text), 'quality': quality}
    return {'success': False, 'error': 'invalid_stage'}


def main():
    # Keep URL query strings and document content out of stdout and process argv.
    payload = json.loads(sys.stdin.read())
    root = Path(payload['directory'])
    try:
        result = execute(payload['stage'], root, payload.get('url'))
    except Exception:
        result = {'success': False, 'error': 'stage_failed'}
    (root / 'result.json').write_text(json.dumps(result), encoding='utf-8')


if __name__ == '__main__':
    main()
