"""Explicit PostgreSQL test-data recovery with durable backups and resumption.

This tool never chooses documents by today's date. Operators must supply exact
seq_ids, a site, a timezone-aware interval, and a reviewed cursor policy. Stop
workers before planning/executing. Plan is read-only; apply/restore are separate.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

from psycopg import sql
from psycopg.types.json import Jsonb

from crawler.blob_storage import get_blob_path
from crawler.document_identity import find_krivet_document
from delivery.scripts.repair_metadata import digest, write_private

# Parent-first order for restoring the document graph. Jobs/batch history outside
# this graph is retained. Unknown foreign keys or triggers stop the operation.
TABLES = ('documents', 'document_lang', 'document_translations', 'document_anomaly',
          'translation_jobs', 'translation_job_attempts', 'document_summaries',
          'document_summary_quality', 'document_file_jobs')
CONTROL = ('crawl_jobs', 'crawl_schedules', 'crawl_site_progress',
           'translation_batches', 'translation_worker_heartbeats')
ALLOWED_FKS = {
    ('document_lang','documents'), ('document_translations','documents'),
    ('document_anomaly','documents'), ('translation_jobs','documents'),
    ('translation_job_attempts','translation_jobs'), ('document_summaries','documents'),
    ('document_summary_quality','document_summaries'), ('document_file_jobs','documents'),
    ('translation_worker_heartbeats','translation_jobs'),
}
FK_COLUMNS = {'translation_job_attempts':('job_id','id'),
              'document_summary_quality':('summary_id','summary_id'),
              'translation_worker_heartbeats':('current_job_id','id')}


def schema_info(conn):
    columns = conn.execute("""SELECT table_name,column_name,data_type,udt_name,is_nullable,
      column_default,is_identity,is_generated,generation_expression
      FROM information_schema.columns WHERE table_schema='public' AND table_name=ANY(%s)
      ORDER BY table_name,ordinal_position""", (list(TABLES+CONTROL),)).fetchall()
    tables = {r['table_name'] for r in columns}
    if not {'documents','crawl_jobs','crawl_schedules','crawl_site_progress'} <= tables:
        raise ValueError('required recovery schema is missing')
    constraints = conn.execute("""SELECT c.conrelid::regclass::text AS child,
      c.confrelid::regclass::text AS parent, c.contype,
      pg_get_constraintdef(c.oid) AS definition
      FROM pg_constraint c WHERE c.conrelid IN
        (SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))
      OR c.confrelid IN
        (SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))
      ORDER BY child,parent,definition""", (list(TABLES+CONTROL),list(TABLES),)).fetchall()
    for constraint in constraints:
        if constraint['contype'] == 'f' and constraint['parent'] in TABLES:
            if (constraint['child'],constraint['parent']) not in ALLOWED_FKS:
                raise ValueError('unknown document reference: '+constraint['child'])
            child,parent=constraint['child'],constraint['parent']
            left,right=FK_COLUMNS.get(child,('seq_id','seq_id'))
            action='RESTRICT' if child=='document_translations' else ('SET NULL' if child=='translation_worker_heartbeats' else 'CASCADE')
            expected=f'FOREIGN KEY ({left}) REFERENCES {parent}({right}) ON DELETE {action}'
            if constraint['definition']!=expected:
                raise ValueError('unreviewed document foreign key: '+child)
    triggers = conn.execute("""SELECT 1 FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN
      (SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace AND relname=ANY(%s))""",
      (list(TABLES+CONTROL),)).fetchall()
    if triggers:
        raise ValueError('custom triggers require manual review')
    return {'columns': columns, 'constraints': constraints}


def present(schema, name):
    return any(c['table_name'] == name for c in schema['columns'])


@contextmanager
def locked(conn):
    if conn.info.transaction_status != 0:
        raise ValueError('recovery requires an idle connection')
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '5s'")
        schema = schema_info(conn)
        names = [name for name in TABLES+CONTROL if present(schema,name)]
        conn.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(
            sql.SQL(',').join(map(sql.Identifier, sorted(names)))))
        if schema_info(conn)!=schema:
            raise ValueError('schema changed while acquiring locks; retry preview')
        yield schema


def table_rows(conn, table, where, params):
    # where is constructed only from the static clauses in snapshot().
    return [r['row'] for r in conn.execute(sql.SQL('SELECT to_jsonb(t) AS row FROM {} t WHERE '+where+
                      ' ORDER BY to_jsonb(t)::text').format(sql.Identifier(table)),params).fetchall()]


def snapshot(conn, schema, ids, site):
    result = {}
    for name in TABLES:
        if not present(schema,name):
            continue
        if name == 'translation_job_attempts':
            where='job_id IN (SELECT id FROM translation_jobs WHERE seq_id=ANY(%s))'
        elif name == 'document_summary_quality':
            where='summary_id IN (SELECT summary_id FROM document_summaries WHERE seq_id=ANY(%s))'
        else:
            where='seq_id=ANY(%s)'
        result[name] = table_rows(conn,name,where,(ids,))
    result['crawl_site_progress'] = table_rows(conn,'crawl_site_progress','site_id=%s',(site,))
    return result


def ensure_idle(conn, schema, site, ids):
    if conn.execute("SELECT 1 FROM crawl_jobs WHERE site_id=%s AND status IN ('queued','running','cancelling') LIMIT 1",(site,)).fetchone():
        raise ValueError('crawl jobs must be stopped')
    if conn.execute('SELECT 1 FROM crawl_schedules WHERE site_id=%s AND enabled LIMIT 1',(site,)).fetchone():
        raise ValueError('site schedules must be disabled')
    for name, active in [('translation_jobs',['pending','running']),('document_file_jobs',['queued','running'])]:
        if present(schema,name) and conn.execute(sql.SQL('SELECT 1 FROM {} WHERE seq_id=ANY(%s) AND status=ANY(%s) LIMIT 1').format(sql.Identifier(name)),(ids,active)).fetchone():
            raise ValueError(name+' must be stopped')
    if present(schema,'translation_worker_heartbeats') and present(schema,'translation_jobs'):
        if conn.execute('SELECT 1 FROM translation_worker_heartbeats WHERE current_job_id IN (SELECT id FROM translation_jobs WHERE seq_id=ANY(%s)) LIMIT 1',(ids,)).fetchone():
            raise ValueError('translation worker still owns a selected job')
    if present(schema,'document_translations') and conn.execute("SELECT 1 FROM document_translations WHERE seq_id=ANY(%s) AND state IN ('pending','running') LIMIT 1",(ids,)).fetchone():
        raise ValueError('document translation must be stopped')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def file_inventory(root, ids):
    root=Path(root).resolve(strict=True)
    result=[]
    for seq in ids:
        base=get_blob_path(seq,root=root)
        for path in sorted(base.parent.glob(f'{seq:012d}.*')):
            if path.is_symlink() or path.resolve()!=path or not path.is_file():
                raise ValueError('non-regular blob path requires review')
            result.append({'relative':str(path.relative_to(root)), 'size':path.stat().st_size,
                           'sha256':file_hash(path)})
    return result


def make_plan(conn, *, site_id, seq_ids, collected_from, collected_to, blob_root,
              cursor_before=None, rescan=False):
    ids=sorted(set(seq_ids))
    if not ids or len(ids)>1000 or any(type(n) is not int or n<=0 for n in ids):
        raise ValueError('supply 1–1000 explicit positive seq_ids')
    start,end=(datetime.fromisoformat(v) for v in (collected_from,collected_to))
    if start.tzinfo is None or end.tzinfo is None or start>=end:
        raise ValueError('explicit timezone-aware half-open interval required')
    if rescan and site_id!='krivet-re-kr':
        raise ValueError('rescan policy currently reviewed only for KRIVET')
    if not rescan and cursor_before is None:
        raise ValueError('pre-test cursor snapshot required for this site')
    with locked(conn) as schema:
        ensure_idle(conn,schema,site_id,ids)
        before=snapshot(conn,schema,ids,site_id)
        selected=conn.execute('SELECT seq_id FROM documents WHERE seq_id=ANY(%s) AND site_id=%s AND collected_at >= %s AND collected_at < %s',
                              (ids,site_id,start,end)).fetchall()
        if sorted(r['seq_id'] for r in selected)!=ids:
            raise ValueError('explicit IDs do not all match site/time interval')
        previous=before['crawl_site_progress']
        if rescan:
            after_progress=[dict(row,cursor=None,items_done=0,total_estimate=None,completed_at=None) for row in previous]
        else:
            if not isinstance(cursor_before,list) or len(cursor_before)>1:
                raise ValueError('cursor snapshot must be [] or one complete row')
            columns={r['column_name'] for r in schema['columns'] if r['table_name']=='crawl_site_progress'}
            if any(set(row)!=columns or row['site_id']!=site_id for row in cursor_before):
                raise ValueError('cursor snapshot must match site and complete schema')
            after_progress=cursor_before
        after={name:[] for name in before}
        after['crawl_site_progress']=after_progress
        plan={'version':1,'site_id':site_id,'seq_ids':ids,'interval':[collected_from,collected_to],
              'database':conn.execute('SELECT current_database() AS name').fetchone()['name'],
              'schema':schema,'before':before,'after':after,
              'cursor_policy':'krivet_rescan' if rescan else 'explicit_pre_test_snapshot',
              'files':file_inventory(blob_root,ids)}
    return {'plan':plan,'sha256':digest(plan)}


def insert_rows(conn, schema, name, records):
    columns=[r['column_name'] for r in schema['columns'] if r['table_name']==name and r['is_generated']=='NEVER']
    for row in records:
        conn.execute(sql.SQL('INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM jsonb_populate_record(NULL::{},%s)').format(
            sql.Identifier(name),sql.SQL(',').join(map(sql.Identifier,columns)),
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(name)),(Jsonb(row),))


def replace_progress(conn, plan, target):
    conn.execute('DELETE FROM crawl_site_progress WHERE site_id=%s',(plan['site_id'],))
    insert_rows(conn,plan['schema'],'crawl_site_progress',plan[target]['crawl_site_progress'])


def checked_plan(envelope):
    plan=envelope['plan']
    if plan['version']!=1 or digest(plan)!=envelope['sha256']:
        raise ValueError('invalid recovery plan checksum/version')
    # Plans are private operator artifacts, nevertheless disallow path traversal.
    for item in plan['files']:
        path=Path(item['relative'])
        if path.is_absolute() or '..' in path.parts or len(path.parts)!=3:
            raise ValueError('invalid backup path')
        if not any(path.name.startswith(f'{seq:012d}.') and path.parent == get_blob_path(seq,root=Path('.')).parent
                   for seq in plan['seq_ids']):
            raise ValueError('backup path does not belong to selected documents')
    return plan


def prepare_backup(envelope, blob_root, directory):
    plan=checked_plan(envelope)
    root=Path(blob_root).resolve(strict=True)
    directory=Path(directory).resolve()
    if directory==root or directory.is_relative_to(root):
        raise ValueError('backup directory must be outside blob root')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('backup directory must be private (mode 0700)')
    sync_directory(directory.parent)
    manifest=directory/'manifest.json'
    if manifest.exists():
        if json.loads(manifest.read_text())!=envelope:
            raise ValueError('backup belongs to another plan')
    else:
        write_private(manifest,envelope)
        sync_directory(directory)
    for item in plan['files']:
        source=root/item['relative'];target=directory/'files'/item['relative']
        if source.resolve()!=source or target.resolve()!=target:
            raise ValueError('symlinked blob/backup path requires review')
        if target.exists() and file_hash(target)==item['sha256']:
            continue
        if not source.is_file() or source.is_symlink() or source.resolve()!=source or file_hash(source)!=item['sha256']:
            raise ValueError('blob changed after preview')
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(target.name+'.tmp')
        with source.open('rb') as inp, temporary.open('wb') as out:
            shutil.copyfileobj(inp,out)
            out.flush();os.fsync(out.fileno())
        if file_hash(temporary)!=item['sha256']:
            raise ValueError('backup checksum mismatch')
        os.replace(temporary,target)
        # Persist directory entries as well as file content before DB deletion.
        parent=target.parent
        while parent.is_relative_to(directory):
            sync_directory(parent)
            parent=parent.parent
    return directory


def reconcile_files(plan, root, backup, *, restore):
    for item in plan['files']:
        source=backup/'files'/item['relative'];target=root/item['relative']
        if source.resolve()!=source or target.resolve()!=target:
            raise ValueError('symlinked blob/backup path requires review')
        if not source.is_file() or source.is_symlink() or file_hash(source)!=item['sha256']:
            raise ValueError('backup is incomplete or corrupt')
        if target.exists():
            if target.is_symlink() or target.resolve()!=target or file_hash(target)!=item['sha256']:
                raise ValueError('refusing to overwrite/remove a changed blob')
        if restore and not target.exists():
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_name('.'+target.name+'.recovery-tmp')
            with source.open('rb') as inp, temporary.open('wb') as out:
                shutil.copyfileobj(inp,out);out.flush();os.fsync(out.fileno())
            os.replace(temporary,target)
            sync_directory(target.parent)
        elif not restore and target.exists():
            target.unlink()  # Verified durable copy remains in the private backup.
            sync_directory(target.parent)


def run(conn, envelope, *, blob_root, backup_dir, restore=False, workers_stopped=False):
    if not workers_stopped:
        raise ValueError('stop workers before recovery')
    plan=checked_plan(envelope)
    root=Path(blob_root).resolve(strict=True)
    if restore:
        backup=Path(backup_dir).resolve(strict=True)
        if json.loads((backup/'manifest.json').read_text())!=envelope:
            raise ValueError('backup manifest mismatch')
    else:
        backup=prepare_backup(envelope,root,backup_dir)
    expected,target=('after','before') if restore else ('before','after')
    with locked(conn) as schema:
        if schema!=plan['schema'] or conn.execute('SELECT current_database() AS name').fetchone()['name']!=plan['database']:
            raise ValueError('database/schema differs from reviewed plan')
        ensure_idle(conn,schema,plan['site_id'],plan['seq_ids'])
        current=snapshot(conn,schema,plan['seq_ids'],plan['site_id'])
        if current not in (plan[expected],plan[target]):
            raise ValueError('data changed after preview; no changes applied')
        actual_files=file_inventory(root,plan['seq_ids'])
        if any(item not in plan['files'] for item in actual_files):
            raise ValueError('unexpected or changed blob after preview')
        if restore:
            if current!=plan[target] and plan['site_id']=='krivet-re-kr':
                for row in plan['before']['documents']:
                    if find_krivet_document(conn,row['site_id'],row['post_number'],row['meta_url'],postgres=True) is not None:
                        raise ValueError('recollected identity conflicts with backup; manual review required')
            reconcile_files(plan,root,backup,restore=True)
        elif current==plan[expected] and file_inventory(root,plan['seq_ids'])!=plan['files']:
            raise ValueError('files changed after preview')
        if current!=plan[target]:
            if restore:
                for name in TABLES:
                    if name in plan['before']:
                        insert_rows(conn,schema,name,plan['before'][name])
            else:
                # RESTRICT translations must be explicitly removed. Other graph
                # rows follow verified foreign keys; unknown references block.
                if present(schema,'document_translations'):
                    conn.execute('DELETE FROM document_translations WHERE seq_id=ANY(%s)',(plan['seq_ids'],))
                conn.execute('DELETE FROM documents WHERE seq_id=ANY(%s)',(plan['seq_ids'],))
            replace_progress(conn,plan,target)
            if snapshot(conn,schema,plan['seq_ids'],plan['site_id'])!=plan[target]:
                raise ValueError('post-change graph mismatch; transaction rolled back')
    # DB commit precedes quarantine. A crash here is recovered by rerunning apply:
    # the exact after-state is accepted and only outstanding files are processed.
    if not restore:
        reconcile_files(plan,root,backup,restore=False)
    return {'state':'restored' if restore else 'quarantined','documents':len(plan['seq_ids'])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('plan','apply','restore'))
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--site-id')
    parser.add_argument('--seq-ids',type=Path,help='JSON array of explicitly confirmed document IDs')
    parser.add_argument('--collected-from')
    parser.add_argument('--collected-to')
    parser.add_argument('--blob-root',type=Path,required=True)
    parser.add_argument('--backup-dir',type=Path)
    parser.add_argument('--cursor-before',type=Path)
    parser.add_argument('--krivet-rescan',action='store_true')
    parser.add_argument('--workers-stopped',action='store_true')
    args=parser.parse_args()
    from crawler.db_pg import open_db
    conn=open_db(os.environ['RECOVERY_PG_DSN'])
    try:
        if args.action=='plan':
            envelope=make_plan(conn,site_id=args.site_id,seq_ids=json.loads(args.seq_ids.read_text()),
              collected_from=args.collected_from,collected_to=args.collected_to,blob_root=args.blob_root,
              cursor_before=json.loads(args.cursor_before.read_text()) if args.cursor_before else None,
              rescan=args.krivet_rescan)
            write_private(args.plan,envelope)
            print(json.dumps({'documents':len(envelope['plan']['seq_ids']),'files':len(envelope['plan']['files'])}))
        else:
            if not args.backup_dir:
                parser.error('--backup-dir is required for apply/restore')
            print(json.dumps(run(conn,json.loads(args.plan.read_text()),blob_root=args.blob_root,
                             backup_dir=args.backup_dir,restore=args.action=='restore',workers_stopped=args.workers_stopped)))
    finally:
        conn.close()


if __name__=='__main__':main()
