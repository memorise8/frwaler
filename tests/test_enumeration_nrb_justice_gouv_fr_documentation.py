import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "justice-gouv-fr-documentation"
B = "https://www.justice.gouv.fr"
START = B + "/documentation/ressources?categories%5B%5D=399&categories%5B%5D=431&items_per_page=100"


def card(slug, title, desc):
    return (f'<article class="fr-card"><a class="fr-card__link" href="/documentation/ressources/{slug}">{title}</a>'
            f'<p class="fr-card__desc">{desc}</p><p class="fr-card__detail">Rapport - Publié le 3 mars 2025</p></article>')


LONG = "Rapport de la mission d'évaluation relative aux conseils de juridiction, remis au garde des Sceaux en 2025."
PAGE0 = ("<html><body><main>" + card("rapport-a", "Rapport A", LONG) + card("rapport-b", "Rapport B", "court")
         + "</main></body></html>")
EMPTY = "<html><body><main><p>Aucun résultat</p></main></body></html>"


def detail(node, title, body):
    return (f'<html><head><link rel="shortlink" href="/node/{node}"></head><body><main><h1>{title}</h1>'
            f'<div class="mj-content-header__date">Publié le 3 mars 2025</div><p>{body}</p></main>'
            f'<script type="application/json" data-drupal-selector="drupal-settings-json">'
            f'{{"path": {{"currentPath": "node/{node}"}}}}</script></body></html>')


PAGES = {B + "/documentation/ressources/rapport-a": detail(20055, "Rapport A", LONG),
         B + "/documentation/ressources/rapport-b": detail(20056, "Rapport B", "")}


class JusticeSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(B + "/documentation/ressources?"):
            return PAGE0 if query(url).get("page", "0") == "0" else EMPTY
        return PAGES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertGreaterEqual(len(expected), 1)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
