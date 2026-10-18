import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "csrc-gov-cn-csrc_en"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def row(mid, title):
    return {"manuscriptId": mid, "title": title, "url": f"/csrc_en/c102061/c{mid}/content.shtml",
            "publishedTimeStr": "2026-09-01 10:00:00", "publishedTime": 1788256800000,
            "channelName": "IAC", "contentHtml": "", "channelId": "x", "websiteId": "y", "subTitle": ""}


ROWS = [row("7470217", "The 1st Meeting"), row("7463838", "The 8th Meeting"), row("7463837", "")]
DETAIL = """<html><body><div class="detail-news"><p>The International Advisory Committee of the CSRC held its
annual meeting in Beijing to discuss capital market opening and regulatory cooperation.</p>
<a href="/csrc_en/c102061/c7470217/files/report.pdf">report.pdf</a></div></body></html>"""


class CsrcSplit(unittest.TestCase):
    def serve(self, url):
        if "/searchList/" in url:
            page = query(url).get("page", "1")
            return {"data": {"total": 3, "results": ROWS if page == "1" else []}}
        if "/content.shtml" in url:
            return DETAIL
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
