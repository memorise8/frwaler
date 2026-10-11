"""Bounded live replay measurements in new, explicitly owned SQLite databases.

DOAJ/NTRS filters are test scopes, not completion of their global catalogues.
No network stubs, operating DB access, or external publication.
"""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_adapters import DOAJAdapter, HALAdapter, NTRSAdapter
from crawler.sites import CRAWLERS


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);ap.add_argument('--sites',nargs='*');args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    os.environ['LIBERTREE_DB_BACKEND']='sqlite'
    report={'scope':'live bounded source queries; NOT global source completion','sites':[]}
    def persist():
        (out/'results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    specs=[
      ('doaj-org-search',lambda c:DOAJAdapter(c,'created_date:[2026-10-11T14:00:00Z TO 2026-10-11T15:00:00Z]')),
      ('cea-hal-science-cnrgh',HALAdapter),
      ('ntrs',lambda c:NTRSAdapter(c,{'title':'Dynamics of an Abyssal Circulation Driven by Bottom-Intensified Mixing on Slopes'}))]
    for site,factory in specs:
        if args.sites and site not in args.sites:continue
        site_out=out/site;site_out.mkdir()
        conn=db_libertree.open_db(site_out/'owned.sqlite');db_libertree.init_db(conn);init_schema(conn)
        db_libertree.upsert_site(conn,site,site,CRAWLERS[site].base_url)
        record={'site_id':site,'phases':[],'requests':[]};report['sites'].append(record);persist()
        def instance():
            c=CRAWLERS[site](conn,delay=1.5)
            native=c._session.request
            def request(method,url,**kw):
                response=native(method,url,**kw)
                n=len(record['requests']);body=response.content
                (site_out/f'{n:03}.body').write_bytes(body)
                record['requests'].append({'method':method,'url':response.url,'status':response.status_code,
                                            'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
                persist();return response
            c._session.request=request
            a=factory(c)
            # Small pages expose stopping behavior even in tiny finite scopes.
            if hasattr(a,'size'):a.size=3
            if hasattr(a,'rows'):a.rows=100
            return c,a
        try:
            for phase in ['initial','repeat','remove_latest3_repeat']:
                removed=[]
                if phase=='remove_latest3_repeat':
                    # Only delete documents in this newly-created owned DB.
                    keys=conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 3').fetchall()
                    removed=[r[0] for r in keys]
                    conn.executemany('DELETE FROM documents WHERE post_number=?',[(k,) for k in removed]);conn.commit()
                before=conn.execute('SELECT count(*) FROM documents').fetchone()[0]
                c,a=instance();collector=Collector(c,a,Policy(max_pages=20,wall_seconds=120,full_check_days=0))
                start_requests=len(record['requests']);saved=collector.run()
                state=Store(conn,site).state()
                info={'phase':phase,'http_requests':len(record['requests'])-start_requests,'removed':removed,
                      'saved_new':saved,'rows_before':before,'rows_after':conn.execute('SELECT count(*) FROM documents').fetchone()[0],
                      'engine':collector.stats,'state':state,'scope':a.scope}
                record['phases'].append(info);persist();print(site,phase,info['http_requests'],saved,state['phase'],flush=True)
                if state['phase']!='idle':
                    record['status']='bounded_budget_incomplete';break
                if phase=='repeat' and saved!=0:raise AssertionError('unexpected repeated insertion')
                if removed:
                    remaining={r[0] for r in conn.execute('SELECT post_number FROM documents')}
                    assert set(removed)<=remaining
            else:record['status']='measured'
        except Exception as exc:
            record.update(status='incomplete',error_type=type(exc).__name__,error=str(exc))
            print(site,type(exc).__name__,str(exc),flush=True)
        finally:conn.close();persist()

if __name__=='__main__':main()
