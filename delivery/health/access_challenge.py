"""Recognize observed interstitials without persisting their tokens or text.

This is a bounded classifier, not a way to solve or bypass a challenge. Ordinary
HTTP 405 responses and articles discussing CAPTCHA must remain distinguishable.
"""


def access_challenge(body):
    if not isinstance(body, (str, bytes)) or not body:
        return None
    prefix = body[:128 * 1024]
    cookie_notice = 'this website requires js enabled and cookies'
    js_page_notice = 'please enable javascript to view the page content.'
    aws_js_notice = "in order to continue, we need to verify that you're not a robot."
    human_notice = 'this question is for testing whether you are a human visitor'
    markers = ('human verification', 'pardon our interruption', 'security check',
               'not a bot!',
               cookie_notice, human_notice, js_page_notice, aws_js_notice,
               '/_incapsula_resource?')
    if isinstance(prefix, bytes):
        markers = tuple(marker.encode() for marker in markers)
    if not any(marker in prefix.lower() for marker in markers):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(prefix, 'html.parser')
    if not soup.get_text(' ', strip=True) and not soup.find(['article', 'main', 'h1', 'form']):
        # Both delivered environment.govt.nz scopes returned this empty HTTP
        # 200 shell: robots exclusion plus a local Incapsula script. It is
        # not an empty catalogue. Ignore inert examples and normal content.
        from urllib.parse import parse_qs, urlsplit
        robots = soup.find('meta', attrs={'name': lambda value: value and value.casefold() == 'robots'})
        directives = {v.strip().casefold() for v in (robots.get('content', '') if robots else '').split(',')}
        if directives == {'noindex', 'nofollow'}:
            for script in soup.find_all('script', src=True):
                if script.get('type') not in (None, 'text/javascript', 'application/javascript', 'module'):
                    continue
                try:
                    resource = urlsplit(script['src'])
                except ValueError:
                    continue
                if (not resource.scheme and not resource.netloc
                        and resource.path == '/_Incapsula_Resource'
                        and parse_qs(resource.query).get('SWJIYLWA')):
                    return 'imperva_empty_script_interstitial'
    if not soup.find(['article', 'main']):
        # Earth-Prints returned HTTP 202 with an otherwise empty AWS WAF
        # challenge container and this noscript fallback (no page title).
        import re
        from urllib.parse import urlsplit
        notices = soup.find_all('noscript')
        notice = notices[0] if len(notices) == 1 else None
        heading = notice.find('h1') if notice else None
        expected = ('javascript is disabled ' + aws_js_notice
                    + ' this requires javascript. enable javascript and then reload the page.')
        text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).casefold()
        if (notice and heading and heading.get_text(' ', strip=True).casefold() == 'javascript is disabled'
                and text == expected and soup.select_one('div#challenge-container')):
            for script in soup.find_all('script', src=True):
                if script.get('type') not in (None, 'text/javascript', 'application/javascript', 'module'):
                    continue
                try:
                    resource = urlsplit(script['src'])
                except ValueError:
                    continue
                if (resource.scheme == 'https'
                        and (resource.hostname or '').endswith('.token.awswaf.com')
                        and resource.path.endswith('/challenge.js')):
                    return 'aws_waf_javascript_verification'
    if not soup.find(['article', 'main', 'h1']):
        # Observed HTTP 200 catalogue responses from data.gov.be contain only
        # this noscript support notice and an active local TSPD resource.
        # Do not mistake an article, quoted example or ordinary JS fallback
        # for the interstitial, and never return the support ID or script data.
        import re
        notices = soup.find_all('noscript')
        text = soup.get_text(' ', strip=True).casefold()
        if (len(notices) == 1
                and notices[0].get_text(' ', strip=True).casefold() == text
                and re.fullmatch(re.escape(js_page_notice)
                                 + r'\s+your support id is:\s*\d+\.', text)):
            for script in soup.find_all('script', src=True):
                if (script.get('type') in (None, 'text/javascript', 'application/javascript', 'module')
                        and re.fullmatch(r'/TSPD/[0-9a-f]+(?:\?[^\s]*)?', script['src'])):
                    return 'tspd_javascript_verification'
    if not soup.find(['article', 'main', 'h1']):
        text = soup.get_text(' ', strip=True).casefold()
        if (human_notice in text and 'what code is in the image?' in text
                and soup.select_one('input#ans[name="answer"][type="text"]')
                and soup.select_one('audio#captcha_audio')
                and any(script['src'].startswith('/TSPD/')
                        for script in soup.find_all('script', src=True))):
            return 'javascript_human_verification'
    if soup.body and not soup.find(['article', 'main', 'h1']):
        # Observed on all three delivered Andra publication filters. Require
        # the otherwise empty interstitial and an active local redirect, so
        # ordinary articles quoting its notice remain ordinary content.
        notices = soup.body.find_all('noscript')
        notice = (len(notices) == 1 and
                  notices[0].get_text(' ', strip=True).casefold() == cookie_notice)
        if notice and soup.body.get_text(' ', strip=True).casefold() == cookie_notice:
            import re
            for script in soup.body.find_all('script', src=False):
                if re.fullmatch(
                    r"\s*window\.location\.href\s*=\s*(['\"])/redirect_[A-Z0-9=]+/[^'\"\r\n]*\1\s*;?\s*",
                    script.get_text(),
                ):
                    return 'javascript_cookie_verification'
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if title == "making sure you're not a bot!" and not soup.find('article'):
        # Observed HTTP 200 on both delivered CERN collection routes. Require
        # the interstitial heading, challenge data and an active local module;
        # an article discussing Anubis is not evidence of an access challenge.
        heading = soup.find('h1', id='title')
        challenge = soup.find('script', id='anubis_challenge', type='application/json')
        module = soup.find('script', id='anubis-main', type='module', src=True)
        if (heading and heading.get_text(' ', strip=True).casefold() == title
                and challenge and module
                and module['src'].split('?', 1)[0]
                == '/.within.website/x/cmd/anubis/static/js/main.mjs'):
            return 'anubis_proof_of_work_verification'
    if title == 'security check' and not soup.find('article'):
        # Observed on the four delivered Drugs and Alcohol search scopes.
        # A regular page with a Turnstile form is not itself an interstitial.
        heading = soup.find('h1')
        status = soup.find('p', id='security-status')
        form = soup.find('form', id='turnstile-form')
        notice = soup.find('noscript')
        control = form.select_one('.cf-turnstile[data-sitekey]') if form else None
        if (heading and heading.get_text(' ', strip=True).casefold() == title
                and status and status.get_text(' ', strip=True).casefold()
                == 'checking your browser. you will continue automatically.'
                and notice and notice.get_text(' ', strip=True).casefold()
                == 'javascript is required to complete this security check.'
                and form and str(form.get('method', '')).casefold() == 'post'
                and control):
            from urllib.parse import urlsplit
            for script in soup.find_all('script', src=True):
                if script.get('type') not in (None, 'text/javascript', 'application/javascript', 'module'):
                    continue
                try:
                    resource = urlsplit(script['src'])
                except ValueError:
                    continue
                if (resource.scheme == 'https'
                        and resource.hostname == 'challenges.cloudflare.com'
                        and resource.path == '/turnstile/v0/api.js'):
                    return 'turnstile_browser_verification'
    if title == 'security check' and not soup.find(['article', 'main']):
        # The observed BMF HTTP 503 is a Myra human-verification page, not
        # an ordinary temporary server failure. Require active controls and
        # the provider resource so articles mentioning Myra are unaffected.
        import re
        text = soup.get_text(' ', strip=True).casefold()
        notice = ('our systems have detected unusual traffic from your computer network.' in text
                  and 'you must confirm that you are human.' in text)
        control = any(link['href'].split('?', 1)[0] == '/myracloud-captcha/'
                      for link in soup.find_all('a', href=True))
        resource = any(
            re.fullmatch(r'https://(?:www\.)?myrasecurity\.com/x-myracloud-[0-9a-f]{32}/',
                         script['src'])
            for script in soup.find_all('script', src=True)
        )
        if notice and control and resource:
            return 'myra_human_verification'
    if title == 'pardon our interruption':
        # The observed CKAN API returned HTTP 200 with this Imperva page.
        # Require both its bot notice and an active style/script resource;
        # an article quoting the title or a resource URL is not sufficient.
        notice = 'made us think you were a bot' in soup.get_text(' ', strip=True).casefold()
        resource = any('/_incapsula_resource?' in str(tag).casefold()
                       for tag in soup.find_all(['style', 'script']))
        return 'imperva_browser_verification' if notice and resource else None
    if title != 'human verification':
        return None
    from urllib.parse import urlsplit
    for script in soup.find_all('script', src=True):
        host = (urlsplit(script['src']).hostname or '').casefold()
        if host.endswith('.captcha.awswaf.com'):
            return 'aws_waf_human_verification'
    return None
