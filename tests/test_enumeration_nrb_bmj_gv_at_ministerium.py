import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "bmj-gv-at-ministerium"
B = "https://www.bmj.gv.at"
LIST = B + "/ministerium/aktuelle-meldungen.html"


def card(slug, title, date):
    return (f'<li class="overview-item"><a class="card-link" href="/ministerium/aktuelle-meldungen/{slug}.html">'
            f'<h2 class="card-title-heading">{title}</h2></a><small class="card-date">{date}</small>'
            f'<p class="card-text">Kurztext {title}</p></li>')


PAGES = {1: card("Justizanstalt-Krems", "Justizanstalt Krems", "11. Mai 2026") + card("Kurz", "Kurz", "10. Mai 2026"),
         2: card("Reform", "Reform des Verjährungsrechts", "3. März 2018")}
BODY = ("Die Justizanstalt Krems steht unter neuer Leitung; die Bundesministerin stellte die neue Leiterin "
        "bei einem Besuch vor und dankte dem bisherigen Team.")


def detail(title, body):
    return (f'<html><body><main id="content"><span class="title">{title}</span>'
            f'<time class="datetime" datetime="2026-05-11">11. Mai 2026</time><p class="abstract">{body}</p></main></body></html>')


DETAILS = {"Justizanstalt-Krems": detail("Justizanstalt Krems unter neuer Leitung", BODY), "Kurz": detail("Kurz", "x"),
           "Reform": detail("Reform des Verjährungsrechts", BODY)}


class BmjSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == LIST:
            p = int(query(url).get("page", "1"))
            body = PAGES.get(min(p, max(PAGES)))  # past the end the site repeats the last page
            return f"<html><body><main><ul>{body}</ul></main></body></html>"
        if url.startswith(B + "/ministerium/aktuelle-meldungen/"):
            return DETAILS.get(url.rsplit("/", 1)[-1].replace(".html", ""))
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
