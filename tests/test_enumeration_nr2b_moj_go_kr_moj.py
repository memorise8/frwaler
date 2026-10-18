import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "moj-go-kr-moj"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def row(seq, num, title, day):
    return f"""<tr><td class="_artclTdNum">{num}</td><td class="_artclTdTitle">
<a class="artclLinkView" href="/bbs/moj/182/{seq}/artclView.do"><strong>{title}</strong></a></td>
<td class="_artclTdRdate">{day}</td><td class="progress">대변인실</td></tr>"""


PAGE1 = ("<html><body><table class='artclTable'><tbody>" + row("611377", "6102", "보도 A", "2026.10.16")
         + row("611351", "6101", "보도 B", "2026.10.15") + "</tbody></table>"
         "<p class='_pageState'><span class='_curPage'>1</span><span class='_totPage'>1</span></p></body></html>")
EMPTY = "<html><body><table class='artclTable'><tbody><tr><td>등록된 글이 없습니다.</td></tr></tbody></table></body></html>"


def detail(title, body):
    return f"""<html><body><h2 class="artclViewTitle">{title}</h2>
<div class="dataInfo"><div class="infor"><dl><dt>작성일</dt><dd>2026.10.16</dd></dl>
<dl><dt>담당부서</dt><dd>인권국</dd></dl></div></div>
<div class="artclView"><p>{body}</p></div>
<dl><dd class="fileList"><a href="/bbs/moj/182/77/download.do">보도자료.pdf</a></dd></dl></body></html>"""


DETAILS = {"611377": detail("보도 A", "법무부 보도자료 본문입니다. " * 6), "611351": detail("보도 B", "짧음")}


class MojSplit(unittest.TestCase):
    def serve(self, url):
        if "/artclView.do" in url:
            return next((v for k, v in DETAILS.items() if f"/{k}/artclView" in url), None)
        if "/moj/221/subview.do" in url:
            return PAGE1 if query(url).get("page", "1") == "1" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
