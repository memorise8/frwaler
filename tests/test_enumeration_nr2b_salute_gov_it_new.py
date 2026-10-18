import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "salute-gov-it-new"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def node(i, kind, title):
    return {"drupal_id": f"id-{i}", "title": title, "field_data_pubblicazione": "2026-09-01",
            "path": {"alias": f"/pubblicazione/{i}"},
            "body": {"processed": "<p>" + "Rapporto del Ministero della Salute sulla sorveglianza. " * 3 + "</p>"},
            "field_abstract": "Rapporto del Ministero della Salute sulla sorveglianza sanitaria annuale.",
            "relationships": {"field_tipologia_pubblicazione": {"name": kind}, "field_temi": [{"name": "Salute"}],
                              "field_listafile": []}}


NODES = [node(1, "Rapporti", "Rapporto A"), node(2, "Opuscoli", "Opuscolo B"), node(3, "Rapporti", "Rapporto C")]
PAYLOAD = {"result": {"data": {"node": {"relationships": {"field_view_list": [
    {"fields": {"view": {"view_query_data": {"nodes": NODES}}}}]}}}}}


class SaluteSplit(unittest.TestCase):
    def serve(self, url):
        return PAYLOAD if "page-data.json" in url else None

    def init(self, crawler):
        crawler._fetch_nodes = lambda cookie_file, data_file: [
            n for n in NODES if crawler._node_type(n) == crawler._TARGET_TYPE]

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
