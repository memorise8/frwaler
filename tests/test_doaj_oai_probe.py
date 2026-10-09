import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from delivery.health.doaj_oai_probe import parse_page, probe
from delivery.health.source_observer import HostGate


def body(identifier="one", token="opaque-token", *, deleted=False):
    header = f'<header status="deleted"><identifier>oai:doaj.org/article:{identifier}</identifier></header>' if deleted else f'<header><identifier>oai:doaj.org/article:{identifier}</identifier></header>'
    metadata = '' if deleted else '<metadata><dc:dc><dc:title>Title</dc:title><dc:description>Abstract</dc:description></dc:dc></metadata>'
    return (f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><ListRecords><record>{header}{metadata}</record><resumptionToken completeListSize="100">{token}</resumptionToken></ListRecords></OAI-PMH>').encode()


def response(data):
    return {"status": 200, "curl_exit": 0, "body_complete": True, "body": data, "headers": {}}


class DoajOaiProbeTests(unittest.TestCase):
    def test_token_only_continuation_and_redacted_result(self):
        fetch = Mock(side_effect=[response(body()), response(body("two", "second-token"))])
        result = probe(pages=2, fetch=fetch, gate=HostGate(delay=0))
        self.assertEqual(result["unique_identifiers_observed"], 2)
        query = parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)
        self.assertEqual(query, {"verb": ["ListRecords"], "resumptionToken": ["opaque-token"]})
        self.assertNotIn("opaque-token", json.dumps(result))
        self.assertFalse(result["full_collection_verified"])
        self.assertIsNone(result["enumerable_total"])

    def test_deleted_records_are_separate_from_active_metadata(self):
        data = parse_page(body(deleted=True))
        self.assertEqual(data["deleted_headers"], 1)
        self.assertEqual(data["field_presence"], {})
        self.assertEqual(len(data["id_hashes"]), 1)

    def test_oai_error_is_not_exhaustion(self):
        error = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badArgument">private diagnostic</error></OAI-PMH>'
        result = probe(fetch=Mock(return_value=response(error)), gate=HostGate(delay=0))
        self.assertEqual(result["state"], "oai_error")
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_repeated_ids_or_tokens_stop(self):
        for second, expected in ((body("one", "second"), "repeated_ids_between_pages"),
                                 (body("two"), "repeated_continuation_token")):
            fetch = Mock(side_effect=[response(body()), response(second)])
            result = probe(fetch=fetch, gate=HostGate(delay=0))
            self.assertEqual(result["state"], expected)
            self.assertEqual(fetch.call_count, 2)

    def test_terminal_page_is_not_full_coverage_certification(self):
        result = probe(fetch=Mock(return_value=response(body(token=""))), gate=HostGate(delay=0))
        self.assertEqual(result["state"], "source_end_signal_observed_requires_reconciliation")
        self.assertFalse(result["full_collection_verified"])

    def test_incomplete_and_wrong_xml_fail_closed(self):
        data = response(body()); data["body_complete"] = False
        self.assertEqual(probe(fetch=Mock(return_value=data))["state"], "request_failed_or_incomplete")
        for data in (b'<html/>', b'<!DOCTYPE foo><foo/>'):
            with self.assertRaises(ValueError):
                parse_page(data)
