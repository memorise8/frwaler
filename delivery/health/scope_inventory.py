"""Read-only source entry/configuration inventory. Static signals are not coverage proof."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .results import atomic_json, utc_now

SENSITIVE = re.compile(r"key|token|secret|password|signature|auth|credential|session", re.I)


def public_url(value):
    """Only literal HTTP endpoints without credentials; never persist credential values."""
    if not isinstance(value, str) or any(c in value for c in ('{', '}', '\n', '\r')):
        return None
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            return None
        if u.port not in (None, 80, 443):
            return None
        # Drupal's public search term is named "keys"; it is not an API key.
        if any(SENSITIVE.search(k) and k.lower() not in ('keys','keyword','keywords')
               for k, _ in parse_qsl(u.query, keep_blank_values=True)):
            return None
        return urlunsplit((u.scheme, u.netloc, u.path, u.query, ''))
    except ValueError:
        return None


def literal(node, names):
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left, names), literal(node.right, names)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def static_profile(source, *, config=None, base_url=None):
    tree = ast.parse(source)
    names = {}
    candidates = []
    def add(role, value):
        url = public_url(value)
        if url and not any(c['url'] == url for c in candidates):
            candidates.append({'role': role, 'url': url, 'scope_review': 'unreviewed'})
    # Only module/class constants, never executing expressions or crawler methods.
    statements = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            statements.extend(node.body)
    for node in statements:
        if isinstance(node, ast.Assign):
            value = literal(node.value, names)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = value
    config_issues = []
    selectors = {}
    if config is not None:
        import soupsieve
        for section in ('list_page', 'detail_page'):
            for key, selector in ((config.get(section) or {}).get('selectors') or {}).items():
                if isinstance(selector, str) and key not in ('item_link_attr',):
                    try:
                        soupsieve.compile(selector)
                    except Exception:
                        config_issues.append('invalid_css_selector:' + section + '.' + key)
        kind = config.get('crawl_type', 'html')
        listing = config.get('list_page')
        if kind != 'api' and not isinstance(listing, dict):
            config_issues.append('missing_list_page_required_by_generic_crawler')
        if isinstance(listing, dict):
            url = listing.get('url')
            params = listing.get('params', {})
            if public_url(url) and isinstance(params, dict):
                if any(SENSITIVE.search(str(k)) for k in params):
                    config_issues.append('potentially_sensitive_parameter_requires_review')
                    url = None
                else:
                    u = urlsplit(url)
                    url = urlunsplit(u._replace(query=urlencode(parse_qsl(u.query) + list(params.items()), doseq=True)))
            add('config.list_page.url', url)
            selectors = {k: v for k, v in listing.get('selectors', {}).items()
                         if k in ('item_container', 'item_link', 'title', 'date') and isinstance(v, str)}
            if not public_url(url):
                config_issues.append('listing_url_not_available_as_public_literal')
        add('config.base_url_fallback', config.get('base_url'))
    for key in ('_LIST_URL', 'LIST_URL', '_START_URL', 'START_URL', '_SEARCH_URL',
                'SEARCH_URL', '_API_URL', 'API_URL', '_API_BASE', 'API_BASE',
                '_BASE_URL', 'BASE_URL', 'base_url'):
        add('constant.' + key, names.get(key))
    add('class.base_url_fallback', base_url)
    cap_names = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                        and re.search(r'MAX.*(?:PAGE|ITEM|RESULT)|PAGE.*LIMIT|MAX_WALL', n.id)})
    return {'entry_candidates': candidates, 'selected_entry': candidates[0] if candidates else None,
            'config_issues': config_issues, 'list_selectors': selectors,
            'pagination_config': ((config or {}).get('list_page') or {}).get('pagination', {}),
            'static_limit_names': cap_names, 'scope_inventory': 'entry_candidates_only_not_complete_scope',
            'current_total': None, 'current_total_quality': 'unknown',
            'enumeration_limit': 'unverified', 'detail_parsing': 'unverified'}


def build():
    from .inventory import inventory, ROOT
    from crawler.sites import CRAWLERS
    baseline = inventory(include_registry=True)
    for row in baseline['sites']:
        cls = CRAWLERS.get(row['site_id'])
        if cls is None:
            row['profile_error'] = 'missing_crawler'
            continue
        path = Path(cls.crawl.__code__.co_filename)
        config_path = getattr(cls, '_config_path', None)
        config = json.loads(Path(config_path).read_text()) if config_path else None
        row.update(static_profile(path.read_text(), config=config, base_url=getattr(cls, 'base_url', None)))
        row['config_source'] = str(Path(config_path).relative_to(ROOT)) if config_path else None
        row['scope_profile_hash'] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return {**baseline, 'observed_at': utc_now(), 'scope': 'static_entry_inventory_only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('output already exists; preserve the previous inventory')
    report = build()
    atomic_json(args.out, report)
    print(json.dumps({'sites': len(report['sites']), 'entries': sum(bool(r.get('selected_entry')) for r in report['sites']),
                      'config_issues': sum(bool(r.get('config_issues')) for r in report['sites'])}))


if __name__ == '__main__':
    main()
