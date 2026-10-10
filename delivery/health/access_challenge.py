"""Recognize observed interstitials without persisting their tokens or text.

This is a bounded classifier, not a way to solve or bypass a challenge. Ordinary
HTTP 405 responses and articles discussing CAPTCHA must remain distinguishable.
"""


def access_challenge(body):
    if not isinstance(body, (str, bytes)) or not body:
        return None
    prefix = body[:128 * 1024]
    cookie_notice = 'this website requires js enabled and cookies'
    human_notice = 'this question is for testing whether you are a human visitor'
    markers = ('human verification', 'pardon our interruption', 'security check',
               'not a bot!',
               cookie_notice, human_notice)
    if isinstance(prefix, bytes):
        markers = tuple(marker.encode() for marker in markers)
    if not any(marker in prefix.lower() for marker in markers):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(prefix, 'html.parser')
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
