from pathlib import Path
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import dependencies_current, eligibility, reviewed_hold
from delivery.scripts.review_academie_incremental import chronology, SITE


class AcademieOrderTests(unittest.TestCase):
    def test_partial_dated_pager_does_not_prove_whole_scope_order(self):
        dated=[{'key':'file-a','date':'2026-10-08'},{'key':'file-b','date':'2026-10-07'}]
        self.assertEqual(chronology(dated)['inferred_order'],('newest_first','date'))
        whole=chronology(dated+[{'key':'editorial-slug','date':None}])
        self.assertEqual(whole['inferred_order'],('arbitrary','order_ambiguous'))
        self.assertEqual(whole['proof']['date']['missing'],1)

    def test_first_and_last_dates_cannot_hide_press_article_inversion(self):
        rows=[dict(key=str(i),date=d) for i,d in enumerate(['2025-08-26','2025-09-03','2024-06-06','2024-07-04','2022-05-19'])]
        result=chronology(rows)
        self.assertEqual(result['inferred_order'],('arbitrary','order_ambiguous'))
        self.assertEqual([v['index'] for v in result['descending_date_inversions']],[0,2])

    def test_reviewed_whole_scope_hold_keeps_baseline_false(self):
        result=eligibility(SITE,CRAWLERS[SITE])
        self.assertEqual(result['classification'],'full_rescan_required')
        self.assertIn('nonchronological',result['reason']);self.assertFalse(result['baseline_complete'])
        self.assertEqual(len(result['order_review']['dependency_source_sha256']),3)

    def test_changed_helper_invalidates_hold_even_if_native_import_is_unchanged(self):
        original=Path.read_bytes
        def altered(path):
            return b'changed helper' if path.name=='academie_source.py' else original(path)
        self.assertIsNotNone(reviewed_hold(SITE,CRAWLERS[SITE]))
        with patch.object(Path,'read_bytes',altered):
            self.assertIsNone(reviewed_hold(SITE,CRAWLERS[SITE]))
            self.assertNotIn('order_review',eligibility(SITE,CRAWLERS[SITE]))

    def test_deleted_helper_invalidates_prior_evidence(self):
        self.assertFalse(dependencies_current({'crawler/nonexistent_academie_helper.py':'0'*64}))


if __name__=='__main__':unittest.main()
