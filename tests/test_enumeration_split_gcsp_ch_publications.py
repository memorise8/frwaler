import unittest

from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'gcsp-ch-publications'


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'gcsp-ch-publications'


class StoredIdentityShape(unittest.TestCase):
    """Operational DB (collect-ops sample, 2026-10-18): 176 rows stored post_number as
    '/publications/<slug>' (leading slash). The live site escapes node ids
    ('"currentPath":"node\\/3353"'), so the unchanged node-id regex misses and the
    slug fallback must give exactly that stored form."""

    def test_escaped_node_path_falls_back_to_stored_slug_form(self):
        import tests.collector_split_helpers as base
        crawler, _ = base.load('gcsp-ch-publications')
        slug = '/publications/nuclear-arms-control-needs-new-start'
        listing = ('<html><body><div class="views-element-container"><div class="card-publications-teaser">'
                   f'<a href="{slug}">x</a></div></div></body></html>')
        rows = crawler._enumeration_items(listing, 'https://www.gcsp.ch/publications?page=0')
        self.assertEqual(1, len(rows))
        detail_html = ('<html><head><script>{"path":{"currentPath":"node\\/3353"}}</script></head><body>'
                       '<h1>Nuclear arms control needs New START</h1>'
                       '<div class="publications-date">6 October 2026</div>'
                       '<div class="basic-page">' + ('Long abstract text. ' * 10) + '</div></body></html>')
        detail = crawler._enumeration_detail(detail_html, 'https://www.gcsp.ch' + slug)
        self.assertEqual('', detail['external_id'])
        paper = crawler._enumeration_paper(rows[0], detail)
        self.assertIsNotNone(paper)
        self.assertEqual(slug, paper['external_id'])
        self.assertEqual('https://www.gcsp.ch' + slug, paper['url'])
        self.assertEqual(slug, rows[0]['slug'])
