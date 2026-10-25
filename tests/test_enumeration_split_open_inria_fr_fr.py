"""S-open-g5: inria-fr-fr enumeration split (crawl() unchanged)."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'inria-fr-fr'
BASE = 'https://www.inria.fr'
LONG = 'Inria annonce un nouveau partenariat de recherche en intelligence artificielle avec plusieurs universites.'


def art(about, title, tag='IA'):
    return (f'<article class="news" about="{about}"><h3><span>{title}</span></h3>'
            f'<div class="field--name-field-main-tag"><div class="field__item">{tag}</div></div></article>')


LIST = ('<html><body><main>' + art('/fr/partenariat-ia', 'Partenariat IA')
        + art('/en/english-news', 'English') + art('/index.php/fr/prix-recherche', 'Prix recherche', 'Prix')
        + art('/fr/breve', 'Breve') + '</main></body></html>')
EMPTY = '<html><body><main><p>Aucun contenu</p></main></body></html>'


def detail(title, desc, body, date, pdf=''):
    link = f'<a href="{pdf}">pdf</a>' if pdf else ''
    return (f'<html><body><main><h1><span>{title}</span></h1><time datetime="{date}T09:00:00Z">x</time>'
            f'<div class="field--name-field-type-actualite">Communiqué de presse</div>'
            f'<div class="field--name-field-description">{desc}</div><div class="field--name-body">{body}</div>'
            f'<div class="field--name-field-tags"><div class="field__item">Recherche</div></div>{link}</main></body></html>')


DETAILS = {
    BASE + '/fr/partenariat-ia': detail('Partenariat IA', 'Accord signe.', LONG, '2026-09-01', '/sites/default/files/cp.pdf'),
    BASE + '/index.php/fr/prix-recherche': detail('Prix recherche', '', LONG, '2026-08-02'),
    BASE + '/fr/breve': detail('Breve', 'Court.', '', '2026-07-03'),
}


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        expected, _ = native_papers(SITE, lambda url, *a, **k: DETAILS.get(url, LIST),
                                    method_patches=['_curl_get'])
        parsed, papers, calls = split_run(SITE, LIST, DETAILS, page=0)
        self.assertEqual(['partenariat-ia', 'prix-recherche', 'breve'], [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        self.assertEqual('partenariat-ia', papers[0]['external_id'])
        self.assertEqual('2026-09-01', papers[0]['published_date'])
        self.assertEqual(BASE + '/sites/default/files/cp.pdf', papers[0]['pdf_url'])

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, EMPTY, page=0)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('<html><body><div id="anubis_challenge"></div></body></html>')


if __name__ == '__main__':
    unittest.main()
