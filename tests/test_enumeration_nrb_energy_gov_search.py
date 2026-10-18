import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "energy-gov-search"
B = "https://www.energy.gov"
API = B + "/api/v1/search"
BODY = ("This notice describes the change in control of authorizations to import or export natural gas, "
        "including the docket numbers, the parties and the conditions set out by the Office.")


def row(n):
    return {"title": f'<a href="/hgeo/articles/doc-{n}">Doc {n}</a>', "titleUnion": f"Doc {n}",
            "id": f"entity:node/{900 + n}:en", "date": "May 11, 2026", "offices": "Office of Fossil Energy",
            "articleType": "Notice"}


PAGES = {0: [row(1), row(2)], 1: [row(3)]}


def api(page):
    last = max(PAGES)
    shown = min(page, last)  # the API repeats the last page past the end
    return {"meta": {"pager": {"currentPage": shown}, "totalResultCount": 999}, "rows": PAGES[shown]}


def detail(n, body):
    return (f'<html><body><main><span class="display-date">May 11, 2026</span>'
            f'<div class="field--name-field-text"><div><p>{body}</p></div></div>'
            f'<a href="/sites/default/files/doc-{n}.pdf">PDF</a></main></body></html>')


class EnergySearchSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(API):
            return api(int(query(url).get("page", "0")))
        m = url.rsplit("/doc-", 1)
        if len(m) == 2 and m[1].isdigit():
            n = int(m[1])
            return detail(n, BODY if n != 2 else "short")
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
