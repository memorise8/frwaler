from pathlib import Path
import json

from crawler.fino_acct.training_mix import build_mix, classify_domain


def test_classify_domain_when_known_sources() -> None:
    assert classify_domain("kicpa:tax_materials") == "tax"
    assert classify_domain("kicpa:ifrs_cases") == "acct"
    assert classify_domain("kicpa:audit_review_cases") == "audit"
    assert classify_domain("public:better-fsc-go-kr-fsc_new") == "regulation"


def test_build_mix_when_regulation_pool_is_short_redistributes_to_acct(tmp_path: Path) -> None:
    input_path = tmp_path / "pairs.jsonl"
    output_path = tmp_path / "mixed.jsonl"
    rows = []
    for group, domain, count in [
        ("tax", "kicpa:tax_materials", 20),
        ("acct", "kicpa:ifrs_cases", 20),
        ("audit", "kicpa:audit_review_cases", 10),
        ("reg", "public:better-fsc-go-kr-fsc_new", 1),
    ]:
        for index in range(count):
            rows.append(
                {
                    "id": f"{group}-{index}",
                    "anchor": f"{group} 질문 {index}",
                    "positive": f"{group} 본문 {index}",
                    "hard_negatives": [f"{group} 음성 {index}"],
                    "domain": domain,
                    "bucket": "test",
                    "mix_source": "test",
                    "generator_tag": "test",
                    "positive_chunk_id": f"p-{group}-{index}",
                    "hard_negative_chunk_id": f"n-{group}-{index}",
                }
            )
    input_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    summary = build_mix(input_path, output_path, total=40, seed=1)
    mixed = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    selected = summary["selected_rows_by_group"]
    targets = summary["target_rows_by_group"]

    assert len(mixed) == 40
    assert isinstance(selected, dict)
    assert isinstance(targets, dict)
    assert selected["regulation"] == 1
    assert selected["acct"] > targets["acct"]
