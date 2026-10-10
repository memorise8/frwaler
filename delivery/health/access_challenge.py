"""Recognize observed interstitials without persisting their tokens or text.

This is a bounded classifier, not a way to solve or bypass a challenge. Ordinary
HTTP 405 responses and articles discussing CAPTCHA must remain distinguishable.
"""


def access_challenge(body):
    if not isinstance(body, (str, bytes)) or not body:
        return None
    prefix = body[:128 * 1024]
    cookie_notice = 'this website requires js enabled and cookies'
    markers = ('human verification', 'pardon our interruption', cookie_notice)
    if isinstance(prefix, bytes):
        markers = tuple(marker.encode() for marker in markers)
    if not any(marker in prefix.lower() for marker in markers):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(prefix, 'html.parser')
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
