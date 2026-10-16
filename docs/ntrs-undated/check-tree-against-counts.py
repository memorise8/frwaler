#!/usr/bin/env python3
"""Offline: compare plan-second-pass.json with the counts measured on 2026-10-15; estimate requests.
No network access."""
import json, math
from pathlib import Path
HERE = Path(__file__).resolve().parent
plan = json.loads((HERE / "plan-second-pass.json").read_text())
sid = "ntrs-nasa-gov-search"
seg = plan["sites"][sid]["segments"]
nodes = {n["key"]: n for n in seg["nodes"]}
CAP = seg["hidden_cap"]

def rows(name):
    return [json.loads(l) for l in (HERE / "counts" / name).read_text().split("\n") if l.strip()]

def canon(p): return json.dumps(sorted(p.items()))
measured = {}
def put(p, total):
    measured[canon(p)] = total
for name in ("results1.jsonl", "results2.jsonl"):
    for r in rows(name):
        if isinstance(r.get("params"), dict) and "total" in r and "aggregations" not in r:
            put(r["params"], r["total"])
        elif isinstance(r.get("params"), dict) and "aggregations" in r:
            put(r["params"], r["total"])
put({}, rows("results1.jsonl")[0]["total"])
for r in rows("results4.jsonl"):
    if r.get("label") == "window":
        put({"created[gte]": r["lo"], "created[lte]": r["hi"], "stiType": r["stiType"]}, r["total"])
    if r.get("label") == "pub-check":
        p = {"created[gte]": r["day"], "created[lte]": r["day"], "stiType": r["stiType"], "published[gte]": r["year"] + "-01-01", "published[lte]": r["year"] + "-12-31"}
        if r["center"]: p["center"] = r["center"]
        put(p, r["got"])

# derived: stiType single-day aggregates and centre aggregates give child totals
for r in rows("results1.jsonl"):
    if r.get("label", "").endswith("aggregations"):
        d = r["params"]["created[gte]"]
        for b in r["aggregations"]["stiType"]["buckets"]:
            put({"created[gte]": d, "created[lte]": d, "stiType": b["key"]}, b["doc_count"])
for r in rows("results2.jsonl"):
    if r.get("label") == "combo":
        for c, n in r["centers"]:
            put({"created[gte]": r["day"], "created[lte]": r["day"], "stiType": r["stiType"], "center": c}, n)

def mt(k): return measured.get(canon(nodes[k]["params"]))
ok = bad = unm = 0; problems = []
for k, n in nodes.items():
    ch = n.get("children")
    if not ch: continue
    pt = mt(k); cts = [mt(c) for c in ch]
    if pt is None or any(c is None for c in cts):
        unm += 1; continue
    if sum(cts) == pt: ok += 1
    else: bad += 1; problems.append((k, pt, sum(cts)))
print(f"split nodes: {ok} sum-verified from measurements, {bad} mismatched, {unm} not fully measured")
for p in problems[:10]: print("  MISMATCH", p)
unmeasured = [k for k, n in nodes.items() if n.get("children") and (mt(k) is None or any(mt(c) is None for c in n["children"]))]
print("not fully measured split nodes:", unmeasured)

# --- request estimate (upper-ish): every visited node is probed once; leaves are paged -----------
est = {}
def total(k):
    v = mt(k)
    return v
requests = 0; leaf_pages = 0; probes = 0
def visit(k, assumed=None):
    global requests, leaf_pages, probes
    n = nodes[k]; t = mt(k)
    if t is None: t = assumed or 0
    probes += 1
    if t >= CAP and n.get("children"):
        share = t // len(n["children"])
        for c in n["children"]: visit(c, share)
    else:
        leaf_pages += max(1, math.ceil(t / 100)) + 1  # +1: end-of-list confirmation
visit(seg["root"])
interval = plan["defaults"]["request_interval_s"]
req = probes + leaf_pages
print(f"probes {probes}, leaf page requests (incl. end check) {leaf_pages}, total ~{req}")
print(f"at {interval}s/request: ~{req*interval/3600:.1f} h before response time; plan limits max_requests={plan['defaults']['max_requests']}, max_runtime_s={plan['defaults']['max_runtime_s']} ({plan['defaults']['max_runtime_s']/3600:.0f} h)")
