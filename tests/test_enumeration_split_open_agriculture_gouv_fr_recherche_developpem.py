"""S-open-g5: agriculture-gouv-fr-recherche-developpem enumeration split (crawl() unchanged)."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'agriculture-gouv-fr-recherche-developpem'
BASE = 'https://agriculture.gouv.fr'
LONG = 'Le ministere annonce un programme de recherche sur les sols agricoles et la biodiversite.'


def card(href, title, date, kind='Communiqué de presse'):
    return (f'<div class="fr-card"><div class="fr-card__body"><h3><a class="fr-card__link" href="{href}">{title}</a></h3>'
            f'<p class="fr-card__desc">Resume {title}</p>'
            f'<p class="fr-card__detail"><span class="fr-card__date">{date}</span>{kind}</p></div></div>')


LIST = ('<html><body><main>'
        + card('/programme-sols-vivants', 'Programme sols vivants', '13 mai 2026')
        + card('/espace-presse?page=2', 'Filtre', '1 mai 2026')
        + '<div class="fr-card__body"><a class="fr-card__link" href="/sans-date-article">Sans date</a></div>'
        + card('/appel-a-projets-innovation', 'Appel a projets innovation', '2 février 2026', 'Actualité')
        + card('/note-courte-sans-texte', 'Note courte', '1 janvier 2026')
        + '</main></body></html>')
EMPTY = '<html><body><main><p>Aucun résultat</p></main></body></html>'


def detail(title, paras, date, pdf=None):
    links = f'<a href="{pdf}">PDF</a>' if pdf else ''
    body = ''.join(f'<p>{p}</p>' for p in paras)
    return (f'<html><head><meta name="description" content="Description {title} pour la presse agricole nationale."></head>'
            f'<body><main><article><h1>{title}</h1><span class="fr-card__date">{date}</span>{body}'
            f'<a class="fr-tag" href="/mots-cles/sol">Sol</a>{links}</article></main></body></html>')


DETAILS = {
    BASE + '/programme-sols-vivants': detail('Programme sols vivants', [LONG, LONG + ' Deuxieme.'], '14 mai 2026',
                                              '/sites/default/files/documents/sols.pdf'),
    BASE + '/appel-a-projets-innovation': detail('Appel a projets innovation', [LONG], '2 février 2026'),
    BASE + '/note-courte-sans-texte': '<html><body><main><article><h1>Note</h1><p>court</p></article></main></body></html>',
}


def route(url, *a, **k):
    if url in DETAILS:
        return DETAILS[url]
    return LIST if url.endswith('pg=1') else EMPTY


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        expected, _ = native_papers(SITE, route, module_patches=['_curl_get'])
        parsed, papers, calls = split_run(SITE, LIST, DETAILS, page=1)
        self.assertEqual(['programme-sols-vivants', 'appel-a-projets-innovation', 'note-courte-sans-texte'],
                         [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        p = papers[0]
        self.assertEqual('programme-sols-vivants', p['external_id'])
        self.assertEqual('2026-05-14', p['published_date'])
        self.assertEqual('Programme sols vivants', p['title'])
        self.assertEqual(BASE + '/programme-sols-vivants', p['url'])

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, EMPTY, page=9)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('<html><body>maintenance</body></html>')
        with self.assertRaises(PermanentFailure):
            c._enumeration_items(b'bytes')


if __name__ == '__main__':
    unittest.main()
