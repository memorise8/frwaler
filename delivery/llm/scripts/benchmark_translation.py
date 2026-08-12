#!/usr/bin/env python3
"""Read JSONL samples, call the endpoint, and write results without touching a DB."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.request
from pathlib import Path


def protected_tokens(text: str) -> list[str]:
    return sorted(set(re.findall(r"https?://[^\s]+|\b\d[\d,./:%-]*\b", text)))


def normalized_token(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="read-only JSONL with id and text")
    parser.add_argument("output", type=Path, help="new JSONL result path")
    parser.add_argument("--endpoint", default=os.getenv("LLM_ENDPOINT", "http://127.0.0.1:8088/v1/chat/completions"))
    parser.add_argument("--model", default=os.getenv("SERVED_MODEL_NAME", "qwen3-30b-a3b-instruct-2507"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--summary", type=Path, help="new aggregate JSON path without source text")
    args = parser.parse_args()
    token = os.environ.get("LLM_API_KEY")
    if not token:
        parser.error("LLM_API_KEY is required")
    if args.output.exists():
        parser.error("output already exists; refusing to overwrite")
    if args.summary and args.summary.exists():
        parser.error("summary already exists; refusing to overwrite")
    samples = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    samples = samples[: max(0, min(args.limit, 50))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    measurements = []
    with args.output.open("x", encoding="utf-8") as out:
        for item in samples:
            source = str(item["text"])
            payload = json.dumps({
                "model": args.model, "temperature": 0, "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": "Translate into Korean faithfully. Preserve names, numbers, dates, and URLs. Return only the translation."},
                    {"role": "user", "content": source},
                ],
            }).encode()
            request = urllib.request.Request(args.endpoint, payload, {
                "Authorization": f"Bearer {token}", "Content-Type": "application/json"
            })
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=600) as response:
                body = json.load(response)
            translation = body["choices"][0]["message"]["content"]
            protected = protected_tokens(source)
            missing = [value for value in protected if value not in translation]
            normalized_translation = normalized_token(translation)
            missing_normalized = [value for value in protected
                                  if normalized_token(value) not in normalized_translation]
            result = {"id": item.get("id"), "source": source, "translation": translation,
                      "language": item.get("language"), "length_bucket": item.get("length_bucket"),
                      "latency_seconds": round(time.monotonic() - started, 3), "usage": body.get("usage"),
                      "finish_reason": body["choices"][0].get("finish_reason"),
                      "protected_tokens": protected, "missing_protected_tokens": missing,
                      "missing_normalized_protected_tokens": missing_normalized}
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            measurements.append(result)
    if args.summary:
        latencies = [item["latency_seconds"] for item in measurements]
        usages = [item.get("usage") or {} for item in measurements]
        protected_count = sum(len(item["protected_tokens"]) for item in measurements)
        missing_count = sum(len(item["missing_protected_tokens"]) for item in measurements)
        missing_normalized_count = sum(len(item["missing_normalized_protected_tokens"]) for item in measurements)
        summary = {
            "sample_count": len(measurements),
            "languages": sorted({str(item["language"]) for item in measurements}),
            "length_buckets": {bucket: sum(item["length_bucket"] == bucket for item in measurements)
                               for bucket in sorted({str(item["length_bucket"]) for item in measurements})},
            "latency_seconds": {
                "min": min(latencies, default=0), "median": statistics.median(latencies) if latencies else 0,
                "max": max(latencies, default=0), "total": round(sum(latencies), 3),
            },
            "tokens": {
                "prompt": sum(int(item.get("prompt_tokens", 0)) for item in usages),
                "completion": sum(int(item.get("completion_tokens", 0)) for item in usages),
            },
            "protected_tokens": {"total": protected_count, "missing": missing_count,
                                 "exact_preservation_rate": 1 if protected_count == 0 else round(1 - missing_count / protected_count, 6),
                                 "missing_normalized": missing_normalized_count,
                                 "normalized_preservation_rate": 1 if protected_count == 0 else round(1 - missing_normalized_count / protected_count, 6)},
        }
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
