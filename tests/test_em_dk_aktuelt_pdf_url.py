import unittest

from tests.collector_split_helpers import load

SITE = "em-dk-aktuelt"
URL = "https://www.em.dk/aktuelt/udgivelser/2023/aug/vaekstplan-for-bornholm/"
TEXT = ("Regeringen har sammen med Bornholms Regionskommune lagt en vækstplan for Bornholm, "
        "der skal styrke erhvervslivet og skabe flere arbejdspladser på øen.")


def page(*buttons):
    links = "".join(f'<a class="btn" href="{h}">Hent</a>' for h in buttons)
    return (f'<html><body><h1 class="heading">Vækstplan for Bornholm</h1><span class="date">17. august 2023</span>'
            f'<div class="rich-text"><p>{TEXT}</p></div><div class="group">{links}</div></body></html>')


class EmDkPdfUrl(unittest.TestCase):
    def setUp(self):
        _, self.module = load(SITE)

    def test_picks_pdf_button_after_picture(self):
        paper = self.module._parse_detail(URL, page("/media/11758/vaekstplan-for-bornholm.jpg",
                                                    "/media/11759/vaekstplan-for-bornholm.pdf"))
        self.assertEqual("https://www.em.dk/media/11759/vaekstplan-for-bornholm.pdf", paper["pdf_url"])

    def test_no_pdf_when_only_picture_or_word(self):
        paper = self.module._parse_detail(URL, page("/media/11758/vaekstplan-for-bornholm.jpg",
                                                    "/Media/638270008322306948/One%20pager.docx"))
        self.assertIsNone(paper["pdf_url"])


if __name__ == "__main__":
    unittest.main()
