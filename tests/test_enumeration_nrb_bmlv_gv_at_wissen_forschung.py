import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "bmlv-gv-at-wissen-forschung"
BASE = "https://www.bmlv.gv.at/wissen-forschung/publikationen"
LIST = BASE + "/doktyp.php?id=14"
PAD = "x" * 200


def pub(pid, title, beitrag=None, extra=""):
    link = f'<a href="beitrag.php?id={beitrag}">Beitrag</a>' if beitrag else ""
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<meta property="books:release_date" content="2024-05-01"></head><body><div id="content">'
            f'<h2>{title}</h2><p>Untertitel der Publikation {pid} mit genug Text fuer eine Beschreibung.</p>'
            f'<div class="floatbox">Dokumenttyp: Bericht Erscheinungsdatum: 05.2024 Herausgeber: BMLV '
            f'Seiten: 120 Autor: <a href="person.php?id=1">Max Muster</a></div>{link}'
            f'<a href="/pdf_pool/publikationen/p{pid}.pdf">PDF</a>{extra}</div><!-- {PAD} --></body></html>').encode("latin-1")


BODIES = {
    LIST: (f'<html><body><div id="content"><a href="publikation.php?id=411">A</a>'
           f'<a href="publikation.php?id=400">B</a><a href="publikation.php?id=411">A</a>'
           f'<!-- <a href="publikation.php?id=999">hidden</a> --> {PAD}</div></body></html>').encode(),
    BASE + "/publikation.php?id=411": pub(411, "Sicherheitspolitische Jahresvorschau", beitrag=77),
    BASE + "/publikation.php?id=400": pub(400, "Strategie und Sicherheit"),
    BASE + "/beitrag.php?id=77": (f'<html><body><div id="content"><div class="floatbox">'
                                  f'<a href="thema.php?id=1">Sicherheit</a><a href="region.php?id=2">Europa</a></div>'
                                  f'<a href="schlagwortsuche.php?sw=NATO">NATO</a> {PAD}</div></body></html>').encode(),
}


class BmlvSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?page=")[0].split("&page=")[0] == LIST:
            return BODIES[LIST]
        return BODIES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        self.assertIn("Themen: Sicherheit", expected[0]["abstract"])  # from the beitrag page
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


class BmlvSplitChecks(unittest.TestCase):
    """SplitChecks-style checks (reviewer, 2026-10-19): the added methods give the very
    paper dict crawl() hands to _save_paper; skip rule and original code are unchanged."""

    ORIGINAL_SHA256 = {
        "crawl": "5a5223eddfeb634c1322b3d9cbe986f2744a6fd49a231ed3c349ae845f7fa7f5",
        "_fetch_publication": "893255544c4ed71b915a0a98fac2f99e5bd3b1afe72a38ae08dc58bc58b1a74f",
    }

    def enumerate(self, bodies):
        import re
        from crawler.enumeration.http import Response
        from tests.collector_split_helpers import load
        crawler, _ = load(SITE)
        out = []
        for item in crawler._enumeration_items(bodies[LIST]):
            first = Response(200, {}, bodies[item["url"]], item["url"])
            resp = first
            m = re.search(rb'href="(beitrag\.php\?id=\d+)"', bodies[item["url"]])
            if m:
                url = BASE + "/" + m.group(1).decode()
                resp = Response(200, {}, bodies[url], url)
            paper = crawler._enumeration_paper(item, first, resp)
            if paper is not None:
                out.append(paper)
        return out

    def native_papers(self, bodies):
        def serve(url):
            if url.split("?page=")[0] == LIST:
                return bodies[LIST]
            return bodies.get(url)
        return native(SITE, serve, module_funcs=["_curl_get"])

    def test_full_paper_equals_crawl_save_argument(self):
        expected = self.native_papers(BODIES)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, self.enumerate(BODIES))

    def test_short_abstract_skip_is_preserved(self):
        bodies = dict(BODIES)
        bodies[BASE + "/publikation.php?id=400"] = (
            '<html><body><div id="content"><h2>K</h2></div><!-- ' + "y" * 300 + " --></body></html>").encode()
        expected = self.native_papers(bodies)
        self.assertEqual(["411"], [p["post_number"] for p in expected])
        self.assertEqual(expected, self.enumerate(bodies))

    def test_original_methods_are_unchanged(self):
        import ast
        import hashlib
        from tests.collector_split_helpers import ROOT
        code = (ROOT / "crawler/sites/custom" / (SITE + ".py")).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.ORIGINAL_SHA256.items():
            self.assertEqual(digest, actual[name], name)


if __name__ == "__main__":
    unittest.main()
