"""Bounded metadata repair: plan first, then apply or restore exact changes.

No connection defaults to an operational database. PostgreSQL credentials are
read from REPAIR_PG_DSN; plans contain data and must be stored privately.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from crawler.document_dates import DATE_FIELDS, DATE_METADATA_FIELDS, normalize_document_dates
from crawler.document_identity import krivet_id

FIELDS = ('post_number',) + DATE_FIELDS + DATE_METADATA_FIELDS


def is_pg(conn):
    return not isinstance(conn, sqlite3.Connection)


def execute(conn, query, values=()):
    return conn.execute(query.replace('?', '%s') if is_pg(conn) else query, values)


def rows(conn, query, values=()):
    cur = execute(conn, query, values)
    names = [d[0] for d in cur.description]
    return [dict(row) if is_pg(conn) else dict(zip(names, row)) for row in cur.fetchall()]


@contextmanager
def locked_documents(conn):
    if is_pg(conn):
        if conn.info.transaction_status != 0:
            raise ValueError('repair requires an idle connection')
        with conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute('LOCK TABLE documents IN SHARE ROW EXCLUSIVE MODE')
            yield
    else:
        if conn.in_transaction:
            raise ValueError('repair requires an idle connection')
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def make_plan(conn, *, site_id, kind, after_seq=0, limit=500, date_order=None, evidence=None):
    if kind not in ('krivet', 'dates') or not site_id or not 1 <= limit <= 1000 or after_seq < 0:
        raise ValueError('explicit site, kind and bounded batch required')
    if kind == 'krivet' and site_id != 'krivet-re-kr':
        raise ValueError('KRIVET repair is limited to krivet-re-kr')
    if date_order and not evidence:
        raise ValueError('numeric date order requires source-format evidence')
    selected = rows(conn, 'SELECT * FROM documents WHERE site_id=? AND seq_id>? ORDER BY seq_id LIMIT ?',
                    (site_id, after_seq, limit))
    collisions = []
    groups = {}
    if kind == 'krivet':
        for row in rows(conn, 'SELECT seq_id,meta_url FROM documents WHERE site_id=?', (site_id,)):
            try:
                groups.setdefault(krivet_id(row['meta_url']), []).append(row['seq_id'])
            except ValueError:
                pass
    changes, unresolved = [], []
    for row in selected:
        before = {key: row[key] for key in FIELDS}
        after = dict(before)
        if kind == 'krivet':
            try:
                wanted = krivet_id(row['meta_url'])
            except ValueError:
                unresolved.append({'seq_id': row['seq_id'], 'reason': 'invalid_pstNo'})
                continue
            if len(groups[wanted]) > 1:
                collisions.append({'post_number': wanted, 'seq_ids': groups[wanted]})
                continue
            after['post_number'] = wanted
        else:
            source = dict(row)
            for field in DATE_FIELDS:
                if row[f'{field}_status'] not in (None, 'legacy_unchecked'):
                    source[field] = row[f'{field}_raw']
            normalized = normalize_document_dates(source, order=date_order)
            after.update({key: normalized[key] for key in DATE_FIELDS + DATE_METADATA_FIELDS})
            for field in DATE_FIELDS:
                if after[f'{field}_status'] not in ('normalized', 'missing'):
                    unresolved.append({'seq_id': row['seq_id'], 'field': field,
                                       'reason': after[f'{field}_status']})
        if before != after:
            changes.append({'seq_id': row['seq_id'], 'site_id': site_id,
                            'meta_url': row['meta_url'], 'before': before, 'after': after})
    plan = {'version': 1, 'kind': kind, 'site_id': site_id, 'date_order': date_order,
            'format_evidence': evidence, 'inspected': len(selected),
            'next_after_seq': selected[-1]['seq_id'] if selected else after_seq,
            'changes': changes, 'collisions': collisions, 'unresolved': unresolved}
    return {'plan': plan, 'sha256': digest(plan)}


def apply_plan(conn, envelope, *, restore=False):
    plan = envelope['plan']
    if envelope['sha256'] != digest(plan) or plan['version'] != 1:
        raise ValueError('invalid plan checksum/version')
    if plan['collisions']:
        raise ValueError('identity collisions require manual review')
    applied = 0
    with locked_documents(conn):
        for change in plan['changes']:
            current = rows(conn, 'SELECT * FROM documents WHERE seq_id=?', (change['seq_id'],))
            if not current or any(current[0][key] != change[key] for key in ('site_id', 'meta_url')):
                raise ValueError('document identity changed after preview')
            old, new = change['before'], change['after']
            if set(old) != set(FIELDS) or set(new) != set(FIELDS):
                raise ValueError('invalid repair fields')
            expected, target = (new, old) if restore else (old, new)
            actual = {key: current[0][key] for key in FIELDS}
            if actual == target:
                continue
            if actual != expected:
                raise ValueError('document changed after preview; create a new plan')
            if plan['kind'] == 'krivet' and not restore:
                wanted = krivet_id(change['meta_url'])
                for other in rows(conn, 'SELECT seq_id,meta_url FROM documents WHERE site_id=?', (change['site_id'],)):
                    if other['seq_id'] == change['seq_id']:
                        continue
                    try:
                        same = krivet_id(other['meta_url']) == wanted
                    except ValueError:
                        same = False
                    if same:
                        raise ValueError('new KRIVET identity collision after preview')
            execute(conn, 'UPDATE documents SET '+','.join(f'{key}=?' for key in FIELDS)+' WHERE seq_id=?',
                    tuple(target[key] for key in FIELDS)+(change['seq_id'],))
            applied += 1
    return applied


def write_private(path, payload):
    # Exclusive creation prevents overwriting the only rollback record.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'restore'))
    parser.add_argument('--sqlite', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--site-id')
    parser.add_argument('--kind', choices=('krivet', 'dates'))
    parser.add_argument('--after-seq', type=int, default=0)
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--date-order', choices=('DMY', 'MDY'))
    parser.add_argument('--format-evidence')
    args = parser.parse_args()
    if args.sqlite:
        mode = 'ro' if args.action == 'plan' else 'rw'
        conn = sqlite3.connect(args.sqlite.resolve().as_uri()+f'?mode={mode}', uri=True)
        conn.execute('PRAGMA foreign_keys=ON')
    else:
        from crawler.db_pg import open_db
        conn = open_db(os.environ['REPAIR_PG_DSN'])
        if args.action == 'plan':
            conn.execute('SET TRANSACTION READ ONLY')
    try:
        if args.action == 'plan':
            payload = make_plan(conn, site_id=args.site_id, kind=args.kind, after_seq=args.after_seq,
                                limit=args.limit, date_order=args.date_order, evidence=args.format_evidence)
            write_private(args.plan, payload)
            print(json.dumps({k: len(payload['plan'][k]) for k in ('changes','collisions','unresolved')}))
        else:
            print(json.dumps({'changed': apply_plan(conn, json.loads(args.plan.read_text()),
                                                   restore=args.action == 'restore')}))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
