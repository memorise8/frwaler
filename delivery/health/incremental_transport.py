"""Audited transport for incremental rollout: shared gate, single attempts.

Install on a newly created crawler only. The callback records every HTTP
response; redirects are not followed and rate limits defer the shared host.
"""
import hashlib
from pathlib import Path

from .request_gate import RequestGate
from .source_observer import retry_after


def install(crawler, gate_dir, output, requests, *, persist=lambda: None):
    gate = RequestGate(gate_dir, delay=1.5)
    output = Path(output)
    native = crawler._session.request

    def request(method, url, **kwargs):
        kwargs['allow_redirects'] = False
        with gate.enter(url):
            index = len(requests)
            try:
                response = native(method, url, **kwargs)
            except Exception as exc:
                requests.append({'method': method, 'url': url, 'status': None,
                                 'error_type': type(exc).__name__, 'body': None})
                persist()
                raise
            body = response.content
            path = output / f'{index:04d}.body'
            path.write_bytes(body)
            requests.append({'method': method, 'url': response.url, 'status': response.status_code,
                             'body': path.name, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)})
            if response.status_code == 429 or (response.status_code == 503 and response.headers.get('Retry-After')):
                gate.defer(url, retry_after(response.headers.get('Retry-After')))
            persist()
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError('source redirect requires scope review')
            return response
    crawler._session.request = request
