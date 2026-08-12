"""Deterministic, content-safe quality gate for structured summaries."""
from __future__ import annotations

import re
from dataclasses import dataclass

GATE_VERSION = "summary-quality-v1"
DECISIONS = {"auto_approved", "review_recommended", "rejected"}


@dataclass(frozen=True)
class QualityResult:
    decision: str
    score: int
    reason_codes: tuple[str, ...]
    checks: dict[str, bool | int | float]
    evidence: tuple[dict[str, int], ...]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+|(?<=[다요함됨임음])\.(?=\s|$)", text.strip()) if part.strip()]


def _normalized(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


def _grams(text: str) -> set[str]:
    value = _normalized(text)
    return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def _similarity(left: str, right: str) -> int:
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0
    return round(100 * len(a & b) / len(a | b))


def _has_forbidden_markup(text: str) -> bool:
    return bool(re.search(r"<\/?think>|```|^\s{0,3}(?:#{1,6}\s|[-*+]\s)", text,
                          re.IGNORECASE | re.MULTILINE))


def _is_truncated(text: str) -> bool:
    stripped = text.rstrip()
    return stripped.endswith(("...", "…")) or not bool(re.search(r"[.!?。！？]$", stripped))


def evaluate_summary(*, title: str, source_text: str, summary_text: str,
                     key_points: tuple[str, ...] | list[str],
                     institutions: tuple[str, ...] | list[str]) -> QualityResult:
    summary_sentences = _sentences(summary_text)
    source_sentences = _sentences(source_text) or ([source_text.strip()] if source_text.strip() else [])
    normalized_sentences = [_normalized(item) for item in summary_sentences]
    normalized_points = [_normalized(item) for item in key_points]
    source_normalized = _normalized(f"{title}\n{source_text}")

    checks: dict[str, bool | int | float] = {
        "summary_has_korean": bool(re.search(r"[가-힣]", summary_text)),
        "summary_sentence_count_valid": 3 <= len(summary_sentences) <= 5,
        "summary_length_valid": 120 <= len(summary_text.strip()) <= 2000,
        "key_point_count_valid": 1 <= len(key_points) <= 5,
        "key_points_korean": all(bool(re.search(r"[가-힣]", item)) for item in key_points),
        "key_point_lengths_valid": all(10 <= len(item.strip()) <= 500 for item in key_points),
        "markup_absent": not _has_forbidden_markup("\n".join((summary_text, *key_points))),
        "summary_not_repeated": len(normalized_sentences) == len(set(normalized_sentences)),
        "key_points_not_repeated": len(normalized_points) == len(set(normalized_points)),
        "output_not_truncated": not _is_truncated(summary_text),
        "institutions_grounded": all(bool(_normalized(item)) and _normalized(item) in source_normalized for item in institutions),
        "summary_sentence_count": len(summary_sentences),
        "summary_chars": len(summary_text.strip()),
        "key_point_count": len(key_points),
    }

    evidence = []
    for index, point in enumerate(key_points):
        similarities = [_similarity(point, sentence) for sentence in source_sentences]
        best = max(range(len(similarities)), key=similarities.__getitem__) if similarities else -1
        evidence.append({"key_point_index": index, "source_sentence_index": best,
                         "similarity": similarities[best] if best >= 0 else 0})
    evidence_average = round(sum(item["similarity"] for item in evidence) / len(evidence)) if evidence else 0
    checks["evidence_average"] = evidence_average

    source_numbers = set(re.findall(r"(?<!\w)\d[\d,.:%+-]*(?!\w)", source_text))
    output_numbers = set(re.findall(r"(?<!\w)\d[\d,.:%+-]*(?!\w)", "\n".join((summary_text, *key_points))))
    numeric_grounding = 100 if not output_numbers else round(100 * len(output_numbers & source_numbers) / len(output_numbers))
    checks["numeric_grounding"] = numeric_grounding

    hard_codes = []
    code_by_check = {
        "summary_has_korean": "summary_not_korean",
        "summary_sentence_count_valid": "invalid_sentence_count",
        "summary_length_valid": "invalid_summary_length",
        "key_point_count_valid": "invalid_key_point_count",
        "key_points_korean": "key_point_not_korean",
        "key_point_lengths_valid": "invalid_key_point_length",
        "markup_absent": "forbidden_markup",
        "summary_not_repeated": "repeated_summary_sentence",
        "key_points_not_repeated": "repeated_key_point",
        "output_not_truncated": "truncated_output",
        "institutions_grounded": "ungrounded_institution",
    }
    for check, code in code_by_check.items():
        if not checks[check]:
            hard_codes.append(code)
    if numeric_grounding < 100:
        hard_codes.append("ungrounded_number")

    format_score = 100 if not hard_codes else 0
    institution_score = 100 if checks["institutions_grounded"] else 0
    score = round(evidence_average * .5 + numeric_grounding * .25 + institution_score * .15 + format_score * .10)
    reasons = list(hard_codes)
    if not hard_codes and score < 95:
        reasons.append("low_evidence")
    decision = "rejected" if hard_codes else ("auto_approved" if score >= 95 else "review_recommended")
    return QualityResult(decision, score, tuple(reasons), checks, tuple(evidence))
