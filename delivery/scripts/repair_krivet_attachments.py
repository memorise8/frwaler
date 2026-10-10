"""Preview/apply/restore exact, source-reviewed KRIVET attachment URL changes.

Only the custom collector krivet-re-kr-kor is targeted; the separate generic
krivet-re-kr collector is not included. Only pdf_url is writable.
Evidence is a separately reviewed source manifest;
source references do not certify PDF bytes or authorize resetting file flags.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from crawler.document_identity import krivet_id
from delivery.scripts.repair_metadata import execute, rows, locked_documents, is_pg, digest, write_private

SITE = 'krivet-re-kr-kor'
OLD_PATH = '/kor/com/fileDown.do'
NEW_PATH = '/com/file/fileDown.do'


def attachment_key(url, path):
    parsed = urlsplit(url or '')
    q = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.krivet.re.kr'
            or parsed.path != path or parsed.fragment
            or any(len(v) != 1 for v in q.values())
            or set(q) - {'fileDownDiv', 'pstNo', 'pstgAtchNo', 'pstgClsfCd', 'fileName'}
            or q.get('fileDownDiv') != ['NOTICOMM']
            or not re.fullmatch('[A-Za-z0-9_-]+', q.get('pstNo', [''])[0])
            or not re.fullmatch('[0-9]+', q.get('pstgAtchNo', [''])[0])):
        raise ValueError('invalid KRIVET attachment source identity')
    return q['pstNo'][0], q['pstgAtchNo'][0], q.get('pstgClsfCd', [None])[0]


def validate_pair(before, after, uid):
    old, new = attachment_key(before, OLD_PATH), attachment_key(after, NEW_PATH)
    if old[:2] != new[:2] or old[0] != uid or (old[2] and old[2] != new[2]):
        raise ValueError('attachment identity changed')


def protected_hash(row):
    raw = json.dumps({k: v for k, v in row.items() if k != 'pdf_url'},
                     ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def evidence_map(evidence):
    if not re.fullmatch('[0-9a-f]{64}', str(evidence.get('source_manifest_sha256', ''))):
        raise ValueError('reviewed source manifest hash required')
    found = {}
    for entry in evidence.get('records', []):
        uid = entry['post_number']
        if uid in found or krivet_id(entry['source_url']) != uid:
            raise ValueError('ambiguous source document evidence')
        if not re.fullmatch('[0-9a-f]{64}', str(entry.get('source_sha256', ''))):
            raise ValueError('source response hash required')
        if entry.get('pdf_url') and attachment_key(entry['pdf_url'], NEW_PATH)[0] != uid:
            raise ValueError('source PDF identity mismatch')
        found[uid] = entry
    if not found:
        raise ValueError('source evidence records required')
    return found


def file_table_exists(conn):
    if is_pg(conn):
        return bool(rows(conn, "SELECT to_regclass('public.document_file_jobs') AS name")[0]['name'])
    return bool(rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='document_file_jobs'"))


def active_files(conn, seq):
    return file_table_exists(conn) and bool(rows(conn,
        "SELECT seq_id FROM document_file_jobs WHERE seq_id=? AND status IN ('queued','running')", (seq,)))


def identity_groups(conn):
    groups = {}
    for row in rows(conn, 'SELECT seq_id,meta_url FROM documents WHERE site_id=?', (SITE,)):
        try:
            groups.setdefault(krivet_id(row['meta_url']), []).append(row['seq_id'])
        except (ValueError, TypeError):
            pass
    return groups


def make_plan(conn, evidence, *, after_seq=0, limit=500):
    if type(limit) is not int or not 1 <= limit <= 1000 or type(after_seq) is not int or after_seq < 0:
        raise ValueError('bounded batch required')
    sources = evidence_map(evidence)
    selected = rows(conn, 'SELECT * FROM documents WHERE site_id=? AND seq_id>? ORDER BY seq_id LIMIT ?',
                    (SITE, after_seq, limit))
    groups = identity_groups(conn)
    changes, unresolved = [], []
    for row in selected:
        before = row['pdf_url']
        if not before or urlsplit(before).path != OLD_PATH:
            continue
        try:
            uid = krivet_id(row['meta_url'])
            if row['post_number'] != uid or len(groups.get(uid, [])) != 1:
                raise ValueError('document identity collision or repair required')
            if uid not in sources or not sources[uid].get('pdf_url'):
                raise ValueError('missing reviewed primary attachment')
            after = sources[uid]['pdf_url']
            validate_pair(before, after, uid)
            if any(row.get(k) for k in ('pdf_downloaded', 'text_extracted', 'pdf_sha256', 'pdf_size_bytes')):
                raise ValueError('existing file artifact requires separate review')
            if active_files(conn, row['seq_id']):
                raise ValueError('active file job requires review')
            changes.append(dict(seq_id=row['seq_id'], site_id=SITE, post_number=uid,
                                meta_url=row['meta_url'], before=before, after=after,
                                protected_sha256=protected_hash(row), source=sources[uid]))
        except (ValueError, TypeError) as exc:
            unresolved.append(dict(seq_id=row['seq_id'], reason=str(exc)))
    plan = dict(version=1, kind='krivet_attachment_url', site_id=SITE,
                source_manifest_sha256=evidence['source_manifest_sha256'], evidence_sha256=digest(evidence),
                inspected=len(selected), next_after_seq=selected[-1]['seq_id'] if selected else after_seq,
                changes=changes, unresolved=unresolved)
    return dict(plan=plan, sha256=digest(plan))


def apply_plan(conn, envelope, *, restore=False):
    plan = envelope['plan']
    if (envelope['sha256'] != digest(plan) or type(plan.get('version')) is not int or plan['version'] != 1
            or plan.get('kind') != 'krivet_attachment_url' or plan.get('site_id') != SITE
            or plan.get('unresolved') or len(plan['changes']) > 1000):
        raise ValueError('invalid or unresolved attachment repair plan')
    applied = 0
    with locked_documents(conn):
        # Block file-queue transitions/insertions while checking and applying.
        if is_pg(conn) and file_table_exists(conn):
            conn.execute('LOCK TABLE document_file_jobs IN SHARE ROW EXCLUSIVE MODE')
        groups = identity_groups(conn)
        for change in plan['changes']:
            current = rows(conn, 'SELECT * FROM documents WHERE seq_id=?', (change['seq_id'],))
            if not current:
                raise ValueError('document missing after preview')
            row = current[0]
            if (any(row[k] != change[k] for k in ('site_id', 'post_number', 'meta_url'))
                    or row['site_id'] != SITE or krivet_id(row['meta_url']) != row['post_number']
                    or len(groups.get(row['post_number'], [])) != 1):
                raise ValueError('document identity collision or changed after preview')
            validate_pair(change['before'], change['after'], row['post_number'])
            if change['source']['pdf_url'] != change['after'] or change['source']['post_number'] != row['post_number']:
                raise ValueError('changed source evidence')
            if protected_hash(row) != change['protected_sha256']:
                raise ValueError('document changed after preview')
            if active_files(conn, row['seq_id']):
                raise ValueError('active file job requires review')
            expected, target = (change['after'], change['before']) if restore else (change['before'], change['after'])
            if row['pdf_url'] == target:
                continue
            if row['pdf_url'] != expected:
                raise ValueError('attachment changed after preview')
            execute(conn, 'UPDATE documents SET pdf_url=? WHERE seq_id=?', (target, row['seq_id']))
            applied += 1
    return applied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'restore'))
    parser.add_argument('--sqlite', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--after-seq', type=int, default=0)
    parser.add_argument('--limit', type=int, default=500)
    args = parser.parse_args()
    if args.sqlite:
        mode = 'ro' if args.action == 'plan' else 'rw'
        conn = sqlite3.connect(args.sqlite.resolve().as_uri()+f'?mode={mode}', uri=True)
    else:
        from crawler.db_pg import open_db
        conn = open_db(os.environ['REPAIR_PG_DSN'])
        if args.action == 'plan':
            conn.execute('SET TRANSACTION READ ONLY')
    try:
        if args.action == 'plan':
            if not args.evidence:
                parser.error('--evidence is required for plan')
            envelope = make_plan(conn, json.loads(args.evidence.read_text()), after_seq=args.after_seq, limit=args.limit)
            write_private(args.plan, envelope)
            print(json.dumps({k: len(envelope['plan'][k]) for k in ('changes', 'unresolved')}))
        else:
            print(json.dumps(dict(changed=apply_plan(conn, json.loads(args.plan.read_text()), restore=args.action == 'restore'))))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
