"""Bounded public-entry HTTP observations, NOT crawler health or full-capacity certification.

No crawler is executed and no DB/blob credentials are loaded. A new output directory
is required for each attempt. Each redirect goes through the same host gate and
public-IP validation. Bodies/headers exist only in private temporary files.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import gzip
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from urllib.parse import urljoin, urlsplit

from .results import atomic_json, site_path, utc_now
from .scope_inventory import public_url

MAX_BODY = 2 * 1024 * 1024


class HostDeferred(Exception):
    pass


def retry_after(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(now, now + int(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(now, date.timestamp())
        except (ValueError, TypeError, OverflowError):
            return now + 60


class HostGate:
    """Single observer process; redirects and body reads share the per-host lock."""
    def __init__(self, delay=1):
        self.delay = delay
        self._guard = threading.Lock()
        self._hosts = {}

    def state(self, host):
        with self._guard:
            return self._hosts.setdefault(host, {'lock': threading.Lock(), 'last_end': 0, 'defer_until': 0})

    @contextmanager
    def enter(self, host):
        state = self.state(host)
        with state['lock']:
            if state['defer_until'] > time.time():
                raise HostDeferred()
            wait = state['last_end'] + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                yield
            finally:
                state['last_end'] = time.monotonic()

    def defer(self, host, value):
        # Called while holding this host's gate.
        state = self.state(host)
        state['defer_until'] = max(state['defer_until'], retry_after(value))

    def cooldowns(self):
        return {h: s['defer_until'] for h, s in self._hosts.items() if s['defer_until'] > time.time()}


def public_address(host, port):
    # DNS resolution itself is bounded, and curl uses the validated address.
    script = 'import socket,json,sys; print(json.dumps([r[4][0] for r in socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)]))'
    result = subprocess.run([sys.executable, '-c', script, host, str(port)],
                            capture_output=True, timeout=5, check=True,
                            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
    addresses = json.loads(result.stdout)
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('non_public_address')
    return next((a for a in addresses if ':' not in a), addresses[0])


def fetch_once(url, timeout=20):
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == 'https' else 80)
    address = public_address(u.hostname, port)
    pinned = '[' + address + ']' if ':' in address else address
    with tempfile.TemporaryDirectory(prefix='source-observation-') as directory:
        root = Path(directory)
        command = ['curl', '-q', '--silent', '--show-error', '--proto', '=http,https',
                   '--noproxy', '*', '--connect-timeout', '10', '--max-time', str(timeout),
                   '--max-filesize', str(MAX_BODY), '--resolve', f'{u.hostname}:{port}:{pinned}',
                   '--user-agent', 'Libertree-Source-Audit/1.0', '--header', 'Accept: text/html,application/json',
                   '--dump-header', str(root/'headers'), '--output', str(root/'body'),
                   '--write-out', '%{http_code}', '--url', url]
        result = subprocess.run(command, capture_output=True, timeout=timeout+5,
                                env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'})
        raw_headers = (root/'headers').read_bytes() if (root/'headers').exists() else b''
        body = (root/'body').read_bytes() if (root/'body').exists() else b''
        headers = {}
        for block in re.split(rb'\r?\n\r?\n', raw_headers):
            if block.startswith(b'HTTP/'):
                headers = {}
                for line in block.splitlines()[1:]:
                    if b':' in line:
                        key, value = line.split(b':', 1)
                        if key.lower() in (b'content-type', b'content-encoding', b'location', b'retry-after'):
                            headers[key.decode().lower()] = value.decode('latin-1').strip()
        try:
            status = int(result.stdout)
        except ValueError:
            status = 0
        return {'status': status, 'curl_exit': result.returncode, 'headers': headers,
                'body': body[:MAX_BODY], 'body_complete': result.returncode == 0 and len(body) <= MAX_BODY}


def inspect_body(body, content_type, selectors=None):
    """Extract only count candidates and selector counts; never certify scope or totals."""
    result = {'count_candidates': [], 'selector_observations': {}, 'document_count': None,
              'total_quality': 'unknown'}
    content_type = content_type.split(';', 1)[0].lower()
    if content_type == 'application/json' or body.lstrip().startswith((b'{', b'[')):
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            result['parse_state'] = 'invalid_json'
            return result
        stack = [('$', data, 0)]
        visited = 0
        while stack and visited < 2000:
            path, value, depth = stack.pop()
            visited += 1
            if depth > 6:
                continue
            if isinstance(value, dict):
                for k, v in value.items():
                    # Only known schema locations/field names; never reflect arbitrary keys.
                    if k in ('total', 'numFound', 'totalCount', 'totalResults', 'totalElements', 'recordsTotal'):
                        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                            result['count_candidates'].append({'field': k, 'value': v,
                                                               'classification': 'unreviewed_json_count'})
                    if isinstance(v, (dict, list)):
                        stack.append(('$', v, depth+1))
            elif isinstance(value, list):
                stack.extend(('$', v, depth+1) for v in value[:20] if isinstance(v, (dict, list)))
        result['parse_state'] = 'json_observed_scope_unreviewed'
    elif content_type in ('text/html', 'application/xhtml+xml', ''):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, 'html.parser')
        for key, selector in (selectors or {}).items():
            try:
                result['selector_observations'][key] = {'matches': len(soup.select(selector)), 'state': 'evaluated'}
            except Exception as exc:
                result['selector_observations'][key] = {'state': 'selector_error', 'exception_type': type(exc).__name__}
        for node in soup(['script', 'style']):
            node.decompose()
        text = soup.get_text(' ', strip=True)
        patterns = {'ko_total': r'(?:총|전체)\s*([0-9][0-9,]*)\s*건',
                    'en_results': r'\b([0-9][0-9,]*)\s+(?:results|records|items)\b'}
        for label, pattern in patterns.items():
            for value in re.findall(pattern, text, re.I)[:8]:
                result['count_candidates'].append({'field': label, 'value': int(value.replace(',', '')),
                                                   'classification': 'unreviewed_page_count'})
        result['next_link_count'] = len(soup.select('a[rel~=next]'))
        result['parse_state'] = 'html_observed_scope_unreviewed'
    else:
        result['parse_state'] = 'non_listing_content_type'
    result['count_candidates'] = result['count_candidates'][:20]
    return result


def decode_observation_body(body, encoding):
    """Decode unsolicited gzip within the same bound; keep wire bytes untouched."""
    encoding = (encoding or '').strip().lower()
    if encoding not in ('', 'identity', 'gzip', 'x-gzip'):
        raise ValueError('unsupported_content_encoding')
    if encoding in ('gzip', 'x-gzip') or body.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                decoded = stream.read(MAX_BODY + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError('invalid_compressed_body') from exc
        if len(decoded) > MAX_BODY:
            raise ValueError('decoded_body_limit')
        return decoded, 'gzip'
    return body, 'identity'


def observe(site, gate, *, timeout=20, fetch=fetch_once):
    started = time.monotonic()
    entry = site.get('selected_entry')
    result = {'site_id': site['site_id'], 'scope_profile_hash': site.get('scope_profile_hash'),
              'scope': 'entry_http_observation_not_crawler_or_full_capacity_test',
              'entry_role': entry.get('role') if entry else None, 'hops': [],
              'current_total': None, 'current_total_quality': 'unknown',
              'list_enumeration': 'not_verified', 'detail_parsing': 'not_verified',
              'persistence': 'not_tested', 'config_issues': site.get('config_issues', [])}
    url = public_url(entry.get('url')) if entry else None
    if not url:
        result['state'] = 'no_reviewed_literal_entry'
    else:
        visited = set()
        try:
            for _ in range(4):
                if url in visited:
                    result['state'] = 'redirect_loop'
                    break
                visited.add(url)
                host = urlsplit(url).hostname.lower()
                with gate.enter(host):
                    response = fetch(url, timeout=timeout)
                    headers = response['headers']
                    if response['status'] == 429 or (response['status'] == 503 and headers.get('retry-after')):
                        gate.defer(host, headers.get('retry-after'))
                result['hops'].append({'url': url, 'http_status': response['status'],
                                       'curl_exit': response['curl_exit'], 'body_bytes': len(response['body']),
                                       'body_complete': response['body_complete']})
                if response['curl_exit']:
                    result['state'] = {28: 'timeout', 60: 'tls_verification_failed', 63: 'body_limit'}.get(response['curl_exit'], 'transport_error')
                    break
                status = response['status']
                if 300 <= status < 400:
                    next_url = public_url(urljoin(url, headers.get('location', '')))
                    if not next_url:
                        result['state'] = 'redirect_requires_review'
                        break
                    url = next_url
                    continue
                if not 200 <= status < 300:
                    result['state'] = {401: 'authentication_required', 403: 'access_denied', 404: 'entry_not_found',
                                       429: 'rate_limited'}.get(status, 'http_error')
                    break
                result['body_sha256'] = hashlib.sha256(response['body']).hexdigest()
                if not response['body_complete']:
                    result['state'] = 'body_limit'
                    break
                try:
                    body, encoding = decode_observation_body(response['body'], headers.get('content-encoding'))
                except ValueError as exc:
                    result['state'] = str(exc)
                    break
                result.update(decoded_body_bytes=len(body), decoded_body_sha256=hashlib.sha256(body).hexdigest(),
                              body_decoding=encoding)
                result['body_observation'] = inspect_body(body, headers.get('content-type', ''), site.get('list_selectors'))
                result['state'] = 'entry_responded_scope_unverified'
                break
            else:
                result['state'] = 'redirect_limit'
        except HostDeferred:
            result['state'] = 'host_cooldown_deferred'
        except Exception as exc:
            result.update(state='observation_error', exception_type=type(exc).__name__)
    result.update(observed_at=utc_now(), elapsed_s=round(time.monotonic()-started, 3))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=20)
    parser.add_argument('--site', action='append')
    args = parser.parse_args()
    if not (1 <= args.workers <= 4 and 1 <= args.timeout <= 60):
        parser.error('workers 1..4, timeout 1..60')
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    raw = args.inventory.read_bytes()
    sites = json.loads(raw)['sites']
    if args.site:
        requested = set(args.site)
        if requested - {s['site_id'] for s in sites}:
            parser.error('unknown site selection')
        sites = [s for s in sites if s['site_id'] in requested]
    gate = HostGate()
    counts = Counter()
    state = {'started_at': utc_now(), 'finished_at': None, 'total': len(sites), 'completed': 0,
             'inventory_sha256': hashlib.sha256(raw).hexdigest(), 'scope': 'entry_http_only',
             'workers': args.workers, 'per_host_workers': 1, 'request_timeout_s': args.timeout,
             'full_health_diagnosis_complete': False, 'source_total_measurement_complete': False}
    atomic_json(args.out/'scan.json', state)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(observe, s, gate, timeout=args.timeout) for s in sites]
        for future in as_completed(futures):
            result = future.result()
            atomic_json(site_path(args.out, result['site_id']), result)
            counts[result['state']] += 1
            state.update(completed=state['completed']+1, counts=dict(counts), updated_at=utc_now())
            atomic_json(args.out/'scan.json', state)
    state.update(finished_at=utc_now(), host_cooldowns=gate.cooldowns())
    atomic_json(args.out/'scan.json', state)
    print(json.dumps(state))


if __name__ == '__main__':
    main()
