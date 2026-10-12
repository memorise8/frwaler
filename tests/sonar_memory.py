"""Test-only state/storage doubles. No database; JSON round-trip models restart.

Real production Store transactions and Adapter.save are NOT exercised by these doubles.
"""
import copy
from collections import Counter


class MemoryStore:
    def __init__(self):
        self.data = {'sites': {}, 'segments': {}, 'items': {}, 'listed': {}, 'holds': {}, 'events': [], 'files': {}}

    def site(self, site):
        return copy.deepcopy(self.data['sites'].get(site))

    def start_site(self, site, fp, strategy):
        old = self.site(site)
        state = old['state'] if old and old['plan_fp'] == fp else {}
        self.data['sites'][site] = dict(plan_fp=fp, strategy=strategy, state=state, status='running')
        return copy.deepcopy(state)

    def save_state(self, site, state):
        self.data['sites'][site]['state'] = copy.deepcopy(state)

    def finish_site(self, site, status, stop_reason=None, state=None):
        self.data['sites'][site].update(status=status, stop_reason=stop_reason)
        if state is not None:
            self.save_state(site, state)

    def sk(self, site, fp, key):
        return '|'.join((site, fp, key))

    def ensure_segment(self, site, fp, key, *, ordinal=0, parent=None):
        self.data['segments'].setdefault(self.sk(site, fp, key), dict(site_id=site, plan_fp=fp,
            segment_key=key, ordinal=ordinal, parent_key=parent, status='pending', cursor=None,
            detail={}, listed=0, reported_total=None))
        return self.segment(site, fp, key)

    def segment(self, site, fp, key):
        return copy.deepcopy(self.data['segments'].get(self.sk(site, fp, key)))

    def segments(self, site, fp):
        return sorted([copy.deepcopy(r) for r in self.data['segments'].values()
                       if r['site_id'] == site and r['plan_fp'] == fp],
                      key=lambda r: (r['ordinal'], r['segment_key']))

    def update_segment(self, site, fp, key, **kw):
        self.data['segments'][self.sk(site, fp, key)].update(copy.deepcopy(kw))

    def mark_listed(self, site, fp, key, keys):
        sk = self.sk(site, fp, key)
        self.data['listed'][sk] = sorted(set(self.data['listed'].get(sk, [])) | set(keys))
        n = len(self.data['listed'][sk])
        self.data['segments'][sk]['listed'] = n
        return n

    def listed_in_segment(self, site, fp, key, keys):
        return set(keys) & set(self.data['listed'].get(self.sk(site, fp, key), []))

    def listed_count(self, site, fp):
        return len({k for r in self.segments(site, fp)
                    for k in self.data['listed'].get(self.sk(site, fp, r['segment_key']), [])})

    def keys_in_several_segments(self, site, fp, segments):
        counts = Counter(k for s in segments for k in self.data['listed'].get(self.sk(site, fp, s), []))
        return sum(v > 1 for v in counts.values())

    def item(self, site, key):
        return copy.deepcopy(self.data['items'].get(site + '|' + key))

    def known_keys(self, site, keys):
        return {k for k in keys if self.item(site, k)}

    def record_item(self, site, key, status, *, attempt=False, **kw):
        row = self.data['items'].setdefault(site+'|'+key, dict(site_id=site, item_key=key, attempts=0))
        row.update(copy.deepcopy(kw), status=status)
        row['attempts'] += int(attempt)

    def retryable_items(self, site, maximum):
        return [copy.deepcopy(r) for r in self.data['items'].values() if r['site_id'] == site
                and r['status'] == 'failed_transient' and r['attempts'] < maximum]

    def exhaust_items(self, site, maximum):
        for r in self.data['items'].values():
            if r['site_id'] == site and r['status'] == 'failed_transient' and r['attempts'] >= maximum:
                r['status'] = 'failed_retries_exhausted'

    def item_counts(self, site):
        rows = [r for r in self.data['items'].values() if r['site_id'] == site]
        return sum(r['status'] == 'saved' for r in rows), dict(Counter(
            r['status']+':'+str(r.get('reason')) for r in rows if r['status'].startswith('failed_')))

    def host_hold(self, host):
        return self.data['holds'].get(host)

    def hold_host(self, host, reason, url=None, site_id=None):
        self.data['holds'].setdefault(host, dict(reason=reason, url=url, site_id=site_id))

    def event(self, site, kind, detail=None):
        self.data['events'].append(dict(site_id=site, kind=kind, detail=copy.deepcopy(detail)))

    def enqueue_file(self, site, seq, url, **kw):
        self.data['files'].setdefault(f'{site}|{seq}|{url}', dict(url=url, **kw))
