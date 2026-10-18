"""Task 11 source-backed synthetic date regressions. No site or database I/O."""
import ast
import contextlib
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler.document_dates import normalize_date

ROOT=Path(__file__).resolve().parents[1]
SPECS=json.loads((ROOT/'tests/fixtures/collector_defects/cases.json').read_text())
CACHE={}


def value(spec,raw):
    sid=spec['site_id']
    if sid not in CACHE:
        path=ROOT/'crawler/sites/custom'/f'{sid}.py'
        loader=importlib.util.spec_from_file_location('date_regression_'+sid.replace('-','_'),path)
        module=importlib.util.module_from_spec(loader);oldpath=sys.path[:]
        try:loader.loader.exec_module(module)
        finally:sys.path[:]=oldpath
        obj=next((object.__new__(x) for x in vars(module).values() if isinstance(x,type)
                  and getattr(x,'site_id',None)==sid),None)
        CACHE[sid]=(module,obj)
    module,obj=CACHE[sid]
    method=getattr(module,spec['method'],None) or getattr(obj,spec['method'])
    if spec['kind']=='fiscal':
        soup=BeautifulSoup(raw,'html.parser')
        return method(soup,'https://www.doi.gov/2027/highlights')
    soup=BeautifulSoup(raw,'html.parser')
    if spec['kind']=='parts':return method(json.loads(soup.select_one('script').string))
    return method(soup.select_one('time').get_text(strip=True))


def normalized(spec,raw):return normalize_date(value(spec,raw))['value']


def unchanged_source_fingerprint(source,method):
    tree=ast.parse(source);removed=set()
    for n in ast.walk(tree):
        if isinstance(n,ast.FunctionDef) and n.name==method:
            removed.update(range(n.lineno,n.end_lineno+1))
    lines=[line for i,line in enumerate(source.splitlines(),1) if i not in removed and line.strip()
           and line.strip() not in ('from crawler.source_date_text import separate_date_parts','from datetime import date')]
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def build_case(spec):
    class DateCase(unittest.TestCase):
        def setUp(self):
            self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
            for name in ['socket.socket.connect','socket.create_connection','socket.getaddrinfo',
                         'subprocess.Popen._execute_child','sqlite3.connect','psycopg.connect']:
                self.stack.enter_context(patch(name,side_effect=AssertionError('real I/O forbidden')))
        def test_nested_html_date_regression(self):
            expected=spec['regression_expected']
            if expected is None:
                self.assertIsNone(normalized(spec,spec['regression_html']))
            else:
                self.assertEqual(expected,value(spec,spec['regression_html']))
        def test_existing_full_date_unchanged(self):
            self.assertEqual(spec['full_expected'],normalized(spec,spec['full_html']))
        def test_absent_date_is_null(self):
            self.assertIsNone(normalized(spec,spec['empty_html']))
            for raw in spec.get('invalid_html',[]):
                with self.subTest(raw=raw):self.assertIsNone(normalized(spec,raw))
        def test_precision_has_no_invented_day(self):
            expected=spec['partial_expected'];actual=value(spec,spec['partial_html'])
            result=normalize_date(actual)
            if expected is None:
                self.assertIsNone(result['value'])
            else:
                self.assertEqual(expected,actual)
                self.assertIsNone(result['value'])  # DATE column stays NULL at partial precision
                self.assertEqual('month' if len(expected)==7 else 'year',result['precision'])
            for raw,expected in spec.get('additional_partial',[]):
                with self.subTest(raw=raw):
                    actual=value(spec,raw);self.assertEqual(expected,actual)
                    result=normalize_date(actual);self.assertIsNone(result['value'])
                    self.assertEqual('month',result['precision'])
        def test_identity_and_all_other_source_lines_unchanged(self):
            source=(ROOT/'crawler/sites/custom'/f"{spec['site_id']}.py").read_text()
            self.assertEqual(spec['outside_date_sha256'],unchanged_source_fingerprint(source,spec['method']))
    DateCase.__name__='Date_'+spec['site_id'].replace('-','_')
    DateCase.__qualname__=DateCase.__name__
    return DateCase


for spec in SPECS:globals()['Date_'+spec['site_id'].replace('-','_')]=build_case(spec)
