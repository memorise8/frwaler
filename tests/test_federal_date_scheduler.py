"""Explicit multi-range scheduler: actual SQLite/PostgreSQL saves and resume."""
import json
import os
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler.base_crawler import CrawlCancelled
from tests.test_federal_checkpoint import CommittedInterruption
from tests.test_public_api_resume import ApiResumeContract


class PlanContract:
    SITE='federalregister-gov-documents';BACKEND='sqlite';count=ApiResumeContract.count

    def setUp(self):
        ApiResumeContract.setUp(self)
        self.ranges=[{'lower':'2020-01-01','upper':'2020-01-15','expected_count':5},
                     {'lower':'2020-01-16','upper':'2020-01-31','expected_count':4}]
        self.docs=[{'document_number':str(n),'html_url':f'https://www.federalregister.gov/research/{n}',
                    'publication_date':'2020-01-10' if n<5 else '2020-01-20',
                    'title':f'Research {n}','abstract':'Scientific research. '*10} for n in range(9)]
        self.cursor=None;self.calls=[]

    def run_chunk(self,*,limit=None,budget=200,cancel_at=None,interrupt_at=None,fail_at=None):
        c=self.cls(self.conn);self.last=c;self.addCleanup(c._session.close);c.delivery_cursor=self.cursor
        if cancel_at is not None:c.delivery_should_cancel=lambda:self.count()>=cancel_at
        if interrupt_at is not None:
            def committed(seq):
                if self.count()==interrupt_at:raise CommittedInterruption()
            c.delivery_document_saved=committed
        if fail_at is not None:
            original=c._save_paper
            def save(paper):
                if paper['post_number']==str(fail_at):raise RuntimeError('controlled save failure')
                return original(paper)
            c._save_paper=save
        def request(url):
            q={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()};page=int(q['page'])
            lo,hi=q['conditions[publication_date][gte]'],q['conditions[publication_date][lte]']
            self.calls.append((lo,page))
            records=[d for d in self.docs if lo<=d['publication_date']<=hi]
            data={'count':len(records),'results':records[(page-1)*3:page*3],
                  'next_page_url':c._API_BASE+'?'+urlencode(dict(q,page=str(page+1))) if page*3<len(records) else None}
            response=Mock();response.json.return_value=data;return response
        c._request=Mock(side_effect=request)
        try:return c.crawl_date_plan('2020-01-01','2020-01-31',self.ranges,limit=limit,max_requests=budget)
        finally:self.cursor=json.loads(json.dumps(c.delivery_pending_cursor or self.cursor))

    def test_request_budget_resumes_at_next_range_without_false_site_completion(self):
        self.assertEqual(self.run_chunk(budget=2),5)
        self.assertEqual(self.cursor['range_index'],1)
        self.assertFalse(self.last.delivery_date_plan_exhausted)
        self.assertEqual(self.run_chunk(budget=2),4)
        self.assertTrue(self.last.delivery_date_plan_exhausted)
        self.assertFalse(self.last.delivery_exhausted)
        self.assertEqual(self.cursor['completed_source_records'],9)
        before=len(self.calls);self.assertEqual(self.run_chunk(),0);self.assertEqual(len(self.calls),before)

    def test_new_limits_cross_range_boundaries(self):
        for expected in (2,2,2,2,1):self.assertEqual(self.run_chunk(limit=2),expected)
        self.assertEqual(self.count(),9);self.assertTrue(self.last.delivery_date_plan_exhausted)

    def test_budget_reached_after_partial_next_range_preserves_actual_new_count(self):
        self.assertEqual(self.run_chunk(budget=3),8)
        self.assertEqual(self.cursor['range_index'],1)
        self.assertEqual(self.run_chunk(budget=2),1)
        self.assertTrue(self.last.delivery_date_plan_exhausted)

    def test_cancel_and_commit_interruptions_restore_outer_cursor(self):
        with self.assertRaises(CrawlCancelled):self.run_chunk(cancel_at=4)
        self.assertEqual(self.count(),4)
        self.assertEqual(self.cursor['range_index'],0)
        with self.assertRaises(CommittedInterruption):self.run_chunk(interrupt_at=5)
        self.assertEqual(self.count(),5)
        self.assertEqual(self.run_chunk(),4)
        self.assertTrue(self.last.delivery_date_plan_exhausted)

    def test_save_failure_retries_same_unprocessed_record(self):
        self.assertEqual(self.run_chunk(fail_at=6),6)
        self.assertEqual(self.cursor['range_index'],1)
        self.assertEqual(self.last.delivery_source_failure,'item_failed')
        self.assertEqual(self.run_chunk(),3)
        self.assertTrue(self.last.delivery_date_plan_exhausted)

    def test_code_excluded_range_does_not_prevent_later_range(self):
        for d in self.docs[:5]:d['abstract']='short'
        self.assertEqual(self.run_chunk(budget=2),0)
        self.assertEqual(self.cursor['range_index'],1)
        self.assertEqual(self.run_chunk(budget=2),4)
        self.assertTrue(self.last.delivery_date_plan_exhausted)

    def test_plan_change_is_rejected_without_fetch(self):
        self.run_chunk(limit=2);self.ranges[1]['expected_count']=5;self.calls=[]
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.last.delivery_source_failure,'configuration');self.assertEqual(self.calls,[])

    def test_gaps_overlaps_and_overlarge_ranges_are_rejected(self):
        for lower in ('2020-01-15','2020-01-17'):
            self.ranges[1]['lower']=lower
            self.assertEqual(self.run_chunk(),0)
            self.assertEqual(self.last.delivery_source_failure,'configuration')
        self.ranges[1]['lower']='2020-01-16';self.ranges[1]['expected_count']=1500
        self.assertEqual(self.run_chunk(),0);self.assertEqual(self.calls,[])

    def test_changed_active_anchor_does_not_advance_outer_cursor(self):
        self.run_chunk(limit=2);before=dict(self.cursor);self.docs[0]['title']='Changed'
        self.assertEqual(self.run_chunk(),0)
        self.assertEqual(self.cursor,before)
        self.assertEqual(self.last.delivery_source_failure,'invalid_response')

    def test_zero_limit_and_cancel_before_probe(self):
        self.assertEqual(self.run_chunk(limit=0),0);self.assertEqual(self.calls,[])
        with self.assertRaises(CrawlCancelled):self.run_chunk(cancel_at=0)
        self.assertEqual(self.calls,[])


class SQLiteFederalPlan(PlanContract,unittest.TestCase):pass


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class PostgresFederalPlan(PlanContract,unittest.TestCase):
    BACKEND='postgres'
