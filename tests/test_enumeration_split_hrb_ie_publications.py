import unittest

from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'hrb-ie-publications'


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'hrb-ie-publications'
