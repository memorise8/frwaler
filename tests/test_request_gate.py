import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest

from delivery.health.request_gate import RequestGate, NetworkDeferred, UngovernedRequest


def occupy(root, host, queue, ready=None):
    with RequestGate(root, delay=0).enter('https://'+host):
        start=time.monotonic()
        if ready:
            ready.set()
            time.sleep(30)
        else:
            time.sleep(0.12)
            queue.put((start,time.monotonic()))


class RequestGateTests(unittest.TestCase):
    def test_cooldown_survives_new_gate_and_other_hosts_can_proceed(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=RequestGate(folder,delay=0);until=time.time()+3600
            with gate.enter('https://first.example/private?token=secret'):
                gate.defer('https://first.example/private?token=secret',until,'https://redirect.example/secret')
            restarted=RequestGate(folder,delay=0)
            for host in ('first.example','redirect.example'):
                with self.assertRaises(NetworkDeferred) as error:
                    with restarted.enter('https://'+host):self.fail('deferred request entered')
                self.assertEqual(error.exception.retry_at,until)
            with restarted.enter('https://unrelated.example'):pass
            state=(Path(folder)/'state.json').read_text()
            self.assertNotIn('secret',state);self.assertNotIn('private',state)

    def test_nested_transport_is_reentrant(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=RequestGate(folder,delay=0)
            with gate.enter('https://first.example'):
                with gate.enter('https://second.example'):
                    gate.defer('https://second.example',time.time()+10)
            with self.assertRaises(NetworkDeferred):
                with gate.enter('https://second.example'):pass

    def test_processes_cannot_overlap_even_for_different_redirect_chains(self):
        ctx=multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as folder:
            queue=ctx.Queue();workers=[ctx.Process(target=occupy,args=(folder,f'host{i}.example',queue)) for i in range(3)]
            for process in workers:process.start()
            try:
                for process in workers:
                    process.join(8);self.assertEqual(process.exitcode,0)
                intervals=sorted(queue.get(timeout=2) for _ in workers)
                self.assertTrue(all(a[1]<=b[0] for a,b in zip(intervals,intervals[1:])))
            finally:
                for process in workers:
                    if process.is_alive():process.terminate();process.join(2)
                queue.close()

    def test_killed_owner_releases_kernel_lock(self):
        ctx=multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as folder:
            ready=ctx.Event();queue=ctx.Queue();first=ctx.Process(target=occupy,args=(folder,'first.example',queue,ready))
            first.start();self.assertTrue(ready.wait(5));first.terminate();first.join(3)
            second=ctx.Process(target=occupy,args=(folder,'second.example',queue));second.start()
            try:
                second.join(5);self.assertEqual(second.exitcode,0)
            finally:
                if second.is_alive():second.terminate();second.join(2)
                queue.close()

    def test_invalid_and_corrupted_state_do_not_admit_request(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=RequestGate(folder,delay=0)
            with self.assertRaises(UngovernedRequest):
                with gate.enter('not a URL'):self.fail('invalid host entered')
            (Path(folder)/'state.json').write_text('{')
            with self.assertRaises(ValueError):
                with gate.enter('https://example.org'):self.fail('corrupt state entered')

    def test_expired_cooldown_and_minimum_spacing(self):
        with tempfile.TemporaryDirectory() as folder:
            gate=RequestGate(folder,delay=0.08)
            with gate.enter('https://example.org'):gate.defer('https://example.org',time.time()-1)
            finished=time.time()
            with gate.enter('https://example.org'):
                self.assertGreaterEqual(time.time()-finished,0.065)
