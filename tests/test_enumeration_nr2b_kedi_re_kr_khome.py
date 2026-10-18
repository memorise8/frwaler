import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "kedi-re-kr-khome"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
# crawl() POSTs list/detail form data the fake transport cannot see, so one list row
# (the detail endpoint answers the same page for any plNum0).
PAGE1 = """<html><body><table class="colTbl"><tbody><tr><td>1</td><td>연구보고서</td><td>2026.10.01</td>
<td>홍길동</td><td><a href="#" onclick="selectPubFormFn('16587');return false;">인공지능 연구</a></td></tr>
</tbody></table></body></html>"""
EMPTY = """<html><body><table class="colTbl"><tbody><tr><td class="nolist">자료가 없습니다</td></tr>
</tbody></table></body></html>"""
DETAIL = """<html><body><h4 class="boardTit">인공지능 연구</h4>
<div class="reportSummary"><div class="reportCont">""" + "한국교육개발원 연구보고서 요약입니다. " * 8 + """</div></div>
<ul class="reportInfo"><li><span class="tit">보고서번호 :</span>RR 2026-01</li></ul>
<a href="#" onclick="downloadAction('a.pdf','b.pdf','A00/000301','P')">PDF</a></body></html>"""


class KediSplit(unittest.TestCase):
    def serve(self, url):
        if "selectPubForm.do" in url:
            return DETAIL
        if "listPubForm.do" in url:
            return PAGE1 if query(url).get("currentPage", "1") == "1" else EMPTY
        return None

    def serve_bytes(self, url):  # crawl()'s _curl returns bytes
        body = self.serve(url)
        return None if body is None else body.encode("utf-8")

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve_bytes, module_funcs=["_curl"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve_bytes, module_funcs=["_curl"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
