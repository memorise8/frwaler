"""Recognize observed interstitials without persisting their tokens or text.

This is a bounded classifier, not a way to solve or bypass a challenge. Ordinary
HTTP 405 responses and articles discussing CAPTCHA must remain distinguishable.
"""


def access_challenge(body):
    if not isinstance(body, (str, bytes)) or not body:
        return None
    prefix = body[:128 * 1024]
    markers = ('human verification', 'pardon our interruption')
    if isinstance(prefix, bytes):
        markers = tuple(marker.encode() for marker in markers)
    if not any(marker in prefix.lower() for marker in markers):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(prefix, 'html.parser')
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
