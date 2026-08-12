#!/usr/bin/env python3
"""Read JSONL samples, call the endpoint, and write results without touching a DB."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="read-only JSONL with id and text")
    parser.add_argument("output", type=Path, help="new JSONL result path")
    parser.add_argument("--endpoint", default=os.getenv("LLM_ENDPOINT", "http://127.0.0.1:8088/v1/chat/completions"))
    parser.add_argument("--model", default=os.getenv("SERVED_MODEL_NAME", "qwen3-30b-a3b-instruct-2507"))
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    token = os.environ.get("LLM_API_KEY")
    if not token:
        parser.error("LLM_API_KEY is required")
    if args.output.exists():
        parser.error("output already exists; refusing to overwrite")
    samples = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    samples = samples[: max(0, min(args.limit, 50))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
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
            result = {"id": item.get("id"), "source": source, "translation": translation,
                      "latency_seconds": round(time.monotonic() - started, 3), "usage": body.get("usage")}
            out.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
