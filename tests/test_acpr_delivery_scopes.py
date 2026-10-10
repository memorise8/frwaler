"""ACPR publisher card/filter shapes and real SQLite/PostgreSQL lifecycle."""
import json
import os
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit,urlencode
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract

BODY='Cette publication présente les résultats et les méthodes de recherche concernant la supervision financière. '*3
BASE='https://acpr.banque-france.fr'
PATHS={'studies':'/fr/publications-et-statistiques/etudes-et-recherche','press':'/fr/communiques-de-presse'}
DETAILS={'studies':'/fr/publications-et-statistiques/publications/','press':'/fr/communiques-de-presse/'}
QUERY=dict(search_publications_api_fulltext='',**{'format[5412591]':'5412591','format[5412600]':'5412600','start-date_year':'','start-date_month':'','end-date_year':'','end-date_month':''})
VIEWS={'studies':'view-id-espace2_liste_des_publications_et_statistique','press':'view-id-espaces2_press_release'}


def page_url(scope,page):return BASE+PATHS[scope]+'?'+urlencode(dict(QUERY if scope=='studies' else {},page=page))


def listing(scope,page,*,ids=None,size=2):
    all_ids=ids if ids is not None else (['s1','s2','s3','s4'] if scope=='studies' else ['p5','p6','p7','p8'])
    last=max(0,(len(all_ids)-1)//size);items=all_ids[page*size:(page+1)*size];cards=[]
    for uid in items:cards.append(f'<a class="card card-vertical" href="{DETAILS[scope]}{uid}"><h3 class="card-title">Research {uid}</h3><span class="category-btn-grid">Publication</span><small>14 Septembre 2026</small></a>')
    links=f'<a aria-current="page" href="{page_url(scope,page)}">{page+1}</a><a href="{page_url(scope,last)}">Last</a>'
    if page>0:links+=f'<a rel="next" title="Aller à la page précédente" href="{page_url(scope,page-1)}">Previous</a>'
    if page<last:links+=f'<a rel="next" href="{page_url(scope,page+1)}">Next</a>'
    pager=f'<nav class="pager">{links}</nav>' if all_ids else '<div class="view-empty">Aucun résultat</div>'
    return f'<main><div class="view {VIEWS[scope]}">{"".join(cards)}{pager}</div></main>'


def detail(uid,*,date='14 Septembre 2026',body=BODY):
    settings=json.dumps({'piano_props':{'custom_publication_statistic_date':date}})
    return f'<h1>Research {uid}</h1><script data-drupal-selector="drupal-settings-json" type="application/json">{settings}</script><article class="node"><div class="field--name-body"><p>{body}</p></div><a data-file-extension="pdf" href="/system/files/{uid}.pdf">Télécharger</a></article>'


class AcprScopes(SiteContract,unittest.TestCase):
    SITE='acpr-banque-france-fr-fr'
    def crawler(self):
        c=self.configured(self.module.ACPRBanqueFranceCrawler(self.conn));self.addCleanup(c._session.close)
        def fetch(url):
            path=urlsplit(url).path
            for scope in PATHS:
                if path==PATHS[scope]:
                    q=parse_qs(urlsplit(url).query,keep_blank_values=True);page=int(q.pop('page')[0]);self.assertEqual(q,{k:[v] for k,v in (QUERY if scope=='studies' else {}).items()});return listing(scope,page)
            return detail(path.rstrip('/').rsplit('/',1)[-1],date=getattr(self,'source_date','14 Septembre 2026'))
        self.fetch=self.enterContext(patch.object(c,'_curl_get',side_effect=fetch));return c

    def test_both_delivered_routes_and_exact_study_filters(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        for scope in PATHS:
            for page in (0,1):self.assertIn(page_url(scope,page),[r.args[0] for r in self.fetch.call_args_list])
        self.assertEqual(set(self.identities()),{'s1','s2','s3','s4','p5','p6','p7','p8'})

    def test_reviewed_dmy_partial_and_invalid_raw_dates_survive_native_storage(self):
        for date,normalized,status,precision in [('Septembre 2026',None,'partial','month'),('03/04/2026','2026-04-03','normalized','day'),('30/07/2026','2026-07-30','normalized','day'),('14 Septembre 2026 incertain',None,'unrecognized',None)]:
            self.source_date=date;c=self.crawler();self.assertEqual(c.crawl(limit=1),1)
            row=self.conn.execute('SELECT published_date,published_date_raw,published_date_status,published_date_precision,listed_date,listed_date_raw FROM documents').fetchone();values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertEqual(str(values[0]) if values[0] else None,normalized);self.assertEqual(values[1:4],(date,status,precision));self.assertEqual(str(values[4]),'2026-09-14');self.assertEqual(values[5],'14 Septembre 2026')
            self.conn.execute('DELETE FROM documents');self.conn.commit()

    def test_filter_loss_or_injected_filter_is_rejected(self):
        for replacement in ('','format%5B5412600%5D=999&'):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url:original(url).replace('format%5B5412600%5D=5412600&',replacement)
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_previous_link_mislabeled_next_is_not_followed(self):
        for scope in PATHS:
            for page in (0,1,2):
                data=self.module._listing(listing(scope,page,ids=['a','b','c','d','e','f']),scope,page)
                self.assertEqual(data['next_url'],page_url(scope,page+1) if page<2 else None)

    def test_missing_forward_link_or_nonadjacent_rel_next_is_rejected(self):
        raw=listing('studies',1,ids=['a','b','c','d','e','f'])
        for altered in (raw.replace('rel="next" href="'+page_url('studies',2)+'"','href="'+page_url('studies',2)+'"'),raw.replace('rel="next" href="'+page_url('studies',2)+'"','rel="next" href="'+page_url('studies',3)+'"')):
            with self.assertRaises(ValueError):self.module._listing(altered,'studies',1)

    def test_zero_limit_and_initial_cancel_make_no_requests(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.fetch.assert_not_called()

    def test_missing_body_does_not_borrow_navigation_or_footer(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url:detail('s4',body='')+f'<footer><p>{BODY}</p></footer>' if url.endswith('/s4') else original(url)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted);self.assertEqual(self.crawler().crawl(),1)

    def test_detail_title_mismatch_is_not_saved(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url:original(url).replace('<h1>Research s1</h1>','<h1>Other document</h1>')
        self.assertEqual(c.crawl(),7);self.assertNotIn('s1',self.identities());self.assertFalse(c.delivery_exhausted)

    def test_existing_identity_is_not_merged_or_reassigned(self):
        c=self.crawler();c.crawl(limit=1);mark='%s' if self.BACKEND=='postgres' else '?';self.conn.execute(f'UPDATE documents SET meta_url={mark}',('https://acpr.banque-france.fr/old-identity',));self.conn.commit()
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(self.count(),8)
        row=self.conn.execute("SELECT meta_url FROM documents WHERE post_number='s1'").fetchone();self.assertEqual(row['meta_url'] if isinstance(row,dict) else row[0],'https://acpr.banque-france.fr/old-identity')

    def test_source_error_and_empty_listing_are_distinguished(self):
        for result,reason in [('<h1>Error</h1>','invalid_response'),(None,'fetch_failed'),(PermissionError('HTTP 403'),'authentication_failed'),(PermissionError('HTTP 429'),'rate_limited'),(ConnectionError('HTTP 503'),'fetch_failed')]:
            c=self.crawler();self.fetch.side_effect=result if isinstance(result,Exception) else lambda url:result
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,reason);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();self.fetch.side_effect=lambda url:listing('studies' if 'etudes-et-recherche' in url else 'press',0,ids=[])
        self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted)

    def test_no_200_page_cap(self):
        c=self.crawler();original=self.fetch.side_effect;ids=[f's{n}' for n in range(1000,1201)]
        self.fetch.side_effect=lambda url:listing('studies',int(parse_qs(urlsplit(url).query)['page'][0]),ids=ids,size=1) if 'etudes-et-recherche?' in url else original(url)
        self.assertEqual(c.crawl(),205);self.assertTrue(c.delivery_exhausted)
        self.assertIn(page_url('studies',200),[x.args[0] for x in self.fetch.call_args_list])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AcprScopesPostgres(AcprScopes):BACKEND='postgres'


class AcprCursor(AcprScopes):
    def backfill(self,cursor=None):
        c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;return c

    def test_three_three_two_resumes_across_scope_boundary(self):
        cursor=None
        for expected in (3,3,2):
            c=self.backfill(cursor);self.assertEqual(c.crawl(limit=3),expected);cursor=json.loads(json.dumps(c.delivery_pending_cursor));self.assertTrue(all(not isinstance(v,(dict,list)) for v in cursor.values()))
        self.assertTrue(c.delivery_exhausted);self.assertEqual(self.count(),8)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted)

    def test_cancel_after_commit_preserves_completed_offset(self):
        c=self.backfill();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        cursor=c.delivery_pending_cursor;self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),('studies',1,1));before=self.identities()
        c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(all(self.identities()[key]==value for key,value in before.items()))

    def test_body_and_save_failures_hold_position(self):
        for failure in ('body','save'):
            c=self.backfill();original=self.fetch.side_effect;save=c._save_paper
            if failure=='body':self.fetch.side_effect=lambda url:detail('s4',body='') if url.endswith('/s4') else original(url)
            else:
                def fail(paper):
                    if paper['external_id']=='s4':raise RuntimeError('save failed')
                    return save(paper)
                c._save_paper=fail
            self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'item_failed');cursor=c.delivery_pending_cursor;self.assertEqual(cursor['offset'],1)
            c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)
            self.conn.execute('DELETE FROM documents');self.conn.commit()

    def test_changed_previous_current_or_pagination_is_not_resumed(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for kind in ('previous','current','last','empty'):
            c=self.backfill(cursor);original=self.fetch.side_effect
            def fetch(url):
                raw=original(url)
                if url==page_url('studies',0) and kind=='previous':return raw.replace('Research s2','Changed title')
                if url==page_url('studies',1):
                    if kind=='current':return raw.replace('Research s4','Changed title')
                    if kind=='last':return raw.replace('page=1','page=2')
                    if kind=='empty':return listing('studies',0,ids=[])
                return raw
            self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)

    def test_malformed_cursor_is_rejected_without_network(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for change in ({'page':False},{'offset':3},{'page_hash':''},{'version':2},{'last_page':0},{'scope':'all'}):
            c=self.backfill(dict(cursor,**change));self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()

    def test_wall_budget_saves_cursor_and_full_mode_rescans(self):
        c=self.backfill()
        with patch.object(self.module,'_WALL_SECONDS',.5),patch.object(self.module.time,'monotonic',side_effect=lambda:1 if self.count()>=3 else 0):self.assertEqual(c.crawl(),3)
        self.assertIsNone(c.delivery_source_failure);cursor=c.delivery_pending_cursor
        c=self.crawler();c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AcprCursorPostgres(AcprCursor):BACKEND='postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AcprCursorWorker(unittest.TestCase):
    def test_worker_cancel_resume_auto_chain(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=AcprCursorPostgres(methodName='test_three_three_two_resumes_across_scope_boundary')
        try:
            case.setUp();init_delivery_schema(case.conn)
            class Factory:
                DELIVERY_CURSOR_VERSION=case.module.ACPRBanqueFranceCrawler.DELIVERY_CURSOR_VERSION
                DELIVERY_ORDER='arbitrary'
                def __new__(cls,**kw):return case.crawler()
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:Factory},delay=0),3);job=jobs.claim_next_job(case.conn)
            def cancel():
                if case.count()>=4:jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0,should_cancel=cancel),1);self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled');self.assertIsNone(jobs.claim_next_job(case.conn))
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3);counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8);self.assertIsNotNone(case.conn.execute('SELECT completed_at FROM crawl_site_progress').fetchone()['completed_at'])
        finally:case.doCleanups()


class AcprTransport(unittest.TestCase):
    def setUp(self):
        import importlib.util
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'crawler/sites/custom/acpr-banque-france-fr-fr.py';spec=importlib.util.spec_from_file_location('acpr_transport',path);self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m);self.c=self.m.ACPRBanqueFranceCrawler(None);self.addCleanup(self.c._session.close)

    def test_denial_and_service_failure_stop_without_retry(self):
        for status,error in [(403,PermissionError),(429,PermissionError),(503,ConnectionError)]:
            with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=f'error\n{status}'.encode())) as run:
                with self.assertRaises(error):self.c._curl_get(BASE)
                self.assertEqual(run.call_count,1);self.assertNotIn('-skL',run.call_args.args[0])

    def test_response_body_and_cancellation(self):
        with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=b'<h1>Source</h1>\n200')) as run:
            self.assertEqual(self.c._curl_get(BASE),'<h1>Source</h1>');self.c.delivery_should_cancel=lambda:True
            with self.assertRaises(CrawlCancelled):self.c._curl_get(BASE)
            self.assertEqual(run.call_count,1)
