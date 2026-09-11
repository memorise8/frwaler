import os
import unittest

from delivery.crawler_status import result_state


class ResultStateTest(unittest.TestCase):
    def test_zero_is_unknown_not_success(self):
        self.assertEqual(result_state({'status': 'done', 'saved_count': 0}), 'empty')
        self.assertEqual(result_state({'status': 'failed', 'saved_count': 5}), 'failed')
        self.assertEqual(result_state(None), 'unrun')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires disposable test PostgreSQL')
class StatusDatabaseTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        from delivery.be.app import create_app
        from fastapi.testclient import TestClient
        self.conn = db_pg.open_db(os.environ['TEST_PG_DSN'])
        self.conn.execute('DROP TABLE IF EXISTS crawl_jobs CASCADE')
        self.conn.commit()
        init_delivery_schema(self.conn)
        self.client = TestClient(create_app(os.environ['TEST_PG_DSN']))

    def tearDown(self):
        self.client.close()
        self.conn.close()

    def job(self, status, count=0, error=None):
        self.conn.execute('''INSERT INTO crawl_jobs(site_id,status,saved_count,error,finished_at)
            VALUES ('site',%s,%s,%s,CASE WHEN %s IN ('done','failed') THEN clock_timestamp() ELSE NULL END)''',
            (status, count, error, status))
        self.conn.commit()

    def test_latest_failure_does_not_get_hidden_by_past_success(self):
        self.job('done', 3)
        self.job('failed', error='source changed')
        self.job('queued')
        response = self.client.get('/crawler-status')
        self.assertEqual(response.status_code, 200)
        row = response.json()['items'][0]
        self.assertEqual(row['result_state'], 'failed')
        self.assertEqual(row['last_success']['saved_count'], 3)
        self.assertEqual(row['latest_job']['status'], 'queued')
        self.assertEqual(row['active_counts'], {'queued': 1})
        self.assertEqual(row['last_result']['error'], 'source changed')

    def test_no_history_and_site_filter(self):
        self.assertEqual(self.client.get('/crawler-status').json()['items'], [])
        self.job('done')
        self.assertEqual(self.client.get('/crawler-status?site_id=absent').json()['items'], [])
        self.assertEqual(self.client.get('/crawler-status?site_id=site').json()['items'][0]['result_state'], 'empty')

    def test_unavailable_is_not_an_empty_success_response(self):
        from unittest.mock import patch
        with patch('delivery.be.app.db_pg.open_db', side_effect=RuntimeError('private connection details')):
            response = self.client.get('/crawler-status')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private connection', response.text)
