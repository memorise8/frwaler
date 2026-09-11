"""GSI publications after the SharePoint-to-WordPress site migration.

The complete current listing is shared with gsi-ie. Keep the historical ID
for existing jobs while using the same selectors and bounded generic traversal.
"""
from pathlib import Path

from crawler.generic_crawler import GenericCrawler


class GSIPublicationsCrawler(GenericCrawler):
    site_id = "gsi-ie-en-ie"
    site_name = "Geological Survey Ireland — Publications"
    base_url = "https://www.gsi.ie"

    def __init__(self, db_conn, delay=None):
        config = Path(__file__).resolve().parents[1] / "configs" / "gsi-ie.json"
        super().__init__(config, db_conn, delay)
