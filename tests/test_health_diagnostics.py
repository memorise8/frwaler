import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup
from crawler.generic_crawler import GenericCrawler
from delivery.health.diagnostics import Diagnostics, host_only, curl_header_probe
from delivery.health.probe import sample_metadata, run_probe
from delivery.health.results import atomic_json


class HealthDiagnosticsTests(unittest.TestCase):
    def probe(self, *, status=200, body='<li><a href="/1">Document</a></li>', selectors=None, error=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"site_id": "fixture", "site_name": "Fixture", "base_url": "https://fixture.invalid",
                      "list_page": {"url": "https://fixture.invalid/private-path?token=do-not-record",
                                    "selectors": selectors or {"item_container": "li", "item_link": "a"}}}
            (root/'config.json').write_text(json.dumps(config))
            class Fixture(GenericCrawler):
                site_id = 'fixture'
                def __init__(self, db_conn, delay):
                    super().__init__(root/'config.json', db_conn, delay)
            response = requests.Response();response.status_code = status;response._content = body.encode()
            with patch.object(requests.Session, 'request', return_value=response, side_effect=error), patch('time.sleep'):
                return sample_metadata(Fixture, limit=2, delay=0.1, progress=root/'progress.json')

    def test_request_stage_status_and_termination_are_visible_without_content(self):
        result = self.probe()
        diag = result['diagnostics']
        self.assertEqual(diag['listing_stop'], 'pagination_unavailable')
        self.assertEqual(diag['source_failure'], 'configuration')
        calls = [e for e in diag['events'] if e['kind']=='request']
        self.assertEqual(calls[0]['host'], 'fixture.invalid')
        self.assertEqual(calls[0]['http_status'], 200)
        self.assertEqual(calls[0]['origin']['source'], 'crawler/base_crawler.py')
        methods = {e.get('method') for e in diag['events'] if e['kind']=='method'}
        self.assertTrue({'crawl','_fetch_page','_extract_list_items'} <= methods)
        for secret in ('do-not-record', 'private-path', 'Document'):
            self.assertNotIn(secret, json.dumps(result))

    def test_selector_error_has_crawler_location_not_exception_message(self):
        result = self.probe(selectors={'item_container':'#123', 'item_link':'a'})
        self.assertEqual(result['state'], 'error')
        diag = result['diagnostics']
        self.assertEqual(diag['exception_type'], 'SelectorSyntaxError')
        self.assertEqual(diag['source_frames'][-1]['source'], 'crawler/generic_crawler.py')
        self.assertNotIn('#123', json.dumps(result))

    def test_swallowed_block_has_status_and_failed_fetch_method(self):
        result = self.probe(status=403)
        self.assertEqual(result['state'], 'blocked')
        calls = [e for e in result['diagnostics']['events'] if e['kind']=='request']
        self.assertEqual([e['http_status'] for e in calls], [403,403,403])
        self.assertEqual(result['diagnostics']['source_failure'], 'fetch_failed')

    def test_rate_limit_stops_before_crawler_retry(self):
        result = self.probe(status=429)
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['diagnostics']['stop_reason'], 'rate_limited')
        self.assertEqual(result['diagnostics']['event_counts']['request'], 1)
        self.assertEqual(result['diagnostics']['rate_limit']['host'], 'fixture.invalid')

    def test_persistent_gate_prevents_next_probe_from_requesting_limited_host(self):
        import os
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'CRAWLER_PROBE_GATE_DIR':folder}):
            first=self.probe(status=429)
            second=self.probe(status=200)
        self.assertEqual(first['diagnostics']['stop_reason'],'rate_limited')
        self.assertEqual(second['diagnostics']['stop_reason'],'host_cooldown_deferred')
        self.assertEqual(second['diagnostics']['event_counts'].get('request',0),0)

    def test_error_messages_and_urls_are_not_saved(self):
        result = self.probe(error=requests.ConnectionError('secret authentication URL and body'))
        self.assertEqual(result['state'], 'network_error')
        self.assertNotIn('secret authentication', json.dumps(result))
        self.assertEqual(result['diagnostics']['events'][2]['kind'], 'request')

    def test_event_bound_does_not_lose_stop_reason(self):
        diag = Diagnostics(lambda: None, max_events=2)
        for _ in range(5):diag.request('requests','GET','https://fixture.invalid/?secret=abc')
        diag.stop('sample_limit')
        self.assertEqual(len(diag.data['events']), 2)
        self.assertEqual(diag.data['dropped_events'], 3)
        self.assertEqual(diag.data['event_counts']['request'], 5)
        self.assertEqual(diag.data['stop_reason'], 'sample_limit')

    def test_timeout_retains_in_flight_stage(self):
        original = subprocess.Popen
        def child(command, **kwargs):
            progress = command[5]
            code = 'import json,time;from pathlib import Path;Path('+repr(progress)+').write_text(json.dumps({"sample_count":1,"diagnostics":{"events":[{"kind":"request","state":"running"}],"stop_reason":"running"}}));time.sleep(30)'
            return original([sys.executable,'-c',code],**kwargs)
        with patch('delivery.health.probe.subprocess.Popen',side_effect=child):
            result = run_probe({'site_id':'fixture','code_status':'ready','source_hash':'v1'},timeout=1)
        self.assertEqual(result['state'],'timeout')
        self.assertEqual(result['sample_count'],1)
        self.assertEqual(result['diagnostics']['stop_reason'],'hard_timeout')
        self.assertEqual(result['diagnostics']['events'][0]['state'],'running')

    def test_host_redacts_userinfo_path_and_query(self):
        self.assertEqual(host_only('https://secret:password@example.org/private?key=abc'),'example.org')
        self.assertIsNone(host_only('not a URL'))

    def test_preflight_skips_are_explicit_without_launching_child(self):
        with patch('delivery.health.probe.subprocess.Popen') as launch:
            result=run_probe({'site_id':'fixture','code_status':'missing','source_hash':'v1'})
            self.assertEqual(result['diagnostics']['stop_reason'],'unsupported_code_status')
            with patch('delivery.health.probe.missing_credentials',return_value=['SCIENCEON_KEY']):
                result=run_probe({'site_id':'fixture','code_status':'ready','source_hash':'v1'})
            self.assertEqual(result['diagnostics']['missing_credentials'],['SCIENCEON_KEY'])
            launch.assert_not_called()

    def test_crashed_child_preserves_partial_diagnostic(self):
        original=subprocess.Popen
        def child(command, **kwargs):
            code='import json;from pathlib import Path;Path('+repr(command[5])+').write_text(json.dumps({"sample_count":1,"diagnostics":{"events":[]}}));raise SystemExit(2)'
            return original([sys.executable,'-c',code],**kwargs)
        with patch('delivery.health.probe.subprocess.Popen',side_effect=child):
            result=run_probe({'site_id':'fixture','code_status':'ready','source_hash':'v1'})
        self.assertEqual(result['state'],'error');self.assertEqual(result['sample_count'],1)
        self.assertEqual(result['child_returncode'],2)
        self.assertEqual(result['diagnostics']['stop_reason'],'child_exit_without_result')

    def test_curl_keeps_payload_unchanged_and_reads_redirect_and_final_status(self):
        payload = subprocess.CompletedProcess([], 0, stdout=b'original-body', stderr=b'')
        targets = []
        def run(command, **kwargs):
            target = Path(command[2]);targets.append(target)
            target.write_bytes(b'HTTP/1.1 301 Moved\r\nSet-Cookie: secret\r\n\r\nHTTP/2 403\r\n\r\n')
            self.assertEqual(command[3:], ['-sL', 'https://fixture.invalid/?token=secret'])
            return payload
        result, statuses, _ = curl_header_probe(['curl','-sL','https://fixture.invalid/?token=secret'],run,(),{})
        self.assertIs(result,payload)
        self.assertEqual(statuses,[301,403])
        self.assertFalse(targets[0].exists())

    def test_curl_existing_header_contract_is_not_overwritten(self):
        command=['curl','-D','caller-headers','https://fixture.invalid']
        def run(observed, **kwargs):
            self.assertEqual(observed,command)
            return subprocess.CompletedProcess(observed,0)
        _, statuses, _=curl_header_probe(command,run,(),{})
        self.assertEqual(statuses,[])

    def test_curl_429_preserves_retry_after_delay_without_header_contents(self):
        import time
        def run(command, **kwargs):
            Path(command[2]).write_bytes(b'HTTP/2 429\r\nRetry-After: 3600\r\nSet-Cookie: secret\r\n\r\n')
            return subprocess.CompletedProcess(command,0)
        before=time.time()
        _, statuses, retry_at=curl_header_probe(['curl','https://fixture.invalid'],run,(),{})
        self.assertEqual(statuses,[429]);self.assertGreaterEqual(retry_at,before+3600)

    def test_detailed_report_does_not_break_health_reader_or_expose_extra_fields(self):
        from delivery.health.results import read_result, site_path, utc_now
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);site={'site_id':'fixture','source_hash':'v1'}
            report={**site,'state':'healthy','checked_at':utc_now(),'diagnostics':{'events':['x'*300]*100}}
            atomic_json(site_path(root,'fixture'),report)
            result=read_result(root,site)
            self.assertEqual(result['state'],'healthy')
            self.assertNotIn('diagnostics',result)
            report['diagnostics']['events']=['x'*(256*1024)]
            atomic_json(site_path(root,'fixture'),report)
            self.assertEqual(read_result(root,site)['state'],'report_error')
