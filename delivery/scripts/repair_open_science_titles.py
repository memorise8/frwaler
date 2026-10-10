"""Plan, apply or restore source-bound Open Science title-only corrections.

No default database. Source evidence must be independently reviewed. Existing
IDs, dates, files, translations and all other document fields are protected.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3

from crawler.open_science_titles import source_title, legacy_title
from delivery.scripts.repair_metadata import execute, rows, locked_documents, is_pg, digest, write_private

SITE='open-science-canada-ca-communities'
BASE='https://open-science.canada.ca'

def protected_hash(row):
    return digest(json.loads(json.dumps({k:v for k,v in row.items() if k!='title'}, default=str)))

def evidence_map(evidence):
    if not re.fullmatch('[0-9a-f]{64}',str(evidence.get('source_manifest_sha256',''))):
        raise ValueError('reviewed source manifest hash required')
    result={}
    for record in evidence.get('records',[]):
        item=record['source_item'];uid=item.get('uuid');handle=item.get('handle','')
        if (not isinstance(uid,str) or not re.fullmatch('[0-9a-fA-F-]{36}',uid)
                or item.get('type')!='item' or record.get('post_number')!=uid
                or not re.fullmatch(r'123456789/[0-9]+',handle)
                or record.get('meta_url')!=BASE+'/handle/'+handle
                or (item.get('_links',{}).get('self') or {}).get('href')!=BASE+'/server/api/core/items/'+uid
                or not re.fullmatch('[0-9a-f]{64}',str(record.get('source_sha256','')))
                or uid in result or not source_title(item)):
            raise ValueError('missing or ambiguous source title identity')
        result[uid]=record
    if not result:
        raise ValueError('source evidence records required')
    return result

def table_exists(conn,name):
    if is_pg(conn):
        return bool(rows(conn,"SELECT to_regclass(?) AS name",('public.'+name,))[0]['name'])
    return bool(rows(conn,"SELECT name FROM sqlite_master WHERE type='table' AND name=?",(name,)))

def active_metadata(conn):
    return table_exists(conn,'crawl_jobs') and bool(rows(conn,
        "SELECT id FROM crawl_jobs WHERE site_id=? AND status IN ('queued','running','cancelling')",(SITE,)))

def unique_identity(conn,uid,url):
    found=rows(conn,'SELECT seq_id,post_number,meta_url FROM documents WHERE site_id=? AND (post_number=? OR meta_url=?)',(SITE,uid,url))
    return len(found)==1 and found[0]['post_number']==uid and found[0]['meta_url']==url

def make_plan(conn,evidence,*,after_seq=0,limit=500):
    if type(limit) is not int or not 1<=limit<=1000 or type(after_seq) is not int or after_seq<0:
        raise ValueError('bounded batch required')
    sources=evidence_map(evidence);changes=[];unresolved=[]
    selected=rows(conn,'SELECT * FROM documents WHERE site_id=? AND seq_id>? ORDER BY seq_id LIMIT ?',(SITE,after_seq,limit))
    busy=active_metadata(conn)
    for row in selected:
        source=sources.get(row['post_number'])
        if not source:
            continue
        after=source_title(source['source_item'])
        try:
            if busy:
                raise ValueError('active metadata job requires review')
            if row['meta_url']!=source['meta_url'] or not unique_identity(conn,row['post_number'],row['meta_url']):
                raise ValueError('document identity collision or source mismatch')
            if row['title']==after:
                continue
            if row['title']!=legacy_title(source['source_item']):
                raise ValueError('current title is not the exact legacy source normalization')
            changes.append(dict(seq_id=row['seq_id'],site_id=SITE,post_number=row['post_number'],meta_url=row['meta_url'],before=row['title'],after=after,protected_sha256=protected_hash(row),source=source))
        except (ValueError,TypeError) as exc:
            unresolved.append(dict(seq_id=row['seq_id'],reason=str(exc)))
    plan=dict(version=1,kind='open_science_source_title',site_id=SITE,source_manifest_sha256=evidence['source_manifest_sha256'],evidence_sha256=digest(evidence),inspected=len(selected),next_after_seq=selected[-1]['seq_id'] if selected else after_seq,changes=changes,unresolved=unresolved)
    return dict(plan=plan,sha256=digest(plan))

def apply_plan(conn,envelope,*,restore=False):
    plan=envelope['plan']
    if (envelope['sha256']!=digest(plan) or type(plan.get('version')) is not int or plan['version']!=1
            or plan.get('kind')!='open_science_source_title' or plan.get('site_id')!=SITE
            or plan.get('unresolved') or len(plan['changes'])>1000
            or len({c['seq_id'] for c in plan['changes']})!=len(plan['changes'])):
        raise ValueError('invalid or unresolved source title repair plan')
    evidence_map(dict(source_manifest_sha256=plan['source_manifest_sha256'],records=[c['source'] for c in plan['changes']])) if plan['changes'] else None
    applied=0
    with locked_documents(conn):
        if is_pg(conn) and table_exists(conn,'crawl_jobs'):
            conn.execute('LOCK TABLE crawl_jobs IN SHARE ROW EXCLUSIVE MODE')
        if active_metadata(conn):
            raise ValueError('active metadata job requires review')
        for change in plan['changes']:
            current=rows(conn,'SELECT * FROM documents WHERE seq_id=?',(change['seq_id'],))
            if not current:
                raise ValueError('document missing after preview')
            row=current[0];source=change['source'];item=source['source_item']
            if (any(row[k]!=change[k] for k in ('site_id','post_number','meta_url')) or row['site_id']!=SITE
                    or source['post_number']!=row['post_number'] or source['meta_url']!=row['meta_url']
                    or not unique_identity(conn,row['post_number'],row['meta_url'])
                    or change['after']!=source_title(item) or change['before']!=legacy_title(item)
                    or protected_hash(row)!=change['protected_sha256']):
                raise ValueError('source evidence, identity or protected document changed')
            expected,target=(change['after'],change['before']) if restore else (change['before'],change['after'])
            if row['title']==target:
                continue
            if row['title']!=expected:
                raise ValueError('title changed after preview')
            execute(conn,'UPDATE documents SET title=? WHERE seq_id=?',(target,row['seq_id']));applied+=1
    return applied

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['plan','apply','restore']);parser.add_argument('--sqlite',type=Path);parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--evidence',type=Path);parser.add_argument('--after-seq',type=int,default=0);parser.add_argument('--limit',type=int,default=500);args=parser.parse_args()
    if args.sqlite:
        conn=sqlite3.connect(args.sqlite.resolve().as_uri()+('?mode=ro' if args.action=='plan' else '?mode=rw'),uri=True)
    else:
        from crawler.db_pg import open_db
        conn=open_db(os.environ['REPAIR_PG_DSN'])
        if args.action=='plan':conn.execute('SET TRANSACTION READ ONLY')
    try:
        if args.action=='plan':
            if not args.evidence:parser.error('--evidence required for plan')
            result=make_plan(conn,json.loads(args.evidence.read_text()),after_seq=args.after_seq,limit=args.limit);write_private(args.plan,result);print(json.dumps({k:len(result['plan'][k]) for k in ['changes','unresolved']}))
        else:print(json.dumps(dict(changed=apply_plan(conn,json.loads(args.plan.read_text()),restore=args.action=='restore'))))
    finally:conn.close()

if __name__=='__main__':main()
