import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "bmwkms-gv-at-themen"
START = ("https://www.bmwkms.gv.at/themen/kunst-und-kultur/service-kunst-und-kultur"
         "/publikationen/berichte-innovative-film-austria.html")
PAGE = """<html><body><main>
<a class="file" href="/dam/jcr:aaaa-1111/IFA_21_22.pdf">Innovative Film Austria 21/22 <span class="fileinfo">(PDF, 5 MB)</span></a>
<a class="file" href="/dam/jcr:bbbb-2222/IFA_2019.pdf">Innovative Film Austria 2019 (PDF, 4 MB)</a>
<a class="file" href="/dam/jcr:aaaa-1111/IFA_21_22.pdf">doppelt</a>
<a class="file" href="files/katalog.pdf">(PDF, 1 MB)</a>
</main></body></html>"""


class BmwkmsSplit(unittest.TestCase):
    def serve(self, url):
        return PAGE if url.split("?")[0] == START else None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(3, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
