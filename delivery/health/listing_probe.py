"""Check configured generic list selectors and one page transition without saving documents."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .results import atomic_json, site_path, utc_now
from .scope_inventory import public_url, SENSITIVE
from .source_observer import HostGate, HostDeferred, fetch_once


def list_url(config, page):
    listing = config['list_page']
    params = dict(listing.get('params', {}))
    pagination = listing.get('pagination', {})
    if pagination.get('type') == 'query_param':
        params[pagination.get('param', 'page')] = page
    if any(SENSITIVE.search(str(k)) for k in params):
        raise ValueError('parameter_review_required')
    original = public_url(listing['url'])
    if not original:
        raise ValueError('url_review_required')
    u = urlsplit(original)
    return urlunsplit(u._replace(query=urlencode(parse_qsl(u.query) + list(params.items()), doseq=True)))


def list_items(config, body):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, 'html.parser')
    selectors = config['list_page'].get('selectors', {})
    if config.get('crawl_type') == 'single-page':
        nodes = soup.select(selectors.get('link', 'a[href]'))
        pairs = [(node, node.get(selectors.get('link_attr', 'href'), '')) for node in nodes]
    else:
        nodes = soup.select(selectors.get('item_container', 'tr'))
        pairs = []
        for row in nodes:
            link = row.select_one(selectors.get('item_link', 'a[href]'))
            if link is not None:
                pairs.append((link, link.get(selectors.get('item_link_attr', 'href'), '')))
    hashes = []
    invalid = 0
    for node, href in pairs:
        # Match GenericCrawler's base URL resolution, without evaluating JS.
        resolved = public_url(urljoin(config['base_url'], href)) if href else None
        if not resolved:
            invalid += 1
        else:
            hashes.append(hashlib.sha256(resolved.encode()).hexdigest())
    return {'container_matches': len(nodes), 'link_matches': len(pairs),
            'public_link_count': len(hashes), 'unique_public_link_count': len(set(hashes)),
            'invalid_or_review_required_links': invalid, 'link_hashes': sorted(set(hashes)),
            'body_sha256': hashlib.sha256(body).hexdigest(), 'document_validity': 'not_verified'}


def probe(site, config, gate, *, fetch=fetch_once):
    result = {'site_id': site['site_id'], 'observed_at': utc_now(),
              'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
              'scope': 'generic_listing_and_one_transition_only', 'pages': [],
              'source_total': None, 'detail_parsing': 'not_tested', 'persistence': 'not_tested',
              'full_enumeration': 'not_verified'}
    if config.get('crawl_type', 'html') not in ('html', 'single-page'):
        return {**result, 'state': 'requires_api_specific_adapter'}
    if not isinstance(config.get('list_page'), dict):
        return {**result, 'state': 'missing_list_page_configuration'}
    pagination = config['list_page'].get('pagination', {})
    first_page = pagination.get('start', 1)
    pages = [first_page]
    if config.get('crawl_type', 'html') == 'html' and pagination.get('type') == 'query_param':
        step = pagination.get('step', 1)
        if not isinstance(first_page, int) or not isinstance(step, int) or step <= 0:
            return {**result, 'state': 'pagination_configuration_requires_review'}
        pages.append(first_page+step)
    try:
        for page in pages:
            url = list_url(config, page)
            entry = {'page': page, 'hops': []}
            result['pages'].append(entry)
            visited = set()
            for _ in range(4):
                if url in visited:
                    return {**result, 'state': 'redirect_loop'}
                visited.add(url)
                host = urlsplit(url).hostname
                with gate.enter(host):
                    response = fetch(url, timeout=20)
                    headers = response['headers']
                    if response['status'] == 429 or (response['status'] == 503 and headers.get('retry-after')):
                        gate.defer(host, headers.get('retry-after'))
                entry['hops'].append({'url': url, 'http_status': response['status'], 'curl_exit': response['curl_exit']})
                if response['curl_exit']:
                    return {**result, 'state': 'transport_or_body_limit'}
                if 300 <= response['status'] < 400:
                    url = public_url(urljoin(url, headers.get('location', '')))
                    if not url:
                        return {**result, 'state': 'redirect_requires_review'}
                    continue
                if response['status'] != 200 or not response['body_complete']:
                    return {**result, 'state': 'http_error_or_incomplete_body'}
                entry.update(list_items(config, response['body']))
                break
            else:
                return {**result, 'state': 'redirect_limit'}
            if not entry['unique_public_link_count']:
                return {**result, 'state': 'no_list_links_first_page' if page == first_page else 'next_page_empty_requires_end_review'}
        if len(result['pages']) == 1:
            result['state'] = 'list_links_observed_no_query_pagination_contract'
        else:
            a, b = (set(r['link_hashes']) for r in result['pages'])
            result['transition'] = {'shared': len(a & b), 'next_only': len(b-a), 'first_only': len(a-b)}
            result['state'] = ('same_links_on_next_page_requires_review' if a == b
                               else 'distinct_next_page_links_observed_not_full_coverage')
    except HostDeferred:
        result['state'] = 'host_cooldown_deferred'
    except Exception as exc:
        result.update(state='configuration_or_selector_or_transport_error', exception_type=type(exc).__name__)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    sites = [s for s in json.loads(args.inventory.read_text())['sites'] if s.get('config_source')]
    gate = HostGate()
    def run(site):
        path = (root/site['config_source']).resolve()
        if root not in path.parents:
            raise ValueError('invalid_config_path')
        return probe(site, json.loads(path.read_text()), gate)
    state = {'started_at': utc_now(), 'finished_at': None, 'total': len(sites), 'completed': 0,
             'scope': 'configured_generic_listings_only', 'full_health_complete': False}
    atomic_json(args.out/'scan.json', state)
    counts = Counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(run, s) for s in sites]):
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
