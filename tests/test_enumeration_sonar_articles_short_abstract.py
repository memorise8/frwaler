"""Article scope keeps short and empty abstracts (wider than the registered rule); thesis and crawl() do not.

Observed on the server sample pid:100-101: 191 of 1051 article records (18%) had a 0-char abstract,
clustered, which stopped the site on consecutive contract failures.
"""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.plan import load_plan
from tests import test_enumeration_sonar_articles as articles
from tests import test_enumeration_sonar_articles_pid as pid
from tests import test_enumeration_sonar_contract as thesis
from tests.sonar_memory import MemoryStore

SITE = thesis.SITE
LONG = 'Synthetic abstract. ' * 10


def with_abstract(rec, value):
    rec = copy.deepcopy(rec)
    if value is None:
        rec['metadata'].pop('abstracts', None)
    else:
        rec['metadata']['abstracts'] = [{'language': 'eng', 'value': value}] if value is not False else []
    return rec


class ShortAbstract(unittest.TestCase):
    setUp = thesis.Offline.setUp
    run_plan = thesis.Offline.run_plan

    def config(self):
        return copy.deepcopy(load_plan(pid.PID_PLAN))

    def test_empty_short_whitespace_and_absent_abstracts_become_papers(self):
        base = pid.article(7)
        reference = self.crawler._paper_from_article_result(with_abstract(base, LONG))
        for value, expected in [('', ''), ('x', 'x'), ('x' * 99, 'x' * 99), ('   ', ''), (None, ''), (False, '')]:
            with self.subTest(abstract=value):
                paper = self.crawler._paper_from_article_result(with_abstract(base, value))
                self.assertIsNotNone(paper)
                self.assertEqual(expected, paper['abstract'])
                # Identity, dates, category and every other field equal the long-abstract paper.
                self.assertEqual({**reference, 'abstract': expected}, paper)
                self.assertEqual('coar:c_6501', paper['category'])
                self.assertEqual(paper_identity(clean_paper(reference)), paper_identity(clean_paper(paper)))
                self.assertEqual(expected, clean_paper(paper)['abstract'])

    def test_missing_title_is_still_rejected(self):
        for rec in (pid.article(8), with_abstract(pid.article(9), '')):
            rec['metadata']['title'] = []
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(self.crawler._paper_from_article_result(rec))
        with self.assertRaisesRegex(ValueError, 'article scope'):      # scope check also runs first
            bad = with_abstract(pid.article(10), '')
            bad['metadata']['documentType'] = 'coar:c_7a1f'
            self.crawler._paper_from_article_result(bad)

    def test_thesis_contract_and_registered_skip_rules_are_unchanged(self):
        crawler_cls = type(self.crawler)
        self.assertEqual(100, crawler_cls.crawl.__globals__['_MIN_ABSTRACT_LEN'])
        with contextlib.redirect_stdout(io.StringIO()):
            for value in ('', 'x', 'x' * 99, None, False):
                rec = with_abstract(thesis.record(1), value)
                self.assertIsNone(self.crawler._parse_record(rec), value)
                self.assertIsNone(self.crawler._paper_from_result(rec), value)
                # An article-typed record through the registered parsers is skipped just the same.
                self.assertIsNone(self.crawler._parse_record(with_abstract(pid.article(2), value)), value)
        self.assertEqual('x' * 100, self.crawler._parse_record(with_abstract(thesis.record(1), 'x' * 100))['abstract'])
        self.assertEqual('coar:c_7a1f', self.crawler._parse_record(thesis.record(1))['category'])

    def test_registered_crawl_still_skips_short_abstracts(self):
        recs = [with_abstract(thesis.record(1), LONG), with_abstract(thesis.record(2), ''),
                with_abstract(thesis.record(3), 'x' * 99), with_abstract(thesis.record(4), 'x' * 100)]
        c = self.crawler
        saved = []
        c.delivery_cursor = {}
        c._fetch_page = lambda page, size: {'hits': {'hits': copy.deepcopy(recs) if page == 1 else []}}

        def save(paper):
            saved.append(paper['external_id'])
            c._last_save_created = True
        c._save_paper = save
        c._advance_cursor = lambda *a, **k: None
        c._mark_exhausted = lambda: None
        with patch.dict(type(c).crawl.__globals__, {'_RATE_SLEEP': 0}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, c.crawl())
        self.assertEqual(['pid-1', 'pid-4'], saved)

    def test_clustered_empty_abstracts_do_not_stop_the_site(self):
        records = []
        for p in range(1, 221):
            rec = pid.article(p)
            if 60 <= p < 120 or p % 6 == 0:          # 60 consecutive and ~16% scattered empty abstracts
                rec = with_abstract(rec, '')
            elif p % 11 == 0:
                rec = with_abstract(rec, 'short abstract')
            records.append(rec)
        empty = sum(not r['metadata']['abstracts'][0]['value'] for r in records)
        self.assertGreater(empty, 60)
        api = pid.PidAPI(records)
        data = self.config()
        data['sites'][SITE]['segments']['hidden_cap'] = 40
        result = self.run_plan(api, data)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual('all_segments_reconciled', result['stop_reason'])
        self.assertEqual(220, len(self.docs))
        self.assertEqual(0, result['stats']['failed_permanent'])
        self.assertEqual((220, {}), self.store.item_counts(SITE))
        for rec in records:
            expected = rec['metadata']['abstracts'][0]['value']
            self.assertEqual(expected.strip(), self.docs[rec['metadata']['pid']]['abstract'])
        self.assertEqual({'coar:c_6501'}, {p['category'] for p in self.docs.values()})

    def test_untitled_record_among_a_cluster_is_a_single_review_item(self):
        records = [with_abstract(pid.article(p), '') for p in range(1, 61)]
        records[30]['metadata']['title'] = []
        data = self.config()
        data['sites'][SITE]['segments']['hidden_cap'] = 40
        result = self.run_plan(pid.PidAPI(records), data)
        self.assertEqual(('complete_with_review', 'adapter_contract_failures:1'),
                         (result['status'], result['stop_reason']), result)
        self.assertEqual((59, {'failed_permanent:empty_item': 1}), self.store.item_counts(SITE))


if __name__ == '__main__':
    unittest.main()
