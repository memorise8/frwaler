import unittest

from delivery.scripts.summarize_structure_discovery import reviewed_tier, transitions, phase_metrics


class SummaryTests(unittest.TestCase):
    def test_parser_failure_is_not_promoted_to_source_impossibility(self):
        self.assertEqual(reviewed_tier({'measured_tier':'불가'},
            {'audited_status':'needs_review','issues':['redirect_or_parser_problem_not_final_inability']}), '낮음')

    def test_inconsistent_identity_downgrades_apparent_route(self):
        self.assertEqual(reviewed_tier({'measured_tier':'보통'},
            {'audited_status':'needs_review','issues':['scope_0:identity_or_order_changed_on_repeat']}), '불안정')

    def test_partial_transition_denominator_uses_only_processed_sites(self):
        result = transitions([{'initial_tier':'높음','reviewed_tier':'낮음'},
                              {'initial_tier':'낮음','reviewed_tier':'낮음'}])
        self.assertEqual(result['delta_among_processed']['낮음'], 1)
        self.assertEqual(result['changed_sites'], 1)
        self.assertEqual(sum(result['delta_among_processed'].values()), 0)

    def test_unrecognised_list_is_low_evidence_not_observed_instability(self):
        self.assertEqual(reviewed_tier({'measured_tier':'불안정'},
            {'audited_status':'needs_review','issues':['scope_0:oldest_identity_unresolved']}), '낮음')

    def test_phase_time_excludes_the_pause_and_counts_errors_as_responses(self):
        state = {'started_at':'2026-10-12T01:00:00+00:00','finished_at':'2026-10-12T01:02:00+00:00',
                 'workers':8,'status':'finished','scheduled':4,'finished':4}
        result = phase_metrics(state, [{'requests':[
            {'checked_at':'2026-10-12T00:30:00+00:00','status':200},
            {'checked_at':'2026-10-12T01:01:00+00:00','status':403},
            {'checked_at':'2026-10-12T01:01:00+00:00','status':None}]}])
        self.assertEqual(result['sites_per_minute'], 2)
        self.assertEqual(result['responses_received_for_completed_sites'], 1)


if __name__ == '__main__': unittest.main()
