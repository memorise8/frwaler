import gzip
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import requests

from crawler.base_crawler import BaseCrawler
from delivery.health.access_challenge import access_challenge
from delivery.health.probe import sample_metadata
from delivery.health.source_observer import HostGate, observe

CHALLENGE = b'''<html><head><title>Human Verification</title>
<script>window.gokuProps={"key":"private-challenge-token"}</script>
<script src="https://example.captcha.awswaf.com/challenge/captcha.js"></script>
</head><body><div id="captcha-container"></div></body></html>'''

IMPERVA = b'''<html><head><title>Pardon Our Interruption</title>
<style>.wait { background: white url("/_Incapsula_Resource?private-challenge-token"); }</style>
</head><body>Something about your browser made us think you were a bot.</body></html>'''

COOKIE_REDIRECT = b'''<html><head></head><body>
<script>window.location.href='/redirect_TESTCHALLENGE123====/publications?f=230';</script>
<noscript>This website requires JS enabled and cookies</noscript></body></html>'''


class ChallengeClassification(unittest.TestCase):
    def test_cookie_interstitial_requires_notice_empty_body_and_active_redirect(self):
        self.assertEqual(access_challenge(COOKIE_REDIRECT), 'javascript_cookie_verification')
        self.assertEqual(access_challenge(COOKIE_REDIRECT.decode()), 'javascript_cookie_verification')
        for body in (
            COOKIE_REDIRECT.replace(b'<script>', b'<pre>').replace(b'</script>', b'</pre>'),
            COOKIE_REDIRECT.replace(b'/redirect_TESTCHALLENGE123====/', b'/ordinary-page/'),
            COOKIE_REDIRECT.replace(b'<noscript>', b'<p>').replace(b'</noscript>', b'</p>'),
            COOKIE_REDIRECT.replace(b'</body>', b'<article>Publication text</article></body>'),
            COOKIE_REDIRECT.replace(b'</body>', b'<p>Real publication</p></body>'),
            COOKIE_REDIRECT.replace(b'window.location.href=', b'// window.location.href='),
            b'x' * (128 * 1024) + COOKIE_REDIRECT,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_cookie_200_entry_holds_host_and_does_not_expose_redirect(self):
        site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/publications'}}
        gate = HostGate(0)
        response = dict(status=200,curl_exit=0,body_complete=True,body=COOKIE_REDIRECT,
                        headers={'content-type':'text/html'})
        first = observe(site,gate,fetch=lambda *a,**kw:response)
        second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('TESTCHALLENGE123', json.dumps(first))

    def test_cookie_200_stops_requests_and_curl_before_redirect_or_retry(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=COOKIE_REDIRECT)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)
            self.assertNotIn('TESTCHALLENGE123', json.dumps(result))

    def test_imperva_requires_title_bot_notice_and_active_resource(self):
        self.assertEqual(access_challenge(IMPERVA), 'imperva_browser_verification')
        self.assertEqual(access_challenge(IMPERVA.decode()), 'imperva_browser_verification')
        for body in (IMPERVA.replace(b'Pardon Our Interruption', b'Security report'),
                     IMPERVA.replace(b'made us think you were a bot', b'normal document'),
                     IMPERVA.replace(b'<style>', b'<pre>').replace(b'</style>', b'</pre>'),
                     IMPERVA.replace(b'/_Incapsula_Resource?', b'/ordinary-image?'),
                     b'x' * (128 * 1024) + IMPERVA):
            self.assertIsNone(access_challenge(body))

    def test_imperva_200_compressed_entry_holds_host_without_retry(self):
        site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/api'}}
        gate = HostGate(0)
        response = dict(status=200,curl_exit=0,body_complete=True,body=gzip.compress(IMPERVA),
                        headers={'content-type':'text/html','content-encoding':'gzip'})
        first = observe(site,gate,fetch=lambda *a,**kw:response)
        second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
        self.assertEqual(first['state'],'source_access_challenge')
        self.assertEqual(second['state'],'host_access_held')
        self.assertNotIn('private-challenge-token',json.dumps(first))

    def test_article_about_captcha_and_ordinary_405_are_not_challenges(self):
        for body in (b'<h1>Method Not Allowed</h1>',
                     b'<title>Security report</title><p>Human Verification captcha.awswaf.com</p>',
                     b'<title>Human Verification</title><script src="https://captcha.awswaf.com.example.org/a"></script>',
                     b'{"title":"Human Verification","url":"https://x.captcha.awswaf.com/a"}'):
            self.assertIsNone(access_challenge(body))
        self.assertEqual(access_challenge(CHALLENGE), 'aws_waf_human_verification')

    def test_entry_405_and_200_gzip_challenge_hold_host_without_second_fetch(self):
        for status, compress in ((405, False), (200, False), (405, True)):
            with self.subTest(status=status, compress=compress):
                gate = HostGate(0)
                site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/list'}}
                body = gzip.compress(CHALLENGE) if compress else CHALLENGE
                response = dict(status=status,curl_exit=0,body_complete=True,body=body,
                                headers={'content-type':'text/html','content-encoding':'gzip' if compress else ''})
                with patch('builtins.print'):
                    first = observe(site,gate,fetch=lambda *a,**kw:response)
                    second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
                self.assertEqual(first['state'],'source_access_challenge')
                self.assertEqual(second['state'],'host_access_held')
                self.assertNotIn('body_observation',first)
                self.assertNotIn('private-challenge-token',json.dumps(first))

    def test_regular_405_is_http_error_and_does_not_hold_host(self):
        site = {'site_id':'fixture','selected_entry':{'url':'https://example.org/list'}}
        gate = HostGate(0)
        response = dict(status=405,curl_exit=0,body_complete=True,body=b'<h1>Method not allowed</h1>',headers={})
        for _ in range(2):
            self.assertEqual(observe(site,gate,fetch=lambda *a,**kw:response)['state'],'http_error')

    def probe(self, transport, *, status=405, partial=False, body=CHALLENGE):
        class Fixture(BaseCrawler):
            site_id='fixture'
            site_name='Fixture'
            base_url='https://example.org'
            def crawl(self, limit=None):
                if partial:self._save_paper({'title':'Document','url':'https://example.org/1'})
                for _ in range(3):
                    try:
                        if transport=='curl':subprocess.run(['curl','-sL','https://example.org/list'],capture_output=True)
                        else:self._session.get('https://example.org/list')
                    except Exception:
                        pass
                raise AssertionError('collector must not continue after the challenge')
        response=requests.Response();response.status_code=status;response._content=body
        def run(command,*a,**kw):
            Path(command[2]).write_bytes(f'HTTP/2 {status}\r\n\r\n'.encode())
            return subprocess.CompletedProcess(command,0,stdout=body,stderr=b'')
        with tempfile.TemporaryDirectory() as folder,patch.object(requests.Session,'request',return_value=response) as request,patch('subprocess.run',side_effect=run) as command:
            result=sample_metadata(Fixture,limit=3,delay=0,progress=Path(folder)/'progress.json')
            self.assertEqual(command.call_count if transport=='curl' else request.call_count,1)
        self.assertNotIn('private-challenge-token',json.dumps(result))
        return result

    def test_requests_and_curl_stop_before_retries_on_200_or_405(self):
        for transport in ('requests','curl'):
            for status in (200,405):
                with self.subTest(transport=transport,status=status):
                    result=self.probe(transport,status=status)
                    self.assertEqual(result['state'],'blocked')
                    self.assertEqual(result['diagnostics']['stop_reason'],'source_access_challenge')
                    self.assertEqual(result['sample_count'],0)

    def test_imperva_200_stops_requests_and_curl_after_one_request(self):
        for transport in ('requests','curl'):
            result=self.probe(transport,status=200,body=IMPERVA)
            self.assertEqual(result['state'],'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'],'source_access_challenge')
            self.assertEqual(result['sample_count'],0)

    def test_prior_sample_stays_partial_when_later_page_is_challenged(self):
        result=self.probe('requests',partial=True)
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['sample_count'],1)
        self.assertEqual(result['diagnostics']['stop_reason'],'source_access_challenge')
