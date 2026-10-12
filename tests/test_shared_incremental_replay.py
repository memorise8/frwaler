import hashlib
import tempfile
import unittest
from pathlib import Path

from delivery.scripts.replay_shared_incremental import RecordedTransport


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.body = b'{"results":[]}'
        (self.root/'000.body').write_bytes(self.body)
        self.records = [{'method':'GET', 'url':'https://example.invalid/api?page=1',
                         'status':200, 'bytes':len(self.body),
                         'sha256':hashlib.sha256(self.body).hexdigest()}]

    def test_tampered_recording_is_rejected_before_collection(self):
        (self.root/'000.body').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            RecordedTransport(self.root, self.records)

    def test_wrong_query_and_unrecorded_request_do_not_match_fixture(self):
        replay = RecordedTransport(self.root, self.records)
        with self.assertRaisesRegex(AssertionError, 'contract differs'):
            replay('GET', 'https://example.invalid/api', params={'page':2})
        self.assertEqual(replay.calls, [])
        response = replay('GET', 'https://example.invalid/api', params={'page':1})
        self.assertEqual(response.json(), {'results':[]})
        with self.assertRaisesRegex(AssertionError, 'unrecorded'):
            replay('GET', 'https://example.invalid/api', params={'page':1})


if __name__ == '__main__':
    unittest.main()
