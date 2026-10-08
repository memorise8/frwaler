"""Read-only, streaming audit of every stored site/document and bounded source evidence.

Duplicate keys are candidates, not automatic errors. Only reviewed source rules
can establish identity. Source manifests establish coverage only for their stated
scope; database-only checks cannot prove that no source documents were missed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from crawler.document_dates import normalize_date
from crawler.document_identity import krivet_id


def postgres(conn):
    return not isinstance(conn,sqlite3.Connection)


def stream(conn, query, *, large=False):
    cursor=conn.cursor(name='full_document_audit') if postgres(conn) and large else conn.cursor()
    try:
        cursor.execute(query)
        names=[column[0] for column in cursor.description]
        for row in cursor:
            yield dict(row) if postgres(conn) else dict(zip(names,row))
    finally:
        cursor.close()


def columns(conn):
    if postgres(conn):
        result=defaultdict(set)
        for row in stream(conn,"SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'"):
            result[row['table_name']].add(row['column_name'])
        return result
    result={}
    for table in ('documents','document_lang','document_translations','document_anomaly','translation_jobs',
                  'translation_job_attempts','document_summaries','document_summary_quality','document_file_jobs',
                  'crawl_site_progress','crawl_jobs'):
        result[table]={row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    return result


def identity(site, post, url):
    if site=='krivet-re-kr':
        try:return ('reviewed_source_id',krivet_id(url))
        except (ValueError,TypeError):return ('invalid_source_id',str(url))
    return ('exact_key_url_only',str(post),str(url))


def audit(conn, *, emit=lambda event:None, source_manifest=None):
    """Caller must provide an idle connection; a consistent read-only transaction is used."""
    previous_query_only=None
    if postgres(conn):
        if conn.info.transaction_status!=0:raise ValueError('audit requires an idle connection')
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    else:
        if conn.in_transaction:raise ValueError('audit requires an idle connection')
        previous_query_only=conn.execute('PRAGMA query_only').fetchone()[0]
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
    try:
        schema=columns(conn)
        required={'seq_id','site_id','post_number','meta_url','title','published_date','listed_date'}
        if not required<=schema.get('documents',set()):raise ValueError('unsupported document schema')
        manifest=source_manifest or {'documents':[]}
        if source_manifest and (not manifest.get('site_id') or not manifest.get('scope') or not manifest.get('observed_at')):
            raise ValueError('source manifest requires site_id, scope and observed_at')
        wanted={identity(manifest.get('site_id'),r.get('post_number'),r.get('meta_url')):r
                for r in manifest['documents']}
        if len(wanted)!=len(manifest['documents']):
            raise ValueError('duplicate source identities in supplied manifest')
        by_post=defaultdict(set);by_url=defaultdict(set)
        for key,record in wanted.items():
            if record.get('post_number'):by_post[record['post_number']].add(key)
            if record.get('meta_url'):by_url[record['meta_url']].add(key)
        matched=Counter();possible=set()
        site_reports=defaultdict(Counter)
        selected=list(sorted(required))+[name for name in ('published_date_raw','published_date_status','listed_date_raw','listed_date_status') if name in schema['documents']]
        for row in stream(conn,'SELECT '+','.join(selected)+' FROM documents ORDER BY seq_id',large=True):
            site=row['site_id'];report=site_reports[site];report['documents']+=1
            issues=[]
            post=row['post_number'];url=row['meta_url']
            if post is None or not str(post).strip():
                report['missing_or_blank_id']+=1;issues.append('missing_or_blank_id')
            if post is not None and str(post)!=str(post).strip():
                report['id_surrounding_whitespace']+=1;issues.append('id_surrounding_whitespace')
            try:
                parsed=urlsplit(url or '')
                valid_url=parsed.scheme in ('http','https') and bool(parsed.hostname)
            except ValueError:valid_url=False
            if not valid_url:report['invalid_meta_url']+=1;issues.append('invalid_meta_url')
            if not (row['title'] or '').strip():report['missing_title']+=1;issues.append('missing_title')
            if site=='krivet-re-kr':
                report['source_identity_checked']+=1
                try:
                    expected=krivet_id(url)
                    if post!=expected:
                        report['confirmed_wrong_key']+=1;issues.append('wrong_key_extraction')
                except (TypeError,ValueError):
                    report['invalid_source_identity']+=1;issues.append('invalid_source_identity')
            else:
                report['source_identity_rule_unverified']+=1
            for field in ('published_date','listed_date'):
                raw=row.get(field+'_raw') if row.get(field+'_status') not in (None,'legacy_unchecked') else row[field]
                parsed=normalize_date(raw)
                # A reviewed batch may have resolved DMY/MDY using evidence kept
                # in its repair plan. Do not undo that decision by guessing again.
                if row.get(field+'_status')=='normalized' and parsed['status']=='ambiguous':
                    possibilities={normalize_date(raw,order=o)['value'] for o in ('DMY','MDY')}
                    if row[field] in possibilities and row[field] is not None:
                        parsed={'value':row[field],'status':'normalized'}
                report[field+'_'+parsed['status']]+=1
                if row.get(field+'_status') in (None,'legacy_unchecked'):
                    report[field+'_legacy_unchecked']+=1
                if parsed['status'] not in ('normalized','missing'):
                    issues.append(field+'_'+parsed['status'])
                if parsed['value'] is not None and parsed['value']!=row[field]:
                    report[field+'_correction_candidate']+=1;issues.append(field+'_correction_candidate')
            if issues:
                emit({'kind':'document','site_id':site,'seq_id':row['seq_id'],'post_number':post,
                      'meta_url':url,'findings':issues})
            if source_manifest and site==manifest['site_id']:
                key=identity(site,post,url)
                if key in wanted:matched[key]+=1
                elif site!='krivet-re-kr':
                    possible.update(by_post.get(post,()))
                    possible.update(by_url.get(url,()))
        duplicate_groups=0
        for row in stream(conn,'''SELECT site_id,post_number,count(*) AS documents,
                  count(DISTINCT meta_url) AS distinct_meta_urls FROM documents
                  GROUP BY site_id,post_number HAVING count(*)>1 ORDER BY count(*) DESC''',large=True):
            duplicate_groups+=1;site_reports[row['site_id']]['duplicate_key_groups']+=1
            emit({'kind':'duplicate_key_candidate',**row,'classification':'requires_site_rule_and_source_review'})
        references=[]
        for child,left,parent,right in (
            ('document_lang','seq_id','documents','seq_id'),('document_translations','seq_id','documents','seq_id'),
            ('document_anomaly','seq_id','documents','seq_id'),('translation_jobs','seq_id','documents','seq_id'),
            ('document_summaries','seq_id','documents','seq_id'),('document_file_jobs','seq_id','documents','seq_id'),
            ('translation_job_attempts','job_id','translation_jobs','id'),
            ('document_summary_quality','summary_id','document_summaries','summary_id')):
            if left not in schema.get(child,set()) or right not in schema.get(parent,set()):
                references.append({'table':child,'state':'not_present_or_unverified'});continue
            orphan=next(stream(conn,f'SELECT count(*) AS n FROM {child} c WHERE NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{right}=c.{left})'))['n']
            references.append({'table':child,'state':'checked','orphans':orphan})
        source_results=[]
        for key,record in wanted.items():
            state=('ambiguous_existing' if matched[key]>1 else 'present' if matched[key]==1 else
                   'ambiguous_existing' if key in possible or key[0]=='invalid_source_id' else
                   'missing_in_reviewed_source_scope' if key[0]=='reviewed_source_id' else 'missing_candidate_rule_unverified')
            item={'kind':'source_comparison','post_number':record.get('post_number'),
                  'meta_url':record.get('meta_url'),'state':state,'matching_documents':matched[key]}
            source_results.append(item);emit(item)
        fk_scope=[]
        if postgres(conn):
            fk_scope=list(stream(conn,"""SELECT conrelid::regclass::text AS child,confrelid::regclass::text AS parent,
              pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE contype='f'
              AND connamespace='public'::regnamespace ORDER BY child,parent,definition"""))
        return {'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'all_stored_documents_all_sites',
                'read_only':True,'total_documents':sum(r['documents'] for r in site_reports.values()),
                'total_sites':len(site_reports),'sites':{s:dict(v) for s,v in sorted(site_reports.items())},
                'duplicate_key_groups':duplicate_groups,'duplicate_groups_are_not_automatic_errors':True,
                'reference_checks':references,'observed_foreign_keys':fk_scope,
                'file_content_inspection':'not_performed_db_flags_are_not_file_verification',
                'source_comparison':{'scope':manifest.get('scope'),'observed_at':manifest.get('observed_at'),
                                     'results':dict(Counter(r['state'] for r in source_results))},
                'source_completeness':'unverified_outside_supplied_manifest',
                'customer_incident_scope':'requires_test_sites_job_ids_times_versions_and_before_after_evidence'}
    finally:
        conn.rollback()
        if previous_query_only is not None:
            conn.execute(f'PRAGMA query_only={previous_query_only}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source-manifest',type=Path)
    args=parser.parse_args()
    args.out.mkdir(mode=0o700,parents=True,exist_ok=False)
    if args.sqlite:
        conn=sqlite3.connect(args.sqlite.resolve().as_uri()+'?mode=ro',uri=True)
    else:
        from crawler.db_pg import open_db
        conn=open_db(os.environ['AUDIT_PG_DSN'])
    manifest=json.loads(args.source_manifest.read_text()) if args.source_manifest else None
    try:
        with (args.out/'findings.jsonl').open('x') as findings:
            result=audit(conn,source_manifest=manifest,emit=lambda row:findings.write(json.dumps(row,ensure_ascii=False)+'\n'))
        (args.out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({key:result[key] for key in ('total_documents','total_sites','duplicate_key_groups')}))
    finally:conn.close()


if __name__=='__main__':main()
