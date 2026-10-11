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

JS_HUMAN = b'''<html><head><script src="/TSPD/private-challenge-token"></script></head>
<body>This question is for testing whether you are a human visitor and to prevent automated spam submission.
<audio id="captcha_audio"></audio><b>What code is in the image?</b>
<input id="ans" name="answer" type="text"><button>submit</button></body></html>'''

MYRA = b'''<html><head><title>Security Check</title>
<script src="https://www.myrasecurity.com/x-myracloud-00000000000000000000000000000000/"></script>
</head><body><h1>Security</h1><h2>Security Check</h2>
<p>Our systems have detected unusual traffic from your computer network.</p>
<a href="/myracloud-captcha/?q=private-challenge-token">here</a>
<p>You must confirm that you are human.</p></body></html>'''


ANUBIS = b'''<html><head><title>Making sure you're not a bot!</title>
<script id="anubis_challenge" type="application/json">{"challenge":"private-challenge-token"}</script>
</head><body><main><h1 id="title">Making sure you're not a bot!</h1>
<script id="anubis-main" type="module" src="/.within.website/x/cmd/anubis/static/js/main.mjs?cacheBuster=v1.27.0"></script>
</main></body></html>'''

ANUBIS_CUSTOM_TITLE = '''<html><head><title>Repositorio Académico - Universidad de Chile</title>
<script id="anubis_challenge" type="application/json">{"challenge":"private-challenge-token"}</script>
</head><body><h1 id="title">Verificando acceso al Repositorio Académico</h1>
<p id="status">Loading...</p>
<p id="anubis-script-error" style="display:none;">Anubis could not load its JavaScript. The server may be overloaded. Please reload the page to try again.</p>
<script id="anubis-main" type="module" src="/.within.website/x/cmd/anubis/static/js/main.mjs?cacheBuster=v1.26.2+dirty"></script>
<div id="progress" role="progressbar" aria-labelledby="status"><div class="bar-inner"></div></div>
</body></html>'''.encode('utf-8')


TURNSTILE = b'''<html><head><title>Security Check</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body><main><h1>Security check</h1>
<p id="security-status">Checking your browser. You will continue automatically.</p>
<form id="turnstile-form" method="post"><input type="hidden" name="csrf" value="private-challenge-token">
<div class="cf-turnstile" data-sitekey="public-site-key" data-callback="done"></div></form>
<noscript>JavaScript is required to complete this security check.</noscript>
</main></body></html>'''


TSPD_JAVASCRIPT = b'''<html><head><script type="text/javascript">var token="private-challenge-token";</script>
<script type="text/javascript" src="/TSPD/0123456789abcdef?type=8"></script>
<noscript>Please enable JavaScript to view the page content.<br/>Your support ID is: 123456789.</noscript>
</head><body></body></html>'''


AWS_JAVASCRIPT = b'''<html><head><title></title>
<script src="https://fixture.region.token.awswaf.com/a/b/challenge.js"></script>
</head><body><div id="challenge-container"></div><noscript><h1>JavaScript is disabled</h1>
In order to continue, we need to verify that you're not a robot.
This requires JavaScript. Enable JavaScript and then reload the page.</noscript></body></html>'''


IMPERVA_EMPTY = b'''<html><head><META NAME="robots" CONTENT="noindex,nofollow">
<script src="/_Incapsula_Resource?SWJIYLWA=private-challenge-token"></script>
<body></body></html>'''


CHALLENGE_VALIDATION = b'''<html><head><title>Challenge Validation</title>
<script>function cp_clge_done(){location.reload(true);}</script>
<script src="/challenge/resource/private-challenge-token"></script></head><body>
<div class="sec-container"><div id="sec-text-container">
<iframe id="sec-text-if" class="crypto" src="/challenge/text"></iframe></div>
<div id="sec-if-container"><iframe id="sec-cpt-if" class="crypto" src="/challenge/control"></iframe></div>
</div><div><input type="hidden" name="verify-url" value="private-challenge-token"></div></body></html>'''


