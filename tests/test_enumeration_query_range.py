"""Literal range-query parameter support, no network/storage."""
import copy
import unittest
from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from crawler.enumeration.plan import PlanError, site_plan
from crawler.enumeration.strategies import DateRangeSplit


def plan():
    return {'sites': {'example': {
        'strategy': 'date_range_split', 'base_url': 'https://example.org',
        'listing': {'url': 'https://example.org/api?document_type=thesis', 'page_param': 'page'},
        'segments': {'from': '2020', 'filter': {'type': 'query_range', 'param': 'q', 'field': '_created'}},
        'overall_total_check': True}}}


class QueryRange(unittest.TestCase):
    def strategy(self, data=None):
        p=site_plan(data or plan(), 'example')
        return DateRangeSplit(SimpleNamespace(plan=p,today=date(2020,6,10)))

    def test_date_ranges_and_one_encoding(self):
        s=self.strategy()
        for key,expected in [('2020','_created:[2020-01-01 TO 2020-06-10]'),
                             ('2020-02','_created:[2020-02-01 TO 2020-02-29]'),
                             ('2020-02-29','_created:[2020-02-29 TO 2020-02-29]')]:
            self.assertEqual({'q':expected},s.filter_params(key))
            url=s.page_url(1,s.filter_params(key))
            self.assertEqual([expected],parse_qs(urlsplit(url).query)['q'])
            self.assertEqual(['thesis'],parse_qs(urlsplit(url).query)['document_type'])
            self.assertNotIn('%253A',url)

    def test_range_filter_still_unchanged(self):
        d=plan();d['sites']['example']['segments']['filter']={'type':'range','from_param':'from','to_param':'to'}
        self.assertEqual({'from':'2020-02-01','to':'2020-02-29'},self.strategy(d).filter_params('2020-02'))

    def test_validation_and_fingerprint(self):
        d=plan();old=site_plan(d,'example').fingerprint
        d['sites']['example']['segments']['filter']['field']='_updated'
        self.assertNotEqual(old,site_plan(d,'example').fingerprint)
        for key,value in [('param',None),('field','_created OR *'),('to_inclusive',False)]:
            d=plan();d['sites']['example']['segments']['filter'][key]=value
            with self.assertRaises(PlanError):site_plan(d,'example')

    def test_existing_scope_and_disabled_reconciliation_rejected(self):
        for which in ('url','params','overall'):
            d=plan();s=d['sites']['example']
            if which=='url':s['listing']['url']+='&q=existing-scope'
            elif which=='params':s['listing']['params']={'q':'existing-scope'}
            else:s['overall_total_check']=False
            with self.assertRaises(PlanError):site_plan(d,'example')
