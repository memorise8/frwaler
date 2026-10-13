"""Task 10: real file ledger/blob paths with synthetic in-memory HTTP only.

All SQLite files are disposable fixtures inside the test container. No site,
operational/customer database or real socket is used by these added tests.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
from unittest import TestCase, mock

from crawler import blob_storage
from crawler.enumeration.files import FilePass
from crawler.enumeration.http import Fetcher, Response, RequestsTransport
from crawler.enumeration.run import main, run_site
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, PDF, board_plan
from tests.test_enumeration_fix2 import FakeClock


class FilesStage(TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo',
                       'socket.create_connection', 'subprocess.Popen._execute_child', 'psycopg.connect'):
            self.stack.enter_context(mock.patch(target, side_effect=AssertionError('real I/O forbidden')))
        self.clock = FakeClock()
        self.stack.enter_context(mock.patch('delivery.health.request_gate.time', self.clock))
        self.stack.enter_context(mock.patch('crawler.enumeration.http.time', self.clock))
        self.stack.enter_context(mock.patch('crawler.enumeration.files._time', self.clock))
        self.board = FakeBoard(3)
        self.env.run(board_plan(), self.board)
        self.plan = self.env.plan(board_plan())
        self.blob = self.env.root / 'blobs'

    def runner(self, handler=None, *, interval=0, max_requests=None, max_attempts=3, max_bytes=200*1024*1024):
        t = FakeTransport(handler or self.board)
        f = Fetcher(self.env.site_id, t, self.env.root/'file-gates', interval=interval,
                    max_requests=max_requests, retries=3, backoff_s=0, holds=self.env.store,
                    clock=self.clock.monotonic, sleep=self.clock.sleep,
                    resolver=lambda h:['93.184.216.34'])
        return FilePass(self.env.store, f, blob_root=self.blob, doc_conn=self.env.conn,
                        strict_access=True, max_attempts=max_attempts, max_bytes=max_bytes), t

    def files(self):
        return self.env.store.files(self.env.site_id)

    def test_received_pdf_records_size_hash_type_pages_and_document_marker(self):
        fp, transport = self.runner()
        stats = fp.run(self.env.site_id)
        self.assertEqual(3, stats['done'])
        for row in self.files():
            self.assertEqual(('done',len(PDF),hashlib.sha256(PDF).hexdigest(),'application/pdf',2),
                (row['status'],row['size'],row['sha256'],row['content_type'],row['pages']))
            self.assertEqual(PDF,Path(row['path']).read_bytes())
            doc=self.env.conn.execute('SELECT pdf_downloaded,pdf_sha256 FROM documents WHERE seq_id=?',
                                      (row['seq_id'],)).fetchone()
            self.assertEqual((1,row['sha256']),tuple(doc))
        second,t = self.runner()
        self.assertEqual(0,second.run(self.env.site_id)['done'])
        self.assertEqual([],t.urls)

    def test_same_hash_and_duplicate_queue_key(self):
        first=self.files()[0]
        self.env.store.enqueue_file(self.env.site_id,first['seq_id'],first['url'],role='pdf')
        fp,_=self.runner();fp.run(self.env.site_id)
        self.assertEqual(3,len(self.files()))
        self.assertEqual(1,len({r['sha256'] for r in self.files()}))
        self.assertEqual(3,len({r['path'] for r in self.files()}))  # per-document primary layout, not global CAS

    def test_budget_resume_preserves_pending_and_downloads_remaining(self):
        fp,t=self.runner(max_requests=2)  # robots + first PDF
        result=fp.run(self.env.site_id)
        self.assertEqual((1,'request_budget'),(result['done'],result['stopped']))
        self.assertEqual({'done':1,'pending':2},self.env.store.file_counts(self.env.site_id))
        fp,t=self.runner();self.assertEqual(2,fp.run(self.env.site_id)['done'])
        self.assertEqual(2,len([u for u in t.urls if not u.endswith('robots.txt')]))

    def test_partial_atomic_write_interrupted_then_refetched_without_truncation(self):
        seq=self.files()[0]['seq_id'];path=blob_storage.get_blob_path(seq,root=self.blob)
        path.parent.mkdir(parents=True);path.write_bytes(PDF)
        original=blob_storage.os.replace
        def interrupted(src,dst):
            if Path(dst) != path:
                return original(src,dst)
            self.assertEqual(PDF,Path(dst).read_bytes())
            self.assertEqual(PDF,Path(src).read_bytes())
            raise KeyboardInterrupt('crash before atomic replace')
        fp,_=self.runner()
        with mock.patch('crawler.blob_storage.os.replace',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):fp.run(self.env.site_id)
        self.assertEqual(PDF,path.read_bytes())
        self.assertEqual([],list(path.parent.glob('.'+path.name+'.*')))
        self.assertEqual('pending',self.files()[0]['status'])
        fp,_=self.runner();self.assertEqual(3,fp.run(self.env.site_id)['done'])

    def test_blob_and_db_written_before_ledger_interrupt_reconciles(self):
        original=self.env.store.update_file
        def interrupted(*args,**kw):
            if kw.get('status')=='done':raise KeyboardInterrupt('after document/blob before ledger')
            return original(*args,**kw)
        fp,_=self.runner()
        with mock.patch.object(self.env.store,'update_file',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):fp.run(self.env.site_id)
        fp,t=self.runner();result=fp.run(self.env.site_id)
        self.assertEqual((3,1),(result['done'],result['reconciled']))
        self.assertEqual(2,len([u for u in t.urls if not u.endswith('robots.txt')]))

    def test_invalid_existing_blob_matching_db_hash_is_not_reconciled(self):
        row=self.files()[0];bad=b'<html>login</html>';path=blob_storage.get_blob_path(row['seq_id'],root=self.blob)
        path.parent.mkdir(parents=True);path.write_bytes(bad)
        self.env.conn.execute('UPDATE documents SET pdf_downloaded=1,pdf_sha256=? WHERE seq_id=?',
                              (hashlib.sha256(bad).hexdigest(),row['seq_id']));self.env.conn.commit()
        fp,t=self.runner();result=fp.run(self.env.site_id)
        self.assertEqual(3,result['done']);self.assertEqual(0,result.get('reconciled',0))
        self.assertEqual(PDF,path.read_bytes())

    def test_new_url_for_same_document_cannot_reconcile_an_old_blob(self):
        fp,_=self.runner();fp.run(self.env.site_id)
        row=self.files()[0];new_url='https://board.test/changed.pdf'
        self.env.store.enqueue_file(self.env.site_id,row['seq_id'],new_url,role='pdf')
        # Even if the body refresh has already replaced the document's advertised URL,
        # its existing download/hash still describe the previous file candidate.
        self.env.conn.execute('UPDATE documents SET pdf_url=? WHERE seq_id=?',(new_url,row['seq_id']))
        self.env.conn.commit()
        replacement=PDF.replace(b'PDF-1.4',b'PDF-1.5')
        def route(url):
            return self.board(url) if url.endswith('robots.txt') else Response(
                200,{'Content-Type':'application/pdf'},replacement)
        fp,t=self.runner(route);result=fp.run(self.env.site_id)
        self.assertEqual(0,result.get('reconciled',0));self.assertIn(new_url,t.urls)
        new=next(r for r in self.files() if r['url']==new_url)
        self.assertEqual(hashlib.sha256(replacement).hexdigest(),new['sha256'])

    def test_disk_failure_during_reconciliation_is_bounded_retry(self):
        row=self.files()[0];path=blob_storage.get_blob_path(row['seq_id'],root=self.blob)
        path.parent.mkdir(parents=True);path.write_bytes(PDF)
        original=Path.read_bytes
        def failure(p):
            if p==path:raise OSError('read failed')
            return original(p)
        fp,_=self.runner(max_attempts=2)
        with mock.patch.object(Path,'read_bytes',failure):
            self.assertEqual(1,fp.run(self.env.site_id)['failed_transient'])
            self.assertEqual(1,fp.run(self.env.site_id)['failed'])
        self.assertEqual('retries_exhausted:storage_error:OSError',self.files()[0]['reason'])

    def test_oversized_response_is_not_stored(self):
        fp,_=self.runner(max_bytes=len(PDF)-1);result=fp.run(self.env.site_id)
        self.assertEqual(3,result['failed']);self.assertFalse(self.blob.exists())
        self.assertEqual({'too_large'},{r['reason'] for r in self.files()})

    def test_html_or_ambiguous_html_content_type_never_saved_as_pdf(self):
        for body,ctype in ((b'<html><form><input type="password"></form></html>','text/html'),
                           (b'<html>error</html>','application/pdf'),(PDF,'text/html')):
            with self.subTest(ctype=ctype,body=body[:15]):
                def handler(url):
                    return self.board(url) if url.endswith('robots.txt') else Response(200,{'Content-Type':ctype},body)
                fp,_=self.runner(handler);fp.run(self.env.site_id)
                self.assertTrue(all(r['status']=='failed' for r in self.files()))
                self.assertFalse(self.blob.exists())
                # New fixture pass: permanent failures are not retried automatically.
                self.env.store.conn.execute("UPDATE enum_file SET status='pending',attempts=0");self.env.store.conn.commit()

    def test_robots_denial_holds_host_and_stops_before_file_request(self):
        def handler(url):
            return Response(200,{'Content-Type':'text/plain'},b'User-agent: *\nDisallow: /files/\n')
        fp,t=self.runner(handler);result=fp.run(self.env.site_id)
        self.assertEqual('robots_disallowed',result['stopped'])
        self.assertEqual(1,len(t.urls));self.assertIsNotNone(self.env.store.host_hold('board.test'))
        self.assertEqual(['unavailable','pending','pending'],[r['status'] for r in self.files()])
        again,t=self.runner();self.assertIsNotNone(again.run(self.env.site_id)['stopped']);self.assertEqual([],t.urls)

    def test_401_403_and_429_stop_on_first_response_and_preserve_fetcher_policy(self):
        for status in (401,403,429):
            with self.subTest(status=status):
                self.env.store.release_host('board.test')
                self.env.store.save_state(self.env.site_id,{})
                # Gates contain a cooldown after 429; each case uses a distinct directory.
                def handler(url):
                    return self.board(url) if url.endswith('robots.txt') else Response(status,{},b'no')
                fp,t=self.runner(handler);fp.fetcher.gate_dir=self.env.root/f'gate-{status}'
                result=fp.run(self.env.site_id)
                self.assertIsNotNone(result['stopped']);self.assertIsNotNone(self.env.store.host_hold('board.test'))
                self.assertEqual(1,len([u for u in t.urls if '/files/' in u]))
                self.assertEqual(3,fp.fetcher.rate_limit_stop)
                self.env.store.conn.execute("UPDATE enum_file SET status='pending',attempts=0");self.env.store.conn.commit()

    def test_gate_gap_measured_after_response_and_shared_by_new_fetcher(self):
        calls=[]
        def handler(url):
            start=self.clock.time();self.clock.sleep(.25);calls.append((start,self.clock.time()))
            return self.board(url)
        fp,t=self.runner(handler,interval=1.5,max_requests=2);fp.run(self.env.site_id)
        fp,t=self.runner(handler,interval=1.5);fp.run(self.env.site_id)
        self.assertTrue(all(b[0]-a[1]>=1.5 for a,b in zip(calls,calls[1:])),calls)

    def test_transient_retry_only_skips_pending_and_terminal_failures(self):
        rows=self.files();st=self.env.store
        st.update_file(self.env.site_id,rows[0]['seq_id'],rows[0]['url'],status='failed_transient',reason='http_503')
        st.update_file(self.env.site_id,rows[2]['seq_id'],rows[2]['url'],status='failed',reason='invalid_pdf_header')
        fp,t=self.runner();self.assertEqual(1,fp.run(self.env.site_id,retry_only=True)['done'])
        self.assertEqual(['done','pending','failed'],[r['status'] for r in self.files()])
        self.assertEqual([rows[0]['url']],[u for u in t.urls if '/files/' in u])

    def test_transient_transport_partial_body_is_retried_without_partial_blob(self):
        import requests
        transport=RequestsTransport(resolver=lambda h:['93.184.216.34'])
        class Broken:
            headers={};status_code=200;url='https://board.test/files/1.pdf'
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def iter_content(self,size):
                yield PDF[:20]
                raise requests.ConnectionError('interrupted stream')
        row=self.files()[0]
        fake=FakeTransport(self.board)
        def request(method,url,*,headers=None):
            if url.endswith('robots.txt'):return self.board(url)
            with mock.patch.object(transport.session,'request',return_value=Broken()):
                return transport.request(method,url,headers=headers)
        fake.request=request
        f=Fetcher(self.env.site_id,fake,self.env.root/'stream-gate',interval=0,retries=1,
                  resolver=lambda h:['93.184.216.34'],holds=self.env.store)
        result=FilePass(self.env.store,f,blob_root=self.blob,doc_conn=self.env.conn,strict_access=True).run(self.env.site_id)
        self.assertEqual(3,result['failed_transient']);self.assertFalse(self.blob.exists())
        fp,_=self.runner();self.assertEqual(3,fp.run(self.env.site_id,retry_only=True)['done'])

    def test_http_200_truncated_pdf_is_bounded_retry_without_partial_blob(self):
        def handler(url):
            return self.board(url) if url.endswith('robots.txt') else Response(
                200,{'Content-Type':'application/pdf'},PDF[:40])
        fp,_=self.runner(handler,max_attempts=2)
        self.assertEqual(3,fp.run(self.env.site_id)['failed_transient'])
        self.assertFalse(self.blob.exists())
        fresh,_=self.runner();self.assertEqual(3,fresh.run(self.env.site_id,retry_only=True)['done'])

    def test_redirect_every_hop_is_governed_and_private_destination_never_requested(self):
        def handler(url):
            if url.endswith('robots.txt'):return Response(404,{},b'')
            if url.endswith('/files/1.pdf'):return Response(302,{'Location':'https://cdn.test/object.pdf'},b'')
            if url.endswith('/files/2.pdf'):return Response(302,{'Location':'http://127.0.0.1/private.pdf'},b'')
            return Response(200,{'Content-Type':'application/pdf'},PDF)
        fp,t=self.runner(handler);result=fp.run(self.env.site_id)
        self.assertEqual(1,result['done']);self.assertEqual(1,result['unavailable'])
        self.assertTrue(result['stopped'].startswith('private_destination'))
        self.assertIn('https://cdn.test/robots.txt',t.urls)
        self.assertIn('https://cdn.test/object.pdf',t.urls)
        self.assertFalse(any('127.0.0.1' in u for u in t.urls))
        self.assertFalse(any('/files/3.pdf' in u for u in t.urls))

    def test_cli_files_only_retry_failed_uses_actual_file_pass(self):
        row=self.files()[0];st=self.env.store
        st.update_file(self.env.site_id,row['seq_id'],row['url'],status='failed_transient',reason='test')
        planfile=self.env.root/'plan.json'
        p=board_plan();p['request_interval_s']=1.5
        planfile.write_text(json.dumps({'sites':{self.env.site_id:p}}))
        def fetcher(plan,state_dir,transport=None,**kw):
            fp,t=self.runner(interval=1.5);self.cli_transport=t;return fp.fetcher
        with mock.patch('crawler.enumeration.run.make_fetcher',side_effect=fetcher),contextlib.redirect_stdout(io.StringIO()):
            rc=main(['--plan',str(planfile),'--state-dir',str(self.env.root),'--db',str(self.env.root/'libertree.db'),
                     '--i-know-this-is-isolated','--files-only','--retry-failed','--blob-root',str(self.blob)])
        self.assertEqual(0,rc)
        self.assertEqual(['done','pending','pending'],[r['status'] for r in self.files()])
        self.assertEqual([row['url']],[u for u in self.cli_transport.urls if '/files/' in u])

    def test_combined_files_phase_retains_document_enumeration(self):
        # Refresh the fake body and then receive queued files through the actual CLI path.
        from crawler.enumeration import run
        plan=self.env.plan(board_plan())
        t=FakeTransport(self.board)
        with mock.patch('crawler.enumeration.run.make_fetcher',side_effect=lambda *a,**k:self.runner()[0].fetcher):
            result=run_site(plan,self.env.store,self.env.conn,self.env.root,files=True,log=lambda m:None)
        self.assertEqual('complete',result['status']);self.assertEqual(3,result['files']['done'])

    def test_cli_exit_code_does_not_hide_file_failures_behind_complete_body(self):
        from crawler.enumeration.run import exit_code
        for handler,expected in ((lambda u: Response(200,{'Content-Type':'application/pdf'},b'<html>error</html>'),1),
                                 (lambda u: Response(403,{},b'denied'),2),
                                 (lambda u: Response(429,{},b'slow'),2)):
            self.env.store.release_host('board.test');self.env.store.save_state(self.env.site_id,{})
            self.env.store.conn.execute("UPDATE enum_file SET status='pending',attempts=0");self.env.store.conn.commit()
            def route(url):return self.board(url) if url.endswith('robots.txt') else handler(url)
            fp,_=self.runner(route);fp.fetcher.gate_dir=self.env.root/f'error-gate-{expected}-{id(handler)}'
            stats=fp.run(self.env.site_id)
            self.assertEqual(expected,exit_code([{'status':'complete','files':stats}]))
