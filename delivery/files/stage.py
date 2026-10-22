"""Isolated file parser/downloader. Receives a temporary work directory only."""
from __future__ import annotations

import hashlib
import json
import os
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


PDF_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB (user decision 10-21); larger files are recorded as file_too_large
# Same politeness as collection: one request per host per gap, shared by every stage process.
PDF_REQUEST_INTERVAL_S = 2.0
# Kept in memory for the Fetcher's challenge/login checks; the rest of a PDF goes straight to disk.
STREAM_HEAD_BYTES = 256 * 1024
ROBOTS_CACHE_S = 24 * 3600
FETCH_ERRORS = (('RobotsDisallowed', 'robots_disallowed'), ('RobotsUnavailable', 'robots_unavailable'),
                ('RateLimited', 'rate_limited'), ('PrivateDestination', 'download_failed'),
                ('AccessBlocked', 'access_blocked'), ('BudgetExhausted', 'download_failed'))


def _streaming_transport(sink):
    """RequestsTransport that writes a GET body to ``sink`` instead of memory (robots.txt stays in memory).
    The returned Response carries the first STREAM_HEAD_BYTES as body and the full size as ``size``."""
    from crawler.enumeration.http import BodyTooLarge, RequestsTransport, Response, TransportError

    class StreamingTransport(RequestsTransport):
        def request(self, method, url, *, headers=None, data=None):
            if method != 'GET' or urlsplit(url).path == '/robots.txt':
                return super().request(method, url, headers=headers, data=data)
            try:
                with self.session.request(method, url, headers=headers or {}, stream=True,
                                          timeout=self.timeout, allow_redirects=False) as r:
                    declared = str(r.headers.get('Content-Length') or '').strip()
                    if declared.isdigit() and int(declared) > self.max_bytes:
                        raise BodyTooLarge('body_too_large')
                    head, size = bytearray(), 0
                    with open(sink, 'wb') as out:
                        for chunk in r.iter_content(64 * 1024):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise BodyTooLarge('body_too_large')
                            if len(head) < STREAM_HEAD_BYTES:
                                head += chunk[:STREAM_HEAD_BYTES - len(head)]
                            out.write(chunk)
                    response = Response(r.status_code, dict(r.headers), bytes(head), r.url)
                    response.size = size
                    return response
            except self._requests.RequestException as exc:
                raise TransportError(type(exc).__name__) from exc

    return StreamingTransport(max_bytes=PDF_MAX_BYTES)


# Some CMSs (e.g. Optimizely, sgu.se) answer a JSON description instead of the file when the request
# accepts JSON first; asking for PDF first returns the file itself (S 확인 10-23).
PDF_ACCEPT = 'application/pdf,*/*;q=0.8'


