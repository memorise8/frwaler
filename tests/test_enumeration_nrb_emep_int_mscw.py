import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "emep-int-mscw"
START = "https://emep.int/mscw/mscw_publications.html"
PAGE = """<html><body><a name="2025"></a>
<dl><dt>EMEP Status Report 1/2025</dt><dd><i>"Transboundary particulate matter, photo-oxidants, acidifying and eutrophying components"</i><br>
<font>Joint MSC-W &amp; CCC Report</font><br><a href="/publ/reports/2025/EMEP_Status_Report_1_2025.pdf">pdf</a></dd></dl>
<dl><dt>Note 2/2025</dt><dd>short</dd></dl>
<a name="2024"></a>
<dl><dt>MSC-W Technical Report 1/2024</dt><dd><i>"Model evaluation of the EMEP MSC-W chemical transport model for the year 2022 across Europe"</i><br>
<a href="/publ/reports/2024/MSCW_technical_1_2024.pdf">pdf</a></dd></dl>
</body></html>"""


class EmepSplit(unittest.TestCase):
    def serve(self, url):
        return PAGE if url.split("?")[0] == START else None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
