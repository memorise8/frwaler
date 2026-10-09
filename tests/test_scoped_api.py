import os
import unittest
from unittest.mock import patch

from crawler import db_pg
from delivery.worker import jobs


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class ScopedApi(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from delivery.be.app import create_app
        from delivery.db.schema import init_delivery_schema
        self.enterContext(patch.dict(os.environ, {'DELIVERY_AUTH_MODE': 'disabled'}))
        self.conn = db_pg.open_db(os.environ['TEST_PG_DSN']); self.addCleanup(self.conn.close)
        self.conn.execute('DROP SCHEMA public CASCADE'); self.conn.execute('CREATE SCHEMA public'); self.conn.commit()
        db_pg.init_db(self.conn); init_delivery_schema(self.conn)
        self.site = 'federalregister-gov-documents'
        db_pg.upsert_site(self.conn, self.site, self.site, 'https://www.federalregister.gov')
        self.client = TestClient(create_app(os.environ['TEST_PG_DSN'])); self.addCleanup(self.client.close)
        self.body = {'site_id': self.site, 'lower': '2020-01-01', 'upper': '2020-01-31', 'trial': True,
                     'observation_id': 'fixture-observation', 'ranges': [{'lower': '2020-01-01', 'upper': '2020-01-31', 'expected_count': 10}]}

    def create(self):
        result = self.client.post('/jobs/date-plan', json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['id']

    def test_scope_creation_detail_and_retry_preserve_scope(self):
        jid = self.create()
        detail = self.client.get(f'/jobs/{jid}').json()
        self.assertEqual(detail['scope']['lower'], '2020-01-01')
        self.assertFalse(detail['scope']['whole_site_completion'])
        self.assertIsNone(detail['scope']['completed_at'])
        self.assertEqual(self.client.post(f'/jobs/{jid}/cancel').status_code, 200)
        self.assertEqual(self.client.post(f'/jobs/{jid}/retry', json={}).status_code, 422)
        retried = self.client.post(f'/jobs/{jid}/retry', json={'trial': True})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()['scope_id'], detail['scope_id'])
        self.assertEqual(self.client.post(f'/jobs/{jid}/retry', json={'trial': True}).status_code, 409)

    def test_trial_shape_and_source_scope_validation(self):
        bad = [dict(self.body, trial=False), dict(self.body, site_id='unknown'),
               dict(self.body, observation_id=' '), dict(self.body, upper='2020-02-31'),
               dict(self.body, ranges=[]), dict(self.body, limit_n=True),
               dict(self.body, ranges=[{'lower':'2020-01-02','upper':'2020-01-31','expected_count':10}]),
               dict(self.body, ranges=[{'lower':'2020-01-01','upper':'2020-01-31','expected_count':True}])]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/jobs/date-plan', json=body).status_code, 422)
        self.assertEqual(jobs.list_jobs(self.conn), [])

    def test_authentication_and_active_job_conflict(self):
        with patch.dict(os.environ, {'DELIVERY_AUTH_MODE':'token','DELIVERY_API_TOKEN':'x'*32}):
            self.assertEqual(self.client.post('/jobs/date-plan', json=self.body).status_code, 401)
            result = self.client.post('/jobs/date-plan', json=self.body, headers={'x-delivery-token':'x'*32})
            self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/jobs/date-plan', json=self.body).status_code, 409)

    def test_retry_conflict_and_changed_source_fail_without_new_jobs(self):
        jid = self.create(); jobs.cancel_job(self.conn, jid)
        self.body['observation_id'] = 'second'; second = self.create()
        self.assertEqual(self.client.post(f'/jobs/{jid}/retry', json={'trial':True}).status_code, 409)
        jobs.cancel_job(self.conn, second)
        with patch('delivery.worker.scopes.source_hash_for_class', return_value='changed'):
            self.assertEqual(self.client.post(f'/jobs/{jid}/retry', json={'trial':True}).status_code, 422)
        self.assertEqual(len(jobs.list_jobs(self.conn)), 2)

    def test_unscoped_request_does_not_gain_backfill_permission(self):
        self.assertEqual(self.client.post('/jobs', json={'site_id':self.site,'mode':'backfill','trial':True}).status_code, 422)
