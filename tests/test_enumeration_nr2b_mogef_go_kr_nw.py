import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "mogef-go-kr-nw"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
FUNCS = ["_curl_post", "_curl_get"]


def row(sn, num, title, day):
    return f"""<tr><td class="tnum">{num}</td><td class="title"><a href="javascript:fn_selectView('{sn}')"
title="{title}"><span>{title}</span></a></td><td class="moDpNone">홍보담당관</td><td>{day}</td></tr>"""


PAGE1 = ("<html><body><table class='brdList01'><tbody>" + row("711473", "9001", "보도 A", "2026-10-16")
         + row("711466", "9000", "보도 B", "2026-10-15") + "</tbody></table></body></html>")
EMPTY = "<html><body><table class='brdList01'><tbody></tbody></table></body></html>"


def detail(title, body):
    return f"""<html><body><table><tr><th class="brdViewTit">{title}</th></tr>
<tr><th id="W_th01">담당부서</th><td>홍보담당관</td></tr><tr><th id="W_th03">등록일</th><td>2026-10-16</td></tr>
<tr><td class="addFile"><a onclick="fn_fileDownload('11', '1')">보도자료.pdf</a></td></tr>
<tr><td class="brdViewCont"><p>{body}</p></td></tr></table></body></html>"""


DETAILS = {"711473": detail("보도 A", "성평등가족부 보도자료 본문입니다. " * 5), "711466": detail("보도 B", "짧음")}


class MogefSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        if "nw_rpd_s001d.do" in url:
            return DETAILS.get(q.get("bbtSn"))
        if "nw_rpd_s001.do" in url:
            return PAGE1 if q.get("pageIndex", "1") == "1" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=FUNCS)
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=FUNCS)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