HAL_REFUSAL = b'''<html><head><title>Search - HAL Open Archive</title></head><body>
<nav><a class="logo-HAL" href="//hal.science"><img src="/assets/img/hal-logo-header.png"></a></nav>
<main class="section-corps" id="skip-link"><h1>Sorry, we are unable to process your request</h1>
<div class="flash-message"><p>It seems that your request has been identified as coming from a robot.
If this is not the case, please accept our apologies for the inconvenience.
You can try your search again by <strong>refreshing the page</strong> or by <strong>going back</strong> in your browser.
</p></div></main></body></html>'''


RADWARE = b'''<html><head><title>Radware Page</title>
<meta property="og:url" content="https://radware.com/">
<script>var token="private-challenge-token";window["SSJSConnectorObj"]={};
var resource="/fixture-provider/stormcaster.js";var validation="validate.perfdrive.com";</script>
</head><body><div class="container"><div class="content">
<p class="verify-message">Verifying your browser before proceeding...</p></div></div>
<div class="incident-id"><p><span>Incident ID:</span> private-incident-id</p></div></body></html>'''


class ChallengeClassification(unittest.TestCase):
    def test_radware_requires_loader_notice_provider_and_active_connector(self):
        for body in (RADWARE, RADWARE.decode()):
            self.assertEqual(access_challenge(body), 'radware_browser_verification')
        for body in (
            RADWARE.replace(b'<title>Radware Page', b'<title>Article about Radware Page'),
            RADWARE.replace(b'https://radware.com/', b'https://example.org/'),
            RADWARE.replace(b'Verifying your browser before proceeding...', b'No publications found'),
            RADWARE.replace(b'validate.perfdrive.com', b'example.org'),
            RADWARE.replace(b'"/fixture-provider/stormcaster.js"', b'"https://example.org/fixture-provider/stormcaster.js"'),
            RADWARE.replace(b'<script>', b'<script type="application/json">'),
            RADWARE.replace(b'<script>', b'<pre>').replace(b'</script>', b'</pre>'),
            RADWARE.replace(b'</body>', b'<article>Research on Radware</article></body>'),
            RADWARE.replace(b'</body>', b'<main>Ordinary results</main></body>'),
            RADWARE.replace(b'</body>', b'<p>Ordinary content</p></body>'),
            b'x' * (128 * 1024) + RADWARE,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_radware_200_holds_host_without_exposing_challenge_data(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/press'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True, body=RADWARE,
                        headers={'content-type': 'text/html'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'radware_browser_verification')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))
        self.assertNotIn('private-incident-id', json.dumps(first))

    def test_radware_200_stops_both_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=RADWARE)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_hal_robot_refusal_requires_the_complete_error_page(self):
        for body in (HAL_REFUSAL, HAL_REFUSAL.decode()):
            self.assertEqual(access_challenge(body), 'hal_robot_access_refusal')
        for body in (
            HAL_REFUSAL.replace(b'Search - HAL Open Archive', b'Research about robots'),
            HAL_REFUSAL.replace(b'logo-HAL', b'ordinary-logo'),
            HAL_REFUSAL.replace(b'identified as coming from a robot', b'completed successfully'),
            HAL_REFUSAL.replace(b'<main', b'<article').replace(b'</main>', b'</article>'),
            HAL_REFUSAL.replace(b'</main>', b'<p>Actual search results</p></main>'),
            b'x' * (128 * 1024) + HAL_REFUSAL,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_hal_robot_refusal_200_holds_host(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/search'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True,
                        body=HAL_REFUSAL, headers={'content-type': 'text/html'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'hal_robot_access_refusal')
        self.assertEqual(second['state'], 'host_access_held')

    def test_hal_robot_refusal_200_stops_both_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=HAL_REFUSAL)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_challenge_validation_requires_empty_active_controls(self):
        for body in (CHALLENGE_VALIDATION, CHALLENGE_VALIDATION.decode()):
            self.assertEqual(access_challenge(body), 'javascript_challenge_validation')
        for body in (
            CHALLENGE_VALIDATION.replace(b'Challenge Validation', b'Publication list'),
            CHALLENGE_VALIDATION.replace(b'sec-cpt-if', b'ordinary-frame'),
            CHALLENGE_VALIDATION.replace(b'verify-url', b'ordinary-input'),
            CHALLENGE_VALIDATION.replace(b'cp_clge_done', b'ordinary_callback'),
            CHALLENGE_VALIDATION.replace(b'<script', b'<script type="text/plain"'),
            CHALLENGE_VALIDATION.replace(b'src="/challenge', b'src="https://example.org/challenge'),
            CHALLENGE_VALIDATION.replace(b'</body>', b'<main>Research</main></body>'),
            CHALLENGE_VALIDATION.replace(b'</body>', b'<p>No results</p></body>'),
            b'x' * (128 * 1024) + CHALLENGE_VALIDATION,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_challenge_validation_200_holds_host_without_exposing_data(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/press'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True,
                        body=CHALLENGE_VALIDATION, headers={'content-type': 'text/html'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'javascript_challenge_validation')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_challenge_validation_200_stops_both_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=CHALLENGE_VALIDATION)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_empty_imperva_shell_requires_robots_and_active_local_script(self):
        for body in (IMPERVA_EMPTY, IMPERVA_EMPTY.decode()):
            self.assertEqual(access_challenge(body), 'imperva_empty_script_interstitial')
        for body in (
            IMPERVA_EMPTY.replace(b'noindex,nofollow', b'index,follow'),
            IMPERVA_EMPTY.replace(b'NAME="robots"', b'NAME="description"'),
            IMPERVA_EMPTY.replace(b'/_Incapsula_Resource?', b'/ordinary.js?'),
            IMPERVA_EMPTY.replace(b'SWJIYLWA=', b'example='),
            IMPERVA_EMPTY.replace(b'src="/', b'src="https://example.org/'),
            IMPERVA_EMPTY.replace(b'<script ', b'<script type="text/plain" '),
            IMPERVA_EMPTY.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            IMPERVA_EMPTY.replace(b'</body>', b'<article>Publication</article></body>'),
            IMPERVA_EMPTY.replace(b'</body>', b'<p>No search results</p></body>'),
            IMPERVA_EMPTY.replace(b'</body>', b'<main></main></body>'),
            b'x' * (128 * 1024) + IMPERVA_EMPTY,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_empty_imperva_200_holds_host_without_revealing_resource_token(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/publications'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True, body=IMPERVA_EMPTY, headers={'content-type': 'text/html'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'imperva_empty_script_interstitial')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_empty_imperva_200_stops_both_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=IMPERVA_EMPTY)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_aws_js_requires_noscript_interstitial_and_active_provider_resource(self):
        for body in (AWS_JAVASCRIPT, AWS_JAVASCRIPT.decode()):
            self.assertEqual(access_challenge(body), 'aws_waf_javascript_verification')
        for body in (
            AWS_JAVASCRIPT.replace(b'<noscript>', b'<p>').replace(b'</noscript>', b'</p>'),
            AWS_JAVASCRIPT.replace(b'challenge-container', b'research-example'),
            AWS_JAVASCRIPT.replace(b'JavaScript is disabled', b'JavaScript research'),
            AWS_JAVASCRIPT.replace(b'token.awswaf.com/', b'token.awswaf.com.example.org/'),
            AWS_JAVASCRIPT.replace(b'https://fixture.region.token.awswaf.com/', b'https://[invalid/'),
            AWS_JAVASCRIPT.replace(b'/challenge.js', b'/challenge.js.fake'),
            AWS_JAVASCRIPT.replace(b'<script ', b'<script type="text/plain" '),
            AWS_JAVASCRIPT.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            AWS_JAVASCRIPT.replace(b'</body>', b'<article>Research</article></body>'),
            AWS_JAVASCRIPT.replace(b'</body>', b'<p>Real document</p></body>'),
            b'x' * (128 * 1024) + AWS_JAVASCRIPT,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_aws_js_202_holds_host_without_repeating(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/search'}}
        gate = HostGate(0)
        response = dict(status=202, curl_exit=0, body_complete=True, body=gzip.compress(AWS_JAVASCRIPT),
                        headers={'content-type': 'text/html', 'content-encoding': 'gzip'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'aws_waf_javascript_verification')
        self.assertEqual(second['state'], 'host_access_held')

    def test_aws_js_202_stops_both_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=202, body=AWS_JAVASCRIPT)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_tspd_js_notice_requires_empty_interstitial_and_active_local_resource(self):
        for body in (TSPD_JAVASCRIPT, TSPD_JAVASCRIPT.decode()):
            self.assertEqual(access_challenge(body), 'tspd_javascript_verification')
        for body in (
            TSPD_JAVASCRIPT.replace(b'<noscript>', b'<p>').replace(b'</noscript>', b'</p>'),
            TSPD_JAVASCRIPT.replace(b'Your support ID is: 123456789.', b'Ordinary fallback.'),
            TSPD_JAVASCRIPT.replace(b'/TSPD/', b'/scripts/'),
            TSPD_JAVASCRIPT.replace(b'src="/TSPD/', b'src="https://example.org/TSPD/'),
            TSPD_JAVASCRIPT.replace(b'type="text/javascript"', b'type="text/plain"'),
            TSPD_JAVASCRIPT.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            TSPD_JAVASCRIPT.replace(b'</body>', b'<article>Research</article></body>'),
            TSPD_JAVASCRIPT.replace(b'</body>', b'<main>Catalogue</main></body>'),
            TSPD_JAVASCRIPT.replace(b'</body>', b'<p>Real content</p></body>'),
            b'x' * (128 * 1024) + TSPD_JAVASCRIPT,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_tspd_js_200_holds_host_without_exposing_support_id(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/search'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True, body=gzip.compress(TSPD_JAVASCRIPT),
                        headers={'content-type': 'text/html', 'content-encoding': 'gzip'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after hold'))
        self.assertEqual(first['challenge'], 'tspd_javascript_verification')
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('123456789', json.dumps(first))
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_tspd_js_200_stops_both_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=TSPD_JAVASCRIPT)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_turnstile_requires_security_interstitial_and_active_provider_control(self):
        self.assertEqual(access_challenge(TURNSTILE), 'turnstile_browser_verification')
        self.assertEqual(access_challenge(TURNSTILE.decode()), 'turnstile_browser_verification')
        for body in (
            TURNSTILE.replace(b'<title>Security Check', b'<title>Contact form'),
            TURNSTILE.replace(b'<h1>Security check', b'<h1>Contact us'),
            TURNSTILE.replace(b'Checking your browser.', b'Please enter your question.'),
            TURNSTILE.replace(b'JavaScript is required', b'JavaScript is optional'),
            TURNSTILE.replace(b'method="post"', b'method="get"'),
            TURNSTILE.replace(b'class="cf-turnstile"', b'class="example"'),
            TURNSTILE.replace(b'data-sitekey=', b'data-example='),
            TURNSTILE.replace(b'challenges.cloudflare.com/', b'challenges.cloudflare.com.example.org/'),
            TURNSTILE.replace(b'https://challenges.cloudflare.com/', b'https://[invalid/'),
            TURNSTILE.replace(b'/api.js"', b'/api.js.fake"'),
            TURNSTILE.replace(b'<script ', b'<script type="text/plain" '),
            TURNSTILE.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            TURNSTILE.replace(b'</body>', b'<article>Turnstile research</article></body>'),
            b'x' * (128 * 1024) + TURNSTILE,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_turnstile_200_holds_host_without_repeating_or_exposing_token(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/search'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True, body=gzip.compress(TURNSTILE),
                        headers={'content-type': 'text/html', 'content-encoding': 'gzip'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'turnstile_browser_verification')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_turnstile_200_stops_both_supported_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=TURNSTILE)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_anubis_requires_interstitial_and_active_local_module(self):
        self.assertEqual(access_challenge(ANUBIS), 'anubis_proof_of_work_verification')
        self.assertEqual(access_challenge(ANUBIS.decode()), 'anubis_proof_of_work_verification')
        # CERN escapes the apostrophe in both the title and the heading.
        for apostrophe in (b'&#39;', b'&#x27;', b'&apos;'):
            self.assertEqual(access_challenge(ANUBIS.replace(b"'", apostrophe)),
                             'anubis_proof_of_work_verification')
        for body in (
            ANUBIS.replace(b'<title>Making sure', b'<title>Article about making sure'),
            ANUBIS.replace(b'<h1 id="title">Making sure', b'<h1 id="title">Article about making sure'),
            ANUBIS.replace(b'id="anubis_challenge"', b'id="article-example"'),
            ANUBIS.replace(b'type="application/json"', b'type="ignore"'),
            ANUBIS.replace(b'type="module"', b'type="ignore"'),
            ANUBIS.replace(b'src="/.within.website/', b'src="https://example.org/.within.website/'),
            ANUBIS.replace(b'main.mjs?', b'main.mjs.fake?'),
            ANUBIS.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            ANUBIS.replace(b'</body>', b'<article>Research about Anubis</article></body>'),
            b'x' * (128 * 1024) + ANUBIS,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_anubis_200_holds_host_without_repeating_or_exposing_challenge(self):
        site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/collection'}}
        gate = HostGate(0)
        response = dict(status=200,curl_exit=0,body_complete=True,body=gzip.compress(ANUBIS),
                        headers={'content-type':'text/html','content-encoding':'gzip'})
        first = observe(site,gate,fetch=lambda *a,**kw:response)
        second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'anubis_proof_of_work_verification')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_anubis_200_stops_both_supported_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=ANUBIS)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_anubis_custom_title_requires_actual_loader_controls(self):
        for body in (ANUBIS_CUSTOM_TITLE, ANUBIS_CUSTOM_TITLE.decode()):
            self.assertEqual(access_challenge(body), 'anubis_proof_of_work_verification')
        for body in (
            ANUBIS_CUSTOM_TITLE.replace(b'id="title"', b'id="article-heading"'),
            ANUBIS_CUSTOM_TITLE.replace(b'id="anubis_challenge"', b'id="example"'),
            ANUBIS_CUSTOM_TITLE.replace(b'type="application/json"', b'type="ignore"'),
            ANUBIS_CUSTOM_TITLE.replace(b'type="module"', b'type="ignore"'),
            ANUBIS_CUSTOM_TITLE.replace(b'src="/.within.website/', b'src="https://example.org/.within.website/'),
            ANUBIS_CUSTOM_TITLE.replace(b'Loading...', b'No records found'),
            ANUBIS_CUSTOM_TITLE.replace(b'Anubis could not load its JavaScript.', b'Ordinary server maintenance.'),
            ANUBIS_CUSTOM_TITLE.replace(b'role="progressbar"', b'role="presentation"'),
            ANUBIS_CUSTOM_TITLE.replace(b'aria-labelledby="status"', b'aria-labelledby="article"'),
            ANUBIS_CUSTOM_TITLE.replace(b'</body>', b'<article>Research on Anubis</article></body>'),
            ANUBIS_CUSTOM_TITLE.replace(b'<script ', b'<pre ').replace(b'</script>', b'</pre>'),
            b'x' * (128 * 1024) + ANUBIS_CUSTOM_TITLE,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_anubis_custom_title_holds_host_without_exposing_challenge(self):
        site = {'site_id': 'fixture', 'selected_entry': {'url': 'https://example.org/search'}}
        gate = HostGate(0)
        response = dict(status=200, curl_exit=0, body_complete=True, body=ANUBIS_CUSTOM_TITLE,
                        headers={'content-type': 'text/html; charset=utf-8'})
        first = observe(site, gate, fetch=lambda *a, **kw: response)
        second = observe(site, gate, fetch=lambda *a, **kw: self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_anubis_custom_title_stops_both_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=ANUBIS_CUSTOM_TITLE)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_myra_requires_notice_provider_script_and_active_control(self):
        self.assertEqual(access_challenge(MYRA), 'myra_human_verification')
        self.assertEqual(access_challenge(MYRA.decode()), 'myra_human_verification')
        for body in (
            MYRA.replace(b'<title>Security Check', b'<title>Security research'),
            MYRA.replace(b'You must confirm that you are human.', b'Ordinary server maintenance.'),
            MYRA.replace(b'myracloud-captcha/', b'publications/'),
            MYRA.replace(b'www.myrasecurity.com/', b'www.myrasecurity.com.example.org/'),
            MYRA.replace(b'https://www.myrasecurity.com/', b'https://[invalid/'),
            MYRA.replace(b'<script ', b'<a ').replace(b'</script>', b'</a>'),
            MYRA.replace(b'</body>', b'<article>Security research</article></body>'),
            MYRA.replace(b'</body>', b'<main>Publications</main></body>'),
            b'x' * (128 * 1024) + MYRA,
        ):
            with self.subTest(body=body[:80]):
                self.assertIsNone(access_challenge(body))

    def test_myra_503_holds_host_without_repeating_or_exposing_control(self):
        site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/list'}}
        gate = HostGate(0)
        response = dict(status=503,curl_exit=0,body_complete=True,body=MYRA,
                        headers={'content-type':'text/html'})
        first = observe(site,gate,fetch=lambda *a,**kw:response)
        second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(first['challenge'], 'myra_human_verification')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_myra_503_stops_supported_probe_transports_before_retry(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=503, body=MYRA)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

    def test_javascript_human_interstitial_requires_observed_active_controls(self):
        self.assertEqual(access_challenge(JS_HUMAN), 'javascript_human_verification')
        self.assertEqual(access_challenge(JS_HUMAN.decode()), 'javascript_human_verification')
        for body in (
            JS_HUMAN.replace(b'/TSPD/', b'/ordinary-script/'),
            JS_HUMAN.replace(b'<script ', b'<a ').replace(b'</script>', b'</a>'),
            JS_HUMAN.replace(b'name="answer"', b'name="search"'),
            JS_HUMAN.replace(b'captcha_audio', b'article_audio'),
            JS_HUMAN.replace(b'What code is in the image?', b'Normal publication'),
            JS_HUMAN.replace(b'</body>', b'<article>Security research</article></body>'),
        ):
            self.assertIsNone(access_challenge(body))

    def test_javascript_human_200_entry_holds_host_without_retry(self):
        site = {'site_id':'fixture', 'selected_entry':{'url':'https://example.org/annual'}}
        gate = HostGate(0)
        response = dict(status=200,curl_exit=0,body_complete=True,body=JS_HUMAN,
                        headers={'content-type':'text/html'})
        first = observe(site,gate,fetch=lambda *a,**kw:response)
        second = observe(site,gate,fetch=lambda *a,**kw:self.fail('fetch after host hold'))
        self.assertEqual(first['state'], 'source_access_challenge')
        self.assertEqual(second['state'], 'host_access_held')
        self.assertNotIn('private-challenge-token', json.dumps(first))

    def test_javascript_human_200_stops_both_supported_probe_transports(self):
        for transport in ('requests', 'curl'):
            result = self.probe(transport, status=200, body=JS_HUMAN)
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['diagnostics']['stop_reason'], 'source_access_challenge')
            self.assertEqual(result['sample_count'], 0)

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
