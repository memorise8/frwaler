"""Offline acceptance fixtures exercise real crawler loops and SQLite writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from crawler import db_libertree as db
from crawler.sites import CRAWLERS
from crawler.search_credentials import SEARCH_KEYS, missing_credentials, redact
from delivery.capabilities import HAL_RESUME, capabilities, validate_request, CapabilityError


class CollectionContractTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'LIBERTREE_DB_BACKEND':'sqlite'})
        self.env.start(); self.addCleanup(self.env.stop)

    def database(self, site):
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close); db.init_db(conn)
        db.upsert_site(conn,site,'Fixture','https://fixture.invalid')
        conn.executemany('INSERT INTO documents(site_id,post_number,meta_url,title) VALUES(?,?,?,?)',
                         [(site,f'old-{i}',f'https://fixture.invalid/old/{i}',f'Old {i}') for i in range(20000)])
        conn.commit(); return conn

    @staticmethod
    def total(conn):
        return conn.execute('SELECT count(*) FROM documents').fetchone()[0]

    def test_doaj_repeated_three_passes_known_fifty_prefix_and_finishes_200(self):
        site='doaj-org-search'; conn=self.database(site)
        items=[{'id':str(i),'bibjson':{'title':f'New {i}','abstract':'x'*150}} for i in range(200)]
        def response(url):
            q=parse_qs(urlparse(url).query); page=int(q['page'][0]); size=int(q['pageSize'][0])
            return json.dumps({'results':items[(page-1)*size:page*size],'total':200})
        for n in range(18):
            c=CRAWLERS[site](conn,delay=0)
            with mock.patch.object(c,'_curl_get',side_effect=response), contextlib.redirect_stdout(io.StringIO()):
                c.crawl(limit=3)
            self.assertEqual(self.total(conn),20000+(n+1)*3)
        c=CRAWLERS[site](conn,delay=0)
        with mock.patch.object(c,'_curl_get',side_effect=response), contextlib.redirect_stdout(io.StringIO()):
            c.crawl(limit=None)
        self.assertEqual(self.total(conn),20200)

    def test_hal_families_resume_actual_position_and_skip_existing_in_limit(self):
        for site in sorted(HAL_RESUME):
            with self.subTest(site=site):
                conn=self.database(site)
                docs=[{'docid':i,'halId_s':f'hal-{i}','uri_s':f'https://fixture.invalid/{i}',
                       'title_s':[f'New {i}'],'abstract_s':['x'*200]} for i in range(200)]
                cursor=None
                for expected in (3,6):
                    c=CRAWLERS[site](conn,delay=0); c.delivery_cursor=cursor
                    # Return a page larger than the requested limit to force a
                    # genuine within-page stop; no mocked save/cursor logic.
                    with mock.patch.object(c,'_fetch_page',side_effect=lambda start,rows:{'response':{'numFound':200,'docs':docs[start:start+10]}}), contextlib.redirect_stdout(io.StringIO()):
                        c.crawl(limit=3)
                    cursor=c.delivery_pending_cursor
                    self.assertEqual(self.total(conn),20000+expected)
                    self.assertEqual(cursor,{'offset':expected, 'version':2})
                # A lost cursor causes duplicate reads, not duplicate limits.
                c=CRAWLERS[site](conn,delay=0)
                with mock.patch.object(c,'_fetch_page',side_effect=lambda start,rows:{'response':{'numFound':200,'docs':docs[start:start+10]}}),contextlib.redirect_stdout(io.StringIO()):
                    c.crawl(limit=3)
                self.assertEqual(self.total(conn),20009)
                self.assertEqual(c.delivery_pending_cursor,{'offset':9, 'version':2})

    def test_hal_failed_item_does_not_advance_past_failure(self):
        for site in HAL_RESUME:
            with self.subTest(site=site):
                c=CRAWLERS[site](None,delay=0)
                docs=[{'docid':i,'halId_s':f'hal-{i}','uri_s':f'https://fixture.invalid/{i}',
                       'title_s':['Title'],'abstract_s':['x'*200]} for i in range(10)]
                with mock.patch.object(c,'_fetch_page',return_value={'response':{'docs':docs}}), mock.patch.object(c,'_save_paper',side_effect=RuntimeError('fixture')), contextlib.redirect_stdout(io.StringIO()):
                    c.crawl(limit=3)
                self.assertIsNone(c.delivery_pending_cursor)
                self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_both_scienceon_collectors_count_new_records_across_runs(self):
        for site,root in (('scienceon-api','JAKO'),('scienceon-foreign-api','NART')):
            with self.subTest(site=site):
                conn=self.database(site)
                class Client:
                    def search(self,query,page,**kwargs):
                        records=[{'CN':f'{root}{i:08d}','DBCode':root,'Title':f'New {i}'} for i in range(200)]
                        return 200,records[(page-1)*100:page*100]
                for expected in (20003,20006):
                    c=CRAWLERS[site](conn,delay=0,client=Client());c.ROOTS=(root,)
                    with contextlib.redirect_stdout(io.StringIO()):c.crawl(limit=3)
                    self.assertEqual(self.total(conn),expected)


class CapabilityAndKeyTest(unittest.TestCase):
    def test_search_errors_are_not_empty_success_for_all_eight_keyed_collectors(self):
        # Exercise the actual helper entry points. Stub only their transport.
        cases = {
          'education-govt-nz-our-work':('_algolia_query',(), 'process_text','results'),
          'education-govt-nz-search':('_algolia_query',(0,), '_request','results'),
          'inrae-fr-actualites':('_algolia_page',(0,), '_curl_post','hits'),
          'nkvts-no-english':('_algolia_page',(0,), 'session','hits'),
          'phfscience-nz-digital-library':('_algolia_query',({},'test'), '_curl_post_json','hits'),
          'phfscience-nz-news-publications':('_algolia_query',(), 'process_bytes','hits'),
          'cancer-fr-catalogue-des-public':('_algolia_page',('',0), '_curl_post_json','hits'),
          'transparency-gov-au-publications':('_fetch_page',(0,3), 'session','value'),
        }
        for site,(method,args,transport,field) in cases.items():
            for denied in (False,True):
                with self.subTest(site=site,denied=denied),contextlib.ExitStack() as stack:
                    c=CRAWLERS[site](None,delay=0)
                    payload={'status':403,'message':'fixture denied'} if denied else {field:[{'hits':[]}] if field=='results' else []}
                    raw=json.dumps(payload)
                    stack.enter_context(mock.patch('time.sleep'))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    response=mock.Mock();response.json.return_value=payload
                    if transport.startswith('process'):
                        stack.enter_context(mock.patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout=raw if transport=='process_text' else raw.encode(),stderr='')))
                    elif transport=='session':
                        stack.enter_context(mock.patch.object(c._session,'post',return_value=response))
                        if denied and field=='value':
                            import requests
                            response.status_code=403
                            response.raise_for_status.side_effect=requests.HTTPError(response=response)
                    elif transport=='_request':
                        stack.enter_context(mock.patch.object(c,transport,return_value=response))
                    else:
                        stack.enter_context(mock.patch.object(c,transport,return_value=raw))
                    getattr(c,method)(*args)
                    self.assertEqual(c.delivery_source_failure,'authentication_failed' if denied else None)

    def test_secret_redaction_handles_split_stdout_writes(self):
        from delivery.worker.worker import _TeeCapture
        output=io.StringIO(); capture=_TeeCapture(output,10)
        with mock.patch.dict(os.environ,{'EDUCATION_GOVT_NZ_SEARCH_KEY':'split-fixture-secret'}):
            capture.write('header split-fixture-'); capture.write('secret\n')
            self.assertNotIn('split-fixture-secret',output.getvalue())
            self.assertEqual(capture.lines(),['header [REDACTED]'])

    def test_missing_optional_and_required_credentials(self):
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertFalse(missing_credentials('cancer-fr-catalogue-des-public'))
            for site in set(SEARCH_KEYS)-{'cancer-fr-catalogue-des-public'}:
                self.assertEqual(missing_credentials(site),[SEARCH_KEYS[site]])
            self.assertEqual(len(missing_credentials('scienceon-api')),3)

    def test_secret_forwarding_is_limited_and_redaction(self):
        from delivery.health.probe import child_environment
        fake={key:'fixture-secret-'+str(i) for i,key in enumerate(SEARCH_KEYS.values())}
        with mock.patch.dict(os.environ,{**fake,'UNRELATED_SECRET':'never-forward'}):
            with tempfile.TemporaryDirectory() as directory:
                env=child_environment(Path(directory),60)
            for k,v in fake.items(): self.assertEqual(env[k],v)
            self.assertNotIn('UNRELATED_SECRET',env)
            self.assertNotIn(next(iter(fake.values())),redact('header '+next(iter(fake.values()))))

    def test_capability_restrictions_and_truthful_evidence(self):
        with self.assertRaises(CapabilityError): validate_request('doaj-org-search','incremental')
        data=validate_request('doaj-org-search','incremental',trial=True)
        self.assertEqual(data['verification']['live'],'not_verified')
        self.assertEqual(data['features']['chronological_resume'],'unsupported')
        with self.assertRaises(CapabilityError): validate_request('scienceon-api','full',trial=True)
        with self.assertRaises(CapabilityError): validate_request('usgs-gov-news','backfill',trial=True)
        self.assertEqual(capabilities('usgs-gov-news')['count_semantics'],'unverified')

    def test_compose_passes_all_keys_only_to_consumers(self):
        import yaml
        root=Path(__file__).resolve().parents[1]
        main=yaml.safe_load((root/'delivery/docker-compose.yml').read_text())
        health=yaml.safe_load((root/'delivery/docker-compose.health.yml').read_text())
        for service in (main['services']['worker'],main['services']['be'],health['services']['health']):
            for key in SEARCH_KEYS.values(): self.assertIn(key,service['environment'])
        for key in SEARCH_KEYS.values(): self.assertNotIn(key,main['services']['file-worker']['environment'])


if __name__=='__main__': unittest.main()
