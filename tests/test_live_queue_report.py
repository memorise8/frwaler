import json
from pathlib import Path
import tempfile
import unittest

from delivery.scripts.summarize_live_queues import digest, load_batch


class LiveQueueReportTests(unittest.TestCase):
    def fixture(self, out):
        original = dict(site_id='example', delivery=True, live_check='Check the listing total',
                        reason='disagreement', sonnet_strategy='사이트맵', opus_strategy=None)
        result = dict(site_id='example', delivery=True, live_check=original['live_check'], kind='사이트맵',
                      strategy='불가', confidence='낮음', expected_items=None, count_basis='알 수 없음',
                      count_confidence='낮음', hidden_cap_suspected=False, requests=[], actual_HTTP_responses=0,
                      count_evidence=dict(scope_estimates=[]),
                      offline_assessment={k:original[k] for k in ('reason','sonnet_strategy','opus_strategy')})
        (out/'sites').mkdir(); self.write(out/'sites/example.json', result)
        self.write(out/'input-queue.json', [original]); self.write(out/'plans.json', dict(sites=[original]))
        self.write(out/'disposition-audit.json', dict(issues={}, missing=[], sites=[dict(site_id='example',
            verdict='access_or_policy_unavailable', result_sha256=digest(out/'sites/example.json'))]))
        self.seal(out)
        return result

    def write(self, path, data):
        path.write_text(json.dumps(data))

    def seal(self, out):
        self.write(out/'extra-completion.json', dict(complete=True,
            disposition_audit_sha256=digest(out/'disposition-audit.json'),
            plans_sha256=digest(out/'plans.json'), queue_sha256=digest(out/'input-queue.json')))

    def test_report_retains_unknown_and_offline_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp); self.fixture(out); rows,_=load_batch(out,'extra')
            self.assertIsNone(rows[0]['expected_items'])
            self.assertEqual(rows[0]['offline_reason'],'disagreement')
            self.assertEqual(rows[0]['final_disposition'],'access_or_policy_unavailable')

    def test_changed_site_evidence_invalidates_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp); result=self.fixture(out); result['expected_items']=100
            self.write(out/'sites/example.json',result)
            with self.assertRaisesRegex(ValueError,'site evidence changed'):load_batch(out,'extra')

    def test_changed_queue_invalidates_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp); self.fixture(out); self.write(out/'input-queue.json',[])
            with self.assertRaisesRegex(ValueError,'queue or plans changed'):load_batch(out,'extra')

    def test_missing_audit_row_cannot_be_marked_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp); self.fixture(out)
            self.write(out/'disposition-audit.json',dict(issues={},missing=[],sites=[])); self.seal(out)
            with self.assertRaisesRegex(ValueError,'audit site coverage mismatch'):load_batch(out,'extra')


if __name__=='__main__':unittest.main()
