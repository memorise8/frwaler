import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "kostat-go-kr-boardes"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
PAGE1 = """<html><body><div class="board_list_01"><ul>
<li><a href="#" onclick="goView('447170')">보고서 A</a></li><li><a href="#" onclick="goView('447169')">보고서 B</a></li>
</ul></div></body></html>"""
EMPTY = "<html><body><div class='board_list_01'><ul><li>게시물이 없습니다.</li></ul></div></body></html>"


def detail(title, body):
    return f"""<html><body><script>sendSNS('facebook','{title}')</script>
<ul><li><strong>담당부서</strong><span>통계정책과</span></li><li><strong>등록일</strong><span>2026-10-16</span></li></ul>
<div class="bv_b"><div class="board_content"><p>{body}</p></div></div></body></html>"""


DETAILS = {"447170": detail("보고서 A", "국가데이터처 통계 보고서 본문입니다. " * 6), "447169": detail("보고서 B", "짧음")}


class KostatSplit(unittest.TestCase):
    def setUp(self):
        self.bare_list_calls = 0

    def serve(self, url):
        q = query(url)
        if q.get("act") == "view":
            return DETAILS.get(q.get("list_no"))
        if "board.es" in url:
            if "mid" in q:  # the plan's requests (bare first page = page 1)
                return PAGE1 if q.get("nPage", "1") == "1" else EMPTY
            # crawl() passes the list query as a params dict the fake transport cannot see:
            # its first list request is page 1, the next one page 2.
            self.bare_list_calls += 1
            return PAGE1 if self.bare_list_calls == 1 else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.bare_list_calls = 0
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
