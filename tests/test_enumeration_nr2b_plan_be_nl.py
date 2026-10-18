import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "plan-be-nl"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
P = "https://www.plan.be/nl/publicaties/"
INDEX = """<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://www.plan.be/sitemap.xml?page=1</loc></sitemap>
<sitemap><loc>https://www.plan.be/sitemap.xml?page=2</loc></sitemap>
<sitemap><loc>https://www.plan.be/sitemap.xml?page=3</loc></sitemap></sitemapindex>"""


def urlset(*locs):
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in locs) + "</urlset>")


SITEMAPS = {"1": urlset(P + "pub-a", "https://www.plan.be/fr/publications/pub-a"), "2": urlset(P + "pub-b"),
            "3": urlset("https://www.plan.be/nl/nieuws/x")}


def detail(title, body):
    return f"""<html><head><meta property="og:title" content="{title}"><title>{title}</title>
<meta name="description" content="{body}"></head><body><h1>{title}</h1>
<div class="field--name-body"><p>{body}</p></div><time datetime="2026-10-01T00:00:00Z">1 oktober 2026</time>
<a href="/sites/default/files/a.pdf">PDF</a></body></html>"""


DETAILS = {"pub-a": detail("Pub A", "Het Federaal Planbureau publiceert de economische vooruitzichten. " * 3),
           "pub-b": detail("Pub B", "kort")}


class PlanBeSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith("https://www.plan.be/sitemap.xml"):
            page = query(url).get("page")
            return INDEX if page is None else SITEMAPS.get(page)
        if url.startswith(P):
            return DETAILS.get(url.rsplit("/", 1)[-1])
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve, plan=PLAN, types=lambda u: "application/xml" if "sitemap" in u else None)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
