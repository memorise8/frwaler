#!/usr/bin/env python3
"""QA: AI summary quality evaluation.

Heuristic checks per row (cheap, no API calls):

  - length in [40, 700] characters
  - contains Korean (Hangul) characters
  - is not just a copy of abstract (Jaccard char-bigram overlap < 0.85)
  - sentence count in [2, 8]

Optional ``--judge gpt`` adds a per-row LLM critique using
``crawler/llm_providers.py`` style routing — sends abstract + summary,
asks for a JSON verdict {ok: bool, reason: str}.

Usage:
    python -m scripts.qa_summary_quality [--db PATH] [--site SITE_ID] [--sample N] [--judge gpt|gemini]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "papers.db"

sys.path.insert(0, str(ROOT))

HANGUL = re.compile(r"[가-힣]")
SENT_END = re.compile(r"[.!?…]\s")


def _char_bigrams(s: str) -> set:
    s = re.sub(r"\s+", " ", s).strip()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _heuristic_check(row: sqlite3.Row) -> tuple[bool, list[str]]:
    summary = (row["summary"] or "").strip()
    abstract = (row["abstract"] or "").strip()
    issues: list[str] = []

    if not summary:
        return False, ["empty"]

    n = len(summary)
    if n < 40:
        issues.append(f"too short ({n})")
    elif n > 700:
        issues.append(f"too long ({n})")

    if not HANGUL.search(summary):
        issues.append("no Hangul")

    sentences = [s for s in re.split(r"[.!?…]\s+", summary) if s.strip()]
    if len(sentences) < 2:
        issues.append(f"only {len(sentences)} sentence")
    elif len(sentences) > 8:
        issues.append(f"{len(sentences)} sentences (>8)")

    if abstract:
        sim = _jaccard(_char_bigrams(summary), _char_bigrams(abstract))
        if sim > 0.85:
            issues.append(f"too similar to abstract (jaccard={sim:.2f})")

    return (len(issues) == 0), issues


def _llm_judge(provider: str, abstract: str, summary: str, title: str) -> dict:
    """Return {'ok': bool, 'reason': str} via OpenAI/Gemini."""
    prompt = (
        "다음 한국어 행정 문서의 본문 요약 품질을 평가하세요. "
        "기준: 사실성·간결성·중복 없음. JSON 으로 답하세요.\n\n"
        f"제목: {title}\n\n"
        f"본문(또는 초록): {abstract[:2000]}\n\n"
        f"요약: {summary}\n\n"
        '{"ok": true|false, "reason": "<한국어 한 줄>"}'
    )
    try:
        if provider in ("gpt", "openai"):
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return {"ok": False, "reason": "no OPENAI_API_KEY"}
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            r = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=[
                    {"role": "system", "content": "Respond with JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_completion_tokens=200,
            )
            text = r.choices[0].message.content or ""
        else:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                return {"ok": False, "reason": "no GEMINI_API_KEY"}
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            cfg = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
                max_output_tokens=200,
            )
            r = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
                config=cfg,
            )
            text = r.text or ""
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
        return json.loads(text)
    except Exception as exc:
        return {"ok": False, "reason": f"judge error: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--site", default=None)
    parser.add_argument("--sample", type=int, default=30,
                        help="Random sample size (default 30)")
    parser.add_argument("--judge", choices=["gpt", "openai", "gemini", "google"],
                        default=None,
                        help="Run an additional LLM critique per sample")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    sql = ("SELECT id, site_id, title, abstract, summary FROM documents "
           "WHERE summary IS NOT NULL AND summary != ''")
    params: list = []
    if args.site:
        sql += " AND site_id = ?"
        params.append(args.site)
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("[info] no summarized documents to evaluate")
        return 0

    rng = random.Random(args.seed)
    sample = rows if len(rows) <= args.sample else rng.sample(list(rows), args.sample)

    n = 0
    h_ok = 0
    j_ok = 0
    j_run = 0
    issue_counts: dict[str, int] = {}
    failures: list[tuple[int, str, list[str], dict | None]] = []

    for r in sample:
        n += 1
        ok, issues = _heuristic_check(r)
        if ok:
            h_ok += 1
        else:
            for it in issues:
                issue_counts[it.split(" ")[0]] = issue_counts.get(it.split(" ")[0], 0) + 1

        verdict = None
        if args.judge:
            verdict = _llm_judge(args.judge, r["abstract"] or "", r["summary"] or "",
                                 r["title"] or "")
            j_run += 1
            if verdict.get("ok"):
                j_ok += 1

        if not ok or (verdict and not verdict.get("ok")):
            failures.append((r["id"], r["site_id"], issues, verdict))

    print(f"\nQA summary quality — db={db_path}")
    print(f"Population: {len(rows)}, sampled: {n}\n")
    print(f"Heuristic pass: {h_ok}/{n} ({h_ok * 100 // max(1, n)}%)")
    if args.judge:
        print(f"LLM judge ok:   {j_ok}/{j_run} ({j_ok * 100 // max(1, j_run)}%)")
    if issue_counts:
        print(f"\nTop issues:")
        for k, v in sorted(issue_counts.items(), key=lambda kv: -kv[1]):
            print(f"  - {k}: {v}")

    if failures:
        print(f"\nFailures (showing first 20):")
        for i, (doc_id, site, issues, verdict) in enumerate(failures[:20], 1):
            v = f" judge={verdict.get('reason', '')!r}" if verdict else ""
            print(f"  {i:2d}. #{doc_id} [{site}] {', '.join(issues) or 'judge-fail'}{v}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
