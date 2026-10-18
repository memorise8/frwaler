import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "data-statistik-gv-at-web"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def row(ds, title, day):
    return f"""<tr><td><a href="meta.jsp?dataset={ds}">{title}</a><p>Beschreibung {title}</p></td><td>{day}</td>
<td><a href="/ogd/json?dataset={ds}">json</a><a href="/data/{ds}.csv">csv</a></td></tr>"""


CATALOG = ("<html><body><div class='panel'><div class='panel-title'>Bildung</div><table>"
           + row("OGD_A_1", "Lehrpersonen", "16.10.2026") + row("OGD_B_1", "Kurz", "15.10.2026")
           + "</table></div></body></html>")
DETAILS = {
    "OGD_A_1": {"title": "Lehrpersonen an Hochschulen",
                "notes": "Anzahl an Lehrpersonen nach Bundesland und Personaltyp, jährlich aktualisiert seit 2010.",
                "metadata_created": "2026-10-16T00:00:00",
                "resources": [{"url": "https://data.statistik.gv.at/data/OGD_A_1.csv", "format": "csv",
                               "created": "2026-10-16"}],
                "extras": {"begin_datetime": "2010-01-01"}},
    "OGD_B_1": {"title": "", "resources": []},
}


class DataStatistikSplit(unittest.TestCase):
    def serve(self, url):
        if "/ogd/json" in url:
            d = DETAILS.get(query(url).get("dataset"))
            return None if d is None else json.dumps(d)
        if "/web/catalog.jsp" in url:
            return CATALOG
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
