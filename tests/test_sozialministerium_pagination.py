import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SozialministeriumPagination(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "sozialministerium_pagination",
            ROOT / "crawler/sites/custom/sozialministerium-gv-at-services.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.crawler = object.__new__(module.SozialministeriumGvAtServicesCrawler)

    def parse(self, links):
        page = '<main id="content">' + links + '</main>'
        return self.crawler._parse_list_page(
            page, 'https://www.sozialministerium.gv.at/Services/Studien.html')[1]

    def test_actual_table_of_contents_does_not_indicate_next_page(self):
        self.assertFalse(self.parse(
            '<a href="#weitere-studien-und-publikationen-05-1">'
            'Weitere Studien und Publikationen</a>'))

    def test_fragment_and_missing_destinations_are_not_page_transitions(self):
        for link in ['<a rel="next" href="#section">Next</a>',
                     '<a class="next" href="">Weiter</a>',
                     '<a>Next</a>']:
            with self.subTest(link=link):
                self.assertFalse(self.parse(link))

    def test_explicit_next_page_links_remain_supported(self):
        for link in ['<a rel="next" href="?page=2">2</a>',
                     '<a href="?page=2">Weiter</a>',
                     '<a href="?page=2">Nächste Seite</a>',
                     '<a href="?page=2">Next page</a>']:
            with self.subTest(link=link):
                self.assertTrue(self.parse(link))


if __name__ == '__main__':
    unittest.main()
