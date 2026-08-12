import unittest

from delivery.translation.quality import GATE_VERSION, evaluate_summary


class SummaryQualityTest(unittest.TestCase):
    def test_grounded_result_is_approved(self):
        source = (
            "정부는 2025년 기후 예산을 확대한다. 기후연구원이 정책 효과를 분석했다. "
            "지원 대상은 지역 기업이며 예산은 120억원이고 전환 사업을 함께 지원한다. "
            "세부 계획은 지역별 수요를 반영해 단계적으로 시행하고 결과를 공개한다."
        )
        summary = (
            "정부는 2025년 기후 예산을 확대한다. 기후연구원이 정책 효과를 분석했다. "
            "지원 대상은 지역 기업이며 예산은 120억원이고 전환 사업을 함께 지원한다. "
            "세부 계획은 지역별 수요를 반영해 단계적으로 시행하고 결과를 공개한다."
        )
        result = evaluate_summary(title="기후 예산", source_text=source, summary_text=summary,
                                  key_points=("정부는 2025년 기후 예산을 확대한다.",),
                                  institutions=("기후연구원",))
        self.assertEqual(GATE_VERSION, "summary-quality-v1")
        self.assertEqual(result.decision, "auto_approved")
        self.assertGreaterEqual(result.score, 95)
        self.assertEqual(result.evidence[0]["source_sentence_index"], 0)

    def test_foreign_source_low_similarity_requests_review(self):
        result = evaluate_summary(
            title="Climate policy", source_text="The agency expands climate policy support for local firms.",
            summary_text="정부 기관은 지역 기업을 위한 기후 정책 지원을 확대하고 현장 수요를 반영한다. 정책은 기업의 안정적인 전환을 돕는 것을 목표로 하며 관련 지원을 함께 제공한다. 구체적인 지원 방식과 적용 일정은 후속 계획에서 제시되고 단계별 결과도 공개될 예정이다.",
            key_points=("지역 기업을 위한 기후 정책 지원이 확대된다.",), institutions=(),
        )
        self.assertEqual(result.decision, "review_recommended")
        self.assertIn("low_evidence", result.reason_codes)

    def test_contract_and_grounding_failures_are_rejected(self):
        result = evaluate_summary(title="Policy", source_text="Agency budget is 10.",
                                  summary_text="```요약 99...```", key_points=("짧음",),
                                  institutions=("Invented Institute",))
        self.assertEqual(result.decision, "rejected")
        self.assertIn("forbidden_markup", result.reason_codes)
        self.assertIn("ungrounded_institution", result.reason_codes)
        self.assertIn("ungrounded_number", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
