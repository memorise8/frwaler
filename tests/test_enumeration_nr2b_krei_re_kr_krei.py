import json
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "krei-re-kr-krei"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def item(pst, title, day):
    return f"""<div class="item"><a href="/krei/page/24?cmd=view&amp;pst={pst}&amp;pageIndex=1">
<span class="titl">{title}</span><span class="info_dt">게시일:</span><span class="info">{day}</span>
<span class="info_dt">작성자:</span><span class="info">홍보팀</span></a>
<a class="file_down" href="/download?no=9{pst}">pdf</a></div>"""


PAGE1 = ("<html><body><section class='noti_sect'>" + item("501", "보도 A", "2026.10.01")
         + item("500", "보도 B", "2026.09.30") + "</section></body></html>")
EMPTY = "<html><body><section class='noti_sect'></section></body></html>"


def detail(title, body):
    return f"""<html><body><h3>{title}</h3><span class="date">2026.10.01</span>
<div class="item"><div class="tit">카테고리</div><div class="period">보도자료</div></div>
<div class="item"><div class="tit">작성자</div><div class="period">홍보팀</div></div>
<div class="main_cont"><p>{body}</p></div>
<div class="item"><div class="tit">원문</div><a class="file_down" href="/file/1.pdf">a.pdf</a></div>
</body></html>"""


DETAILS = {"501": detail("보도 A", "충분히 긴 본문입니다. " * 10), "500": detail("보도 B", "짧음")}


class KreiSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        if q.get("cmd") == "view":
            return DETAILS.get(q.get("pst"))
        if "/krei/page/24" in url:
            return PAGE1 if q.get("pageIndex", "1") == "1" else EMPTY
        return None

    def init(self, crawler):
        crawler._curl = lambda url, params=None: self.serve(url + ("?" + urlencode(params) if params else ""))

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
