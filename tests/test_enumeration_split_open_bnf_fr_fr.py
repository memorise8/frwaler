"""S-open-g5: bnf-fr-fr enumeration split (crawl() unchanged)."""
import json
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'bnf-fr-fr'
LONG = '<p>La BnF presente une exposition consacree aux manuscrits medievaux et aux collections patrimoniales.</p>'


def node(uuid, nid, title, text, media=None):
    rel = {'field_media1': {'data': {'type': 'media--documents', 'id': media} if media else None}}
    return {'type': 'node--espace_presse', 'id': uuid,
            'attributes': {'drupal_internal__nid': nid, 'title': title, 'created': '2026-03-02T10:00:00+01:00',
                           'changed': '2026-03-03T10:00:00+01:00', 'field_index_solr': {'processed': text},
                           'field_texte_telechargement': 'Communiqué', 'moderation_state': 'published'},
            'relationships': rel}


PAGE = {'data': [node('u-1', 101, 'Exposition manuscrits', LONG, 'm-1'),
                 node('u-2', 102, '', LONG),
                 node('u-3', 103, 'Court', '<p>court</p>'),
                 node('u-4', 104, 'Nouvelle acquisition', LONG)],
        'included': [{'type': 'media--documents', 'id': 'm-1',
                      'relationships': {'field_document': {'data': {'type': 'file--file', 'id': 'f-1'}}}},
                     {'type': 'file--file', 'id': 'f-1',
                      'attributes': {'uri': {'url': '/sites/default/files/cp.pdf'}, 'filename': 'cp.pdf'}}],
        'links': {'next': {'href': 'https://www.bnf.fr/fr/jsonapi/node/espace_presse?page%5Boffset%5D=50'}}}
EMPTY = {'data': [], 'links': {}}


def route(url, *a, **k):
    body = PAGE if 'offset' not in url else EMPTY
    return json.dumps(body).encode()


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        expected, _ = native_papers(SITE, route, method_patches=['_curl_get'])
        parsed, papers, calls = split_run(SITE, json.dumps(PAGE), {}, content_type='application/vnd.api+json', page=0)
        self.assertEqual(['u-1', 'u-2', 'u-3', 'u-4'], [i.key for i in parsed.items])
        self.assertEqual([], calls)
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        self.assertEqual('u-1', papers[0]['external_id'])
        self.assertEqual('101', papers[0]['post_number'])
        self.assertEqual('2026-03-02', papers[0]['published_date'])
        self.assertEqual('https://www.bnf.fr/sites/default/files/cp.pdf', papers[0]['pdf_url'])

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, json.dumps(EMPTY), content_type='application/vnd.api+json', page=3)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items({'errors': [{'status': '403'}]})


if __name__ == '__main__':
    unittest.main()
