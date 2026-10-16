import json
import unittest
from pathlib import Path

from crawler.enumeration.plan import site_plan

ROOT = Path(__file__).resolve().parent.parent / "docs" / "ntrs-undated"
SID = "ntrs-nasa-gov-search"


class NtrsSecondPassPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ROOT / "plan-second-pass.json").read_text())
        cls.seg = cls.raw["sites"][SID]["segments"]
        cls.by = {n["key"]: n for n in cls.seg["nodes"]}

    def test_plan_is_accepted_by_the_plan_validator(self):
        site_plan(self.raw, SID, registered=[SID])

    def test_draft_is_still_refused(self):
        draft = json.loads((ROOT / "plan-second-pass.draft.json").read_text())
        with self.assertRaises(Exception):
            site_plan(draft, SID, registered=[SID])

    def test_child_filters_contain_every_parent_filter(self):
        for node in self.seg["nodes"]:
            for child in node.get("children", []):
                cp = self.by[child]["params"]
                for k, v in node["params"].items():
                    if k.startswith("created[") and k in cp:
                        continue  # a narrower created window replaces the parent's
                    self.assertEqual(cp.get(k), v, (node["key"], child, k))

    def test_no_leaf_is_known_to_exceed_the_cap_and_big_days_are_split(self):
        for day in ("2013-08-12", "2013-09-06", "2026-10-12"):
            self.assertTrue(self.by[day].get("children"), day)
        self.assertTrue(self.by["2026-10-12/REPRINT"].get("children"))
        self.assertTrue(self.by["2013-09-06/CONFERENCE_PAPER/CDMS"].get("children"))

    def test_root_has_no_unmeasured_residual_node(self):
        self.assertEqual(self.by["all"]["children"], ["early", "normal"])
        self.assertNotIn("created-residual", self.by)

    def test_every_child_reference_exists_and_is_used_once(self):
        used = [c for n in self.seg["nodes"] for c in n.get("children", [])]
        self.assertEqual(len(used), len(set(used)))
        self.assertEqual(set(used) | {self.seg["root"]}, set(self.by))


if __name__ == "__main__":
    unittest.main()
