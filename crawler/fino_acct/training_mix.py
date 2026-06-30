from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import random
from typing import Final


DEFAULT_INPUT: Final[Path] = Path(
    "/data_raid/ruci_workspace/embedding-fino/synthetic_training/fsc_fss_kicpa_20260613_current/train_pairs_guarded.jsonl"
)
DEFAULT_OUTPUT: Final[Path] = Path(
    "/data_raid/ruci_workspace/embedding-fino/synthetic_training/fsc_fss_kicpa_20260613_current/kicpa_fsc_fss_mixed_train_pairs.jsonl"
)
DEFAULT_TOTAL: Final[int] = 12_000
DEFAULT_SEED: Final[int] = 20260614
TARGET_RATIOS: Final[dict[str, float]] = {
    "tax": 0.50,
    "acct": 0.35,
    "audit": 0.10,
    "regulation": 0.05,
}


@dataclass(frozen=True, slots=True)
class MixRow:
    row: dict[str, str | list[str]]
    group: str


def classify_domain(domain: str) -> str:
    if domain in {"kicpa:tax_materials", "kicpa:tax_books"}:
        return "tax"
    if domain in {
        "kicpa:audit_review_cases",
        "public:supervisory_case:AUDIT_REVIEW_CASE",
        "public:audit_quality_review:AUDITOR_REVIEW_RECOMMENDATION",
        "public:audit_quality_review:AUDIT_FIRM_QUALITY_REVIEW",
        "public:enforcement:FINANCIAL_STATEMENT_REVIEW",
    }:
        return "audit"
    if domain in {"public:better-fsc-go-kr-fsc_new", "public:policy:FSC_PRESS_RELEASE"}:
        return "regulation"
    return "acct"


def build_mix(input_path: Path, output_path: Path, *, total: int = DEFAULT_TOTAL, seed: int = DEFAULT_SEED) -> dict[str, int | dict[str, int]]:
    rng = random.Random(seed)
    grouped = _read_grouped(input_path)
    targets = _targets(total)
    selected: list[MixRow] = []
    deficit = 0
    for group, target in targets.items():
        rows = grouped.get(group, [])
        rng.shuffle(rows)
        take = min(target, len(rows))
        selected.extend(rows[:take])
        deficit += target - take
    if deficit > 0:
        selected.extend(_fill_deficit(grouped, selected, deficit, rng))
    rng.shuffle(selected)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for item in selected[:total]:
            handle.write(json.dumps(item.row, ensure_ascii=False) + "\n")
    summary = _summary(selected[:total], grouped, targets)
    output_path.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler.fino_acct.training_mix")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = build_mix(args.input, args.output, total=args.total, seed=args.seed)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _read_grouped(path: Path) -> dict[str, list[MixRow]]:
    grouped: dict[str, list[MixRow]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            domain = str(row.get("domain", ""))
            group = classify_domain(domain)
            grouped.setdefault(group, []).append(MixRow(row=row, group=group))
    return grouped


def _targets(total: int) -> dict[str, int]:
    targets = {group: int(total * ratio) for group, ratio in TARGET_RATIOS.items()}
    targets["acct"] += total - sum(targets.values())
    return targets


def _fill_deficit(grouped: dict[str, list[MixRow]], selected: list[MixRow], deficit: int, rng: random.Random) -> list[MixRow]:
    selected_ids = {str(item.row.get("id", "")) for item in selected}
    output: list[MixRow] = []
    for group in ("acct", "tax", "audit", "regulation"):
        candidates = [row for row in grouped.get(group, []) if str(row.row.get("id", "")) not in selected_ids]
        rng.shuffle(candidates)
        take = min(deficit - len(output), len(candidates))
        output.extend(candidates[:take])
        if len(output) >= deficit:
            break
    return output


def _summary(selected: list[MixRow], grouped: dict[str, list[MixRow]], targets: dict[str, int]) -> dict[str, int | dict[str, int]]:
    by_group = Counter(item.group for item in selected)
    by_domain = Counter(str(item.row.get("domain", "")) for item in selected)
    return {
        "total_rows": len(selected),
        "target_rows_by_group": targets,
        "selected_rows_by_group": dict(by_group),
        "available_rows_by_group": {group: len(rows) for group, rows in grouped.items()},
        "selected_rows_by_domain": dict(by_domain),
    }


if __name__ == "__main__":
    raise SystemExit(main())
