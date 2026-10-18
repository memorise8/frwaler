import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "moef-go-kr-nw"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def li(ntt, title, day):
    return f"""<li><h3><a href="javascript:fn_egov_select('{ntt}')">{title}</a></h3>
<div class="boardInfo"><span class="date">{day}</span><span class="depart">재정정책과</span></div></li>"""


PAGE1 = ("<html><body><ul class='boardType3 explnList'>" + li("MOSF_000000000079680", "보도 A", "2026.10.16")
         + li("MOSF_000000000079661", "보도 B", "2026.10.15") + "</ul></body></html>")
EMPTY = "<html><body><ul class='boardType3 explnList'></ul></body></html>"


def detail(title, body):
    return f"""<html><body><div class="detailBoard"><h3>{title}</h3>
<div class="boardInfo"><span class="date">2026.10.16</span></div>
<div class="editorCont"><p>{body}</p></div>
<a href="/com/cmm/fms/FileDown.do?atchFileId=F1&amp;fileSn=1">보도자료.pdf</a></div></body></html>"""


DETAILS = {"MOSF_000000000079680": detail("보도 A", "기획재정부 보도자료 본문입니다. " * 5),
           "MOSF_000000000079661": detail("보도 B", "짧음")}


class MoefSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        if "detailNesDtaView.do" in url:
            return DETAILS.get(q.get("searchNttId1"))
        if "/nw/nes/nesdta.do" in url:
            return PAGE1 if q.get("pageIndex", "1") == "1" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
