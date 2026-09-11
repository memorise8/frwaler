"""Collect direct PDF documents linked by DOI's official guidance portals.

The former /guidance URL is a test page. The department now links to bureau
portals. Follow those explicit links one level, without storing the portals
themselves as documents. Nested lists and bureau pagination are not traversed.
"""
import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from crawler.base_crawler import BaseCrawler


class DoiGuidanceCrawler(BaseCrawler):
    site_id = "doi-gov-guidance"
    site_name = "DOI — Guidance Documents"
    base_url = "https://www.doi.gov"
    START_URL = base_url + "/document-library/departmental-guidance-documents-portals"

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0
        response = self._request(self.START_URL)
        if response is None:
            return 0
        soup = BeautifulSoup(response.content, "html.parser")
        portals = []
        for link in soup.select('main a[href]'):
            label = link.get_text(' ', strip=True)
            url = urljoin(response.url, link['href'])
            host = urlsplit(url).hostname or ''
            if (label.endswith('Guidance Documents') or label == 'ONRR Policy Materials') and host.endswith('.gov'):
                if url not in portals:
                    portals.append(url)
        # Department-hosted material first; then the linked bureau portals.
        portals.sort(key=lambda url: urlsplit(url).hostname != 'www.doi.gov')
        saved = 0
        seen = set()
        for portal in portals:
            response = self._request(portal)
            if response is None:
                continue
            soup = BeautifulSoup(response.content, "html.parser")
            content = soup.select_one('main') or soup.select_one('article')
            if content is None:
                continue
            for link in content.select('a[href]'):
                url = urljoin(response.url, link['href'])
                if not urlsplit(url).path.lower().endswith('.pdf') or url in seen:
                    continue
                title = link.get_text(' ', strip=True)
                if not title:
                    continue
                seen.add(url)
                self._save_paper({
                    'site_id': self.site_id, 'external_id': url, 'title': title,
                    'url': url, 'pdf_url': url, 'category': 'Guidance',
                    'metadata': json.dumps({'guidance_portal': portal}),
                })
                saved += 1
                if limit is not None and saved >= limit:
                    return saved
        print(f'[{self.site_id}] Saved {saved} documents from {len(portals)} portals')
        return saved