def _cached_robots_fetcher(gates, transport, pdf_host=None):
    """Fetcher whose robots.txt verdict per origin is shared on disk by every stage process, so one
    PDF costs one request. A refusal (403/challenge/login) or an unreachable robots.txt is cached
    too: the site is not asked again until the entry expires."""
    import time
    from crawler.enumeration import http
    cache = gates / 'robots'

    class CachedRobotsFetcher(http.Fetcher):
        def _path(self, origin):
            return cache / (hashlib.sha256(origin.encode()).hexdigest()[:24] + '.json')

        def _write(self, origin, entry):
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(origin)
            tmp = path.with_suffix('.tmp%d' % os.getpid())
            tmp.write_text(json.dumps(dict(entry, origin=origin, at=time.time())), encoding='utf-8')
            os.replace(tmp, path)

        def _read(self, origin):
            try:
                entry = json.loads(self._path(origin).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
            ttl = http.ROBOTS_RETRY_S if entry.get('kind') == 'unavailable' else ROBOTS_CACHE_S
            return entry if entry.get('origin') == origin and time.time() - entry.get('at', 0) < ttl else None

        def _load_robots(self, origin):
            seen = {}
            fetch = self._with_retries
            def capture(url, **kwargs):
                seen['response'] = fetch(url, **kwargs)
                return seen['response']
            self._with_retries = capture
            try:
                result = super()._load_robots(origin)
            finally:
                del self._with_retries
            self._robots_text = seen['response'].text() if not isinstance(result, str) else None
            return result

        def _robots_allows(self, url):
            parts = urlsplit(url)
            origin = f'{parts.scheme}://{parts.netloc}'
            fresh = self.respect_robots and origin not in self._robots
            if fresh:
                entry = self._read(origin)
                if entry:
                    fresh = False
                    if entry['kind'] == 'blocked':
                        raise http.AccessBlocked(entry['reason'], url, entry.get('status'))
                    if entry['kind'] == 'unavailable':
                        raise http.RobotsUnavailable(entry['reason'], url, entry.get('status'))
                    self._robots[origin] = http.parse_robots(entry['text']) if entry['kind'] == 'rules' else entry['kind']
                    if entry.get('delay'):
                        self._host_delay[http.host_of(origin)] = entry['delay']
            try:
                allowed = super()._robots_allows(url)
            except http.RobotsUnavailable as exc:
                if fresh:
                    self._write(origin, {'kind': 'unavailable', 'reason': exc.reason, 'status': exc.status})
                raise
            except http.AccessBlocked as exc:
                rule = self._robots.get(origin)
                if fresh and not isinstance(rule, str):  # too_slow/unparseable verdicts are cached as rules below
                    self._write(origin, {'kind': 'blocked', 'reason': exc.reason, 'status': exc.status})
                elif fresh:
                    self._write(origin, {'kind': rule})
                raise
            if fresh:
                rule = self._robots[origin]
                self._write(origin, {'kind': rule if isinstance(rule, str) else 'rules', 'text': getattr(self, '_robots_text', None),
                                     'delay': self._host_delay.get(http.host_of(origin))})
            return allowed

    headers = {'Accept': PDF_ACCEPT} if pdf_host else None
    return CachedRobotsFetcher('file-worker', transport, gates, interval=PDF_REQUEST_INTERVAL_S,
                               retries=2, rate_limit_stop=1, extra_headers=headers,
                               header_hosts=[pdf_host] if pdf_host else None)


def sniff_kind(head: bytes) -> str:
    """PDF 가 아닌 응답이 무엇이었는지(원인표용): html · ole(HWP 5·옛 Office) · zip(HWPX·docx·xlsx) · image · empty · other."""
    lead = head[:1024].lstrip(b'\xef\xbb\xbf \t\r\n\x00')
    if not lead:
        return 'empty'
    if lead[:1] == b'<' or lead[:9].lower() == b'<!doctype':
        return 'html'
    if head.startswith(b'\xd0\xcf\x11\xe0'):
        return 'ole'
    if head.startswith(b'PK\x03\x04'):
        return 'zip'
    if head.startswith((b'\x89PNG', b'\xff\xd8\xff', b'GIF8')):
        return 'image'
    return 'other'


def download_pdf(root, url, transport=None):
    """Fetch one PDF through the collection Fetcher (crawler.enumeration.http): robots.txt is
    honoured (verdict shared on disk, see _cached_robots_fetcher), the honest collector
    User-Agent is sent, every hop must resolve to a public address, and a host gate under the
    shared work directory keeps PDF_REQUEST_INTERVAL_S between requests to one host across
    stage processes. The body streams to disk (up to PDF_MAX_BYTES). A block, robots refusal or
    rate limit is returned as its own error code and never retried around."""
    from crawler.enumeration import http
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        return {'success': False, 'error': 'download_failed'}
    sink = root / 'download.part'
    # root is <blob>/.file-work/task-XXXX; the gates sit beside the task directories and outlive them.
    fetcher = _cached_robots_fetcher(root.parent / '.gates', transport or _streaming_transport(sink), parsed.hostname.lower())
    try:
        response = fetcher.get(url)
    except http.PermanentFailure as exc:
        sink.unlink(missing_ok=True)
        # 404/410 and other permanent answers: retrying would only repeat the same request.
        return {'success': False, 'error': 'file_too_large' if exc.reason == 'body_too_large' else 'download_failed',
                'reason': exc.reason, 'status': exc.status, 'permanent': True}
    except (http.FetchError, http.EnumerationStop) as exc:
        sink.unlink(missing_ok=True)
        code = next((c for name, c in FETCH_ERRORS if isinstance(exc, getattr(http, name))), 'download_failed')
        return {'success': False, 'error': code, 'reason': getattr(exc, 'reason', None), 'status': getattr(exc, 'status', None)}
    if not response.body[:1024].lstrip(b'\xef\xbb\xbf \t\r\n\x00').startswith((b'%PDF-1.', b'%PDF-2.')):
        sink.unlink(missing_ok=True)
        # 무엇을 받았는지만 남긴다(본문은 남기지 않음): 예 invalid_pdf:html:text/html
        ctype = (response.content_type or '').split(';', 1)[0].strip().lower()[:40]
        return {'success': False, 'error': 'invalid_pdf', 'reason': sniff_kind(response.body), 'status': ctype or None}
    target = root / 'download.pdf'
    if getattr(response, 'size', None) is not None and sink.is_file():
        os.replace(sink, target)
    else:  # a transport that returned the whole body in memory (tests)
        target.write_bytes(response.body)
    with target.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'success': True, 'size': target.stat().st_size, 'sha256': digest}


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
