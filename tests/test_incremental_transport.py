import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import requests
from delivery.health.incremental_transport import install


class TransportTests(unittest.TestCase):
    def test_single_attempt_uses_gate_records_body_and_disallows_redirects(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Mock();response=Mock(status_code=200,content=b'{}',url='https://source.example/list',is_redirect=False)
            c._session.request.return_value=response;native=c._session.request;log=[]
            gate=Mock();gate.enter.side_effect=lambda url:contextlib.nullcontext()
            with patch('delivery.health.incremental_transport.RequestGate',return_value=gate):
                install(c,tmp,tmp,log);self.assertIs(c._session.request('GET',response.url),response)
            native.assert_called_once_with('GET',response.url,allow_redirects=False)
            gate.enter.assert_called_once_with(response.url)
            self.assertEqual(log[0]['status'],200);self.assertTrue((Path(tmp)/log[0]['body']).exists())

    def test_failed_wire_attempt_is_counted_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Mock();native=c._session.request;native.side_effect=requests.Timeout()
            log=[];gate=Mock();gate.enter.side_effect=lambda url:contextlib.nullcontext()
            with patch('delivery.health.incremental_transport.RequestGate',return_value=gate):
                install(c,tmp,tmp,log)
                with self.assertRaises(requests.Timeout):c._session.request('GET','https://source.example')
            native.assert_called_once();self.assertEqual(len(log),1);self.assertEqual(log[0]['error_type'],'Timeout')

    def test_rate_limit_defers_shared_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Mock();response=Mock(status_code=429,content=b'',url='https://source.example',headers={'Retry-After':'120'})
            response.raise_for_status.side_effect=requests.HTTPError();c._session.request.return_value=response
            log=[];gate=Mock();gate.enter.side_effect=lambda url:contextlib.nullcontext()
            with patch('delivery.health.incremental_transport.RequestGate',return_value=gate):
                install(c,tmp,tmp,log)
                with self.assertRaises(requests.HTTPError):c._session.request('GET',response.url)
            gate.defer.assert_called_once();self.assertEqual(log[0]['status'],429)

if __name__=='__main__':unittest.main()
