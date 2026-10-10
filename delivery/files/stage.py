"""Isolated file parser/downloader. Receives a temporary work directory only."""
from __future__ import annotations

import json
import ipaddress
import socket
import shutil
import subprocess
from pathlib import Path
import sqlite3
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


def execute(stage, directory, url=None):
    root = Path(directory)
    if stage == 'pdf':
        import requests
        from crawler.pdf_downloader import download_pdf_for
        class HttpSession(requests.Session):
            def send(self, request, **kwargs):
                parsed = urlsplit(request.url)
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                    raise requests.RequestException('invalid PDF URL')
                # Apply on every redirect as well as the first URL. These are
                # public-source PDFs; metadata must not become an internal fetch.
                try:
                    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
                    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                        raise requests.RequestException('non-public PDF destination')
                except (OSError, ValueError) as exc:
                    raise requests.RequestException('invalid PDF destination') from exc
                return super().send(request, **kwargs)
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE documents(seq_id INTEGER,pdf_downloaded INTEGER,pdf_size_bytes INTEGER,pdf_sha256 TEXT)')
        try:
            with HttpSession() as session:
                result = download_pdf_for(conn, 1, url, blob_root=root, session=session)
            if result['success']:
                Path(result['path']).replace(root / 'download.pdf')
                return {'success': True, 'size': result['size_bytes'], 'sha256': result['sha256']}
            error = result.get('error') or ''
            code = 'invalid_pdf' if 'PDF header' in error else 'download_failed'
            if 'max_size' in error:
                code = 'file_too_large'
            return {'success': False, 'error': code}
        finally:
            conn.close()
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
