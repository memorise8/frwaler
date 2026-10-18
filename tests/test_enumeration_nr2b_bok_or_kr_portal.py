import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "bok-or-kr-portal"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def li(ntt, title, day):
    return f"""<li class="bbsRowCls"><span class="t1">보도자료</span><div class="set">
<a href="/portal/bbs/B0000502/view.do?nttId={ntt}&amp;menuNo=201265&amp;pageIndex=1">{title}</a></div>
<span class="depart"><span class="sr-only">부서</span>공보관</span><span class="date"><span class="sr-only">등록일</span>2026.10.16</span></li>"""


PAGE1 = ("<html><body><div class='bd-line'><ul>" + li("11065634", "보도 A", "2026.10.16")
         + li("11065650", "보도 B", "2026.10.15") + "</ul></div></body></html>")
EMPTY = "<html><body><div class='bd-line'><ul><div class='i-no-data pad'><p>조회 내역이 없습니다.</p></div></ul></div></body></html>"


def detail(title, body):
    pad = "<!-- " + "x" * 700 + " -->"
    return f"""<html><body>{pad}<div class="bd-view"><h2 class="subject">{title}</h2>
<dl class="dataInfo"><dt>등록일</dt><dd>2026.10.16</dd></dl>
<div class="dbdata"><p>{body}</p></div>
<dl class="down"><ul><li><a href="/fileSrc/portal/a.pdf">보도자료.pdf</a></li></ul></dl></div></body></html>"""


DETAILS = {"11065634": detail("보도 A", "한국은행 보도자료 본문입니다. " * 6), "11065650": detail("보도 B", "짧음")}


class BokSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        if "/view.do" in url:
            return DETAILS.get(q.get("nttId"))
        if "listCont.do" in url:
            return PAGE1 if q.get("pageIndex", "1") == "1" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
