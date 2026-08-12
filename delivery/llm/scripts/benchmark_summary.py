#!/usr/bin/env python3
"""Benchmark structured Korean summaries from an ignored JSONL snapshot."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("input",type=Path);parser.add_argument("output",type=Path)
    parser.add_argument("--summary",type=Path,required=True);parser.add_argument("--limit",type=int,default=20)
    parser.add_argument("--endpoint",default=os.getenv("LLM_ENDPOINT","http://127.0.0.1:8088/v1/chat/completions"))
    parser.add_argument("--model",default=os.getenv("SERVED_MODEL_NAME","qwen3-30b-a3b-instruct-2507"))
    args=parser.parse_args();token=os.getenv("LLM_API_KEY")
    if not token: parser.error("LLM_API_KEY is required")
    if args.output.exists() or args.summary.exists(): parser.error("output already exists")
    rows=[json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    measurements=[];args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x",encoding="utf-8") as out:
        for item in rows[:max(0,min(args.limit,50))]:
            title,_,text=str(item["text"]).partition("\n\n")
            prompt=(f"Summarize the following {item.get('language','unknown')} document in Korean. "
              "Return one JSON object only with keys summary_ko, key_points, institutions. "
              "summary_ko must be 3 to 5 concise Korean sentences written in Hangul, even when the source is Chinese or Japanese. "
              "key_points must contain up to 5 Korean strings written in Hangul. "
              "institutions must contain only organization names explicitly present in the source and keep original spelling. "
              f"Do not invent facts or include markdown.\n\nTitle: {title}\n\nText: {text}")
            payload=json.dumps({"model":args.model,"temperature":0.1,"max_tokens":1200,
              "response_format":{"type":"json_object"},"messages":[{"role":"user","content":prompt}]}).encode()
            request=urllib.request.Request(args.endpoint,payload,{"Authorization":f"Bearer {token}","Content-Type":"application/json"})
            started=time.monotonic()
            with urllib.request.urlopen(request,timeout=600) as response: body=json.load(response)
            raw=body["choices"][0]["message"]["content"].strip();data=json.loads(raw)
            valid=(isinstance(data.get("summary_ko"),str) and bool(data["summary_ko"].strip()) and
                   isinstance(data.get("key_points"),list) and len(data["key_points"])<=5 and
                   isinstance(data.get("institutions"),list))
            result={"id":item.get("id"),"language":item.get("language"),"length_bucket":item.get("length_bucket"),
              "summary":data,"valid_contract":valid,"has_korean":bool(re.search("[가-힣]",str(data.get("summary_ko","")))),
              "thinking_marker":"<think>" in raw.lower(),"latency_seconds":round(time.monotonic()-started,3),
              "usage":body.get("usage"),"finish_reason":body["choices"][0].get("finish_reason")}
            out.write(json.dumps(result,ensure_ascii=False)+"\n");measurements.append(result)
    latencies=[r["latency_seconds"] for r in measurements];usages=[r.get("usage") or {} for r in measurements]
    aggregate={"sample_count":len(measurements),"valid_contract":sum(r["valid_contract"] for r in measurements),
      "with_korean":sum(r["has_korean"] for r in measurements),"thinking_markers":sum(r["thinking_marker"] for r in measurements),
      "finish_reasons":{reason:sum(str(r["finish_reason"])==reason for r in measurements) for reason in sorted({str(r["finish_reason"]) for r in measurements})},
      "latency_seconds":{"min":min(latencies,default=0),"median":statistics.median(latencies) if latencies else 0,
        "max":max(latencies,default=0),"total":round(sum(latencies),3)},
      "tokens":{"prompt":sum(int(u.get("prompt_tokens",0)) for u in usages),
        "completion":sum(int(u.get("completion_tokens",0)) for u in usages)}}
    args.summary.write_text(json.dumps(aggregate,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__": main()
