import json
import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "sib-swiss-training"
START = "https://www.sib.swiss/training/training-materials"


def course(code, name, abstract):
    return {"@type": "Course", "name": name, "abstract": abstract, "description": "",
            "keywords": "bioinformatics, data", "educationalLevel": "Beginner",
            "hasCourseInstance": [{"url": f"https://www.sib.swiss/training/course/{code}", "startDate": "2026-05-04",
                                   "instructor": [{"name": "A. Teacher"}]}]}


LONG = "An introduction to metagenomics data analysis with hands-on exercises on real sequencing datasets."
BLOCKS = [course("20260504_MEMIC", "Metagenomics", LONG), course("20260601_X", "Short", "tiny"),
          course("20260504_MEMIC", "Metagenomics", LONG)]
PAGE = "<html><body><main>" + "".join(
    f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in BLOCKS) + "</main></body></html>"


class SibTrainingSplit(unittest.TestCase):
    def serve(self, url):
        return PAGE if url.split("?")[0] == START else None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
