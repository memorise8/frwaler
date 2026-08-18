# -*- coding: utf-8 -*-
"""Holds the deployment premise that the delivery runbook documents.

Eighteen GET endpoints carry no operator token -- the entire document corpus
is readable by anyone who can reach the port. What keeps that safe today is
one character in docker-compose.yml: the published port is bound to
127.0.0.1, not 0.0.0.0. If that binding ever loosens, the runbook's premise
is silently false, so it fails here instead.
"""
import os
import re
import unittest

COMPOSE = os.path.join(os.path.dirname(__file__), "..", "delivery", "docker-compose.yml")


class DeliveryComposeBindingTest(unittest.TestCase):
    def setUp(self):
        with open(COMPOSE, encoding="utf-8") as handle:
            self.compose = handle.read()

    def test_published_ports_bind_to_loopback_only(self):
        published = re.findall(r'^\s*-\s*"([^"]+:\d+|\$\{[^}]+\}:\d+|[^"]*)"\s*$',
                               self.compose, flags=re.MULTILINE)
        mappings = [entry for entry in published if re.search(r":\d+$", entry) and "->" not in entry]
        self.assertTrue(mappings, "no published port mappings found -- has the compose file moved?")
        for mapping in mappings:
            self.assertTrue(
                mapping.startswith("127.0.0.1:"),
                f"published port {mapping!r} is not bound to loopback; the runbook premise "
                "that GET endpoints are unreachable from outside no longer holds",
            )

    def test_the_backend_port_is_still_published_through_a_variable(self):
        # Documents the exact line the premise rests on, so a rename is visible.
        self.assertIn('"127.0.0.1:${BE_PORT:-8080}:3001"', self.compose)
        self.assertIn('"127.0.0.1:${FE_PORT:-3000}:3002"', self.compose)
