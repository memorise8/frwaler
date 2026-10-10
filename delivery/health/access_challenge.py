"""Recognize observed interstitials without persisting their tokens or text.

This is a bounded classifier, not a way to solve or bypass a challenge. Ordinary
HTTP 405 responses and articles discussing CAPTCHA must remain distinguishable.
"""


def access_challenge(body):
    if not isinstance(body, (str, bytes)) or not body:
        return None
    prefix = body[:128 * 1024]
    marker = b'human verification' if isinstance(prefix, bytes) else 'human verification'
    if marker not in prefix.lower():
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(prefix, 'html.parser')
    title = soup.title.get_text(' ', strip=True).casefold() if soup.title else ''
    if title != 'human verification':
        return None
    from urllib.parse import urlsplit
    for script in soup.find_all('script', src=True):
        host = (urlsplit(script['src']).hostname or '').casefold()
        if host.endswith('.captcha.awswaf.com'):
            return 'aws_waf_human_verification'
    return None
