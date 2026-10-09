"""Native date-range persistence with controlled provider responses and real DBs."""
import json
import os
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler.base_crawler import CrawlCancelled
from tests.test_public_api_resume import ApiResumeContract


class CommittedInterruption(BaseException):
    pass


class RangeContract:
    SITE='federalregister-gov-documents'; BACKEND='sqlite'
    count=ApiResumeContract.count

    def setUp(self):
        ApiResumeContract.setUp(self)
        self.records=[{'document_number':str(n),'html_url':f'https://www.federalregister.gov/documents/2020/01/10/{n}',
                       'publication_date':'2020-01-10','title':f'Research {n}','abstract':'Scientific research. '*10} for n in range(9)]
        self.cursor=None;self.calls=[]

    def run_chunk(self, *, limit=None, max_pages=20, expected=None, fail_at=None, cancel_at=None,
                  interrupt_at=None, alter=None):
        c=self.cls(self.conn);self.last=c;c.delivery_cursor=self.cursor;c.delivery_mode='full'
        self.addCleanup(c._session.close)
        if cancel_at is not None:c.delivery_should_cancel=lambda:self.count()>=cancel_at
        if interrupt_at is not None:
            def committed(seq):
                if self.count()==interrupt_at:raise CommittedInterruption()
            c.delivery_document_saved=committed
        if fail_at is not None:
            original=c._save_paper
            def save(paper):
                if paper['post_number']==str(fail_at):raise RuntimeError('controlled persistence failure')
                return original(paper)
            c._save_paper=save
        def request(url):
            q={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()}
            self.assertEqual(q['conditions[publication_date][gte]'],'2020-01-01')
            self.assertEqual(q['conditions[publication_date][lte]'],'2020-01-31')
            page=int(q['page']);self.calls.append(page)
            docs=self.records[(page-1)*3:page*3]
            nxt=dict(q,page=str(page+1))
            data={'count':len(self.records),'results':docs,'next_page_url':c._API_BASE+'?'+urlencode(nxt) if page*3<len(self.records) else None}
            if alter:alter(data,q)
            response=Mock();response.json.return_value=data;return response
        c._request=Mock(side_effect=request)
        try:
            return c.crawl_date_range('2020-01-01','2020-01-31',len(self.records) if expected is None else expected,
                                      limit=limit,max_pages=max_pages)
        finally:
            self.cursor=json.loads(json.dumps(c.delivery_pending_cursor or self.cursor))

    def test_limits_resume_exact_remainder_and_range_is_not_whole_site(self):
        for n in (2,2,2,2,1):self.assertEqual(self.run_chunk(limit=2),n)
        self.assertEqual(self.count(),9)
        self.assertTrue(self.last.delivery_date_range_exhausted)
        self.assertFalse(self.last.delivery_exhausted)
        self.assertEqual(self.run_chunk(),0)
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_one_page_budget_can_advance_past_resume_anchor(self):
        for n in (3,3,3):self.assertEqual(self.run_chunk(max_pages=1),n)
        self.assertEqual(self.calls,[1,1,2,2,3])
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_filtered_source_records_advance_checkpoint(self):
        for d in self.records[3:6]:d['abstract']='short'
        self.assertEqual(self.run_chunk(max_pages=1),3)
        self.assertEqual(self.run_chunk(max_pages=1),0)
        self.assertEqual(len(json.loads(self.cursor['seen_keys_json'])),6)
        self.assertEqual(self.run_chunk(max_pages=1),3)
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_cancel_at_two_then_resume(self):
        with self.assertRaises(CrawlCancelled):self.run_chunk(cancel_at=2)
        self.assertEqual(self.count(),2)
        self.assertEqual(self.run_chunk(),7)
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_commit_interruption_replays_saved_item_without_counting_it_twice(self):
        with self.assertRaises(CommittedInterruption):self.run_chunk(interrupt_at=3)
        self.assertEqual(self.count(),3)
        self.assertEqual(self.cursor['processed_in_page'],2)
        self.assertEqual(self.run_chunk(),6)
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_failed_save_does_not_advance_cursor(self):
        self.assertEqual(self.run_chunk(fail_at=2),2)
        self.assertEqual(self.last.delivery_source_failure,'item_failed')
        self.assertEqual(self.cursor['processed_in_page'],2)
        self.assertEqual(self.run_chunk(),7)

    def test_changed_anchor_fails_without_more_saves(self):
        self.run_chunk(limit=2);before=dict(self.cursor);self.records[0]['title']='Changed'
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')
        self.assertEqual(self.cursor,before)

    def test_changed_count_or_ignored_dates_fail_before_saving(self):
        self.assertEqual(self.run_chunk(expected=8),0)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')
        self.records[0]['publication_date']='2019-01-10'
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')
        self.assertEqual(self.count(),0)

    def test_truncated_continuation_is_incomplete(self):
        self.assertEqual(self.run_chunk(alter=lambda d,q:d.update(next_page_url=None)),3)
        self.assertFalse(self.last.delivery_date_range_exhausted)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')

    def test_continuation_cannot_drop_date_filters(self):
        def alter(d,q):
            q.pop('conditions[publication_date][gte]');d['next_page_url']=self.last._API_BASE+'?'+urlencode(q)
        self.assertEqual(self.run_chunk(alter=alter),0)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')

    def test_exact_duplicate_source_record_is_not_counted(self):
        self.records[1]=dict(self.records[0])
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')

    def test_number_reuse_with_different_original_url_is_preserved(self):
        self.records[1]['document_number']=self.records[0]['document_number']
        self.assertEqual(self.run_chunk(),9)
        self.assertTrue(self.last.delivery_date_range_exhausted)

    def test_wrong_source_cursor_rejected_without_request(self):
        self.run_chunk(limit=2);self.cursor['source_hash']='wrong';self.calls=[]
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.last.delivery_source_failure,'configuration')
        self.assertEqual(self.calls,[])

    def test_empty_reviewed_range_finishes_only_that_range(self):
        self.records=[]
        self.assertEqual(self.run_chunk(),0)
        self.assertTrue(self.last.delivery_date_range_exhausted)
        self.assertFalse(self.last.delivery_exhausted)

    def test_cancel_before_request(self):
        with self.assertRaises(CrawlCancelled):self.run_chunk(cancel_at=0)
        self.assertEqual(self.calls,[])


class SQLiteFederalRange(RangeContract,unittest.TestCase):pass


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class PostgresFederalRange(RangeContract,unittest.TestCase):
    BACKEND='postgres'
