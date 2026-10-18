import json
import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query, as_text

SITE = "efk-admin-ch-fr"
REST = "https://www.efk.admin.ch/fr/wp-json/wp/v2"
B = "https://www.efk.admin.ch/fr/"
TEXT = ("Le Contrôle fédéral des finances publie son rapport annuel 2025 consacré à une responsabilité "
        "équitable entre les générations et aux finances fédérales.")


def post(i, slug, title):
    return {"id": i, "link": B + slug + "/", "slug": slug, "date": "2026-03-01T08:00:00",
            "title": {"rendered": title}, "excerpt": {"rendered": f"<p>{TEXT}</p>"},
            "content": {"rendered": f"<p>{TEXT}</p>"},
            "_embedded": {"wp:term": [[{"taxonomy": "category", "name": "Communiqué de presse"}]]}}


POSTS = [post(44531, "rapport-annuel-2025", "Rapport annuel 2025"), post(42585, "programme-annuel-2026", "Programme annuel 2026")]
PAST = {"code": "rest_post_invalid_page_number", "message": "x", "data": {"status": 400}}


def page_html(title):
    return f"<html><body><main><h1>{title}</h1><div class='entry-content'><p>{TEXT}</p></div></main></body></html>"


class EfkSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(REST + "/posts?"):
            return POSTS if query(url).get("page", "1") == "1" else None
        for p in POSTS:
            if url.startswith(f"{REST}/posts/{p['id']}"):
                return p
            if url == p["link"]:
                return page_html(p["title"]["rendered"])
        return None

    def init(self, c):
        def get_text(url, **k):
            body = self.serve(url)
            if body is None and url.startswith(REST + "/posts?"):
                return json.dumps(PAST), url, {"total_pages": "1"}
            return (None if body is None else as_text(body)), url, {"total_pages": "1"}
        c._curl_get_text = get_text
        c._discover_category_id = lambda: 213

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review", "complete_with_mismatch"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
