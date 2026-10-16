#!/usr/bin/env python3
"""Build plan-second-pass.json for NTRS from the count-only checks in counts/results*.jsonl.

Every node's params are absolute (not inherited). The runtime probes a node, and only when its
total is >= hidden_cap does it probe the children and require sum(children) == parent. This script
adds children where a measured total is large, and also for years >= 7000 (structure only, no
measured month counts), so growth cannot silently overflow the cap.

Usage: build-partition-tree.py [--check]   (writes plan-second-pass.json and partition-report.json)
"""
import calendar, json, sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAP = 9800
SAFETY_YEAR = 7000


def load(name):
    return [json.loads(l) for l in (HERE / "counts" / name).read_text().splitlines() if l.strip()]


r1, r2, r3, r4 = (load(f"results{i}.jsonl") for i in (1, 2, 3, 4))

# ---- measured totals keyed by the created range -------------------------------------------
created = {}
for r in r1:
    p = r.get("params")
    if p is not None and "total" in r and set(p) <= {"created[gte]", "created[lte]"}:
        created[(p.get("created[gte]"), p.get("created[lte]"))] = r["total"]
GLOBAL = next(r for r in r1 if r["label"] == "global-before")
TOTAL = GLOBAL["total"]
day_aggs = {r["label"].split()[0]: r for r in r1 if r.get("label", "").endswith("aggregations")}
combos = {(r["day"], r["stiType"]): r for r in r2 if r.get("label") == "combo"}
pubchecks = {(r["day"], r["stiType"], r["center"]): r for r in r4 if r.get("label") == "pub-check"}
windows = [r for r in r4 if r.get("label") == "window"]
leaf3 = {(r["day"], r["stiType"], r["center"]): r for r in r3 if r.get("label") == "leaf"}

pub_years = sorted(int(b["key_as_string"]) for b in GLOBAL["aggregations"]["published"]["buckets"])
PUB_LO, PUB_HI = 1900, 2027
assert pub_years[-1] == PUB_HI, pub_years[-3:]

nodes, children_of = [], {}


def add(key, params, children=None):
    n = {"key": key, "params": params}
    if children:
        n["children"] = children
    nodes.append(n)
    return key


def rng(a, b):
    return {"created[gte]": a, "created[lte]": b}


def month_range(y, m):
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


measured_year = {int(k[0][:4]): v for k, v in created.items() if k[0] and k[0].endswith("-01-01") and k[1].endswith("-12-31")}
assert len(measured_year) == 200 and sum(measured_year.values()) == TOTAL - 1

# the one record before 1900 plus everything 1900..2099: no residual (early + normal == global)
early = created.get((None, "1899-12-31"))
add("all", {}, ["early", "normal"])
add("early", {"created[lte]": "1899-12-31"})
years = [y for y in sorted(measured_year) if measured_year[y] > 0]
add("normal", rng("1900-01-01", "2099-12-31"), [f"{y}" for y in years])

big_days = sorted(d for d in day_aggs)
need_days = {}  # month key -> True when day children are required
for y in years:
    ylo, yhi = f"{y}-01-01", f"{y}-12-31"
    if measured_year[y] >= SAFETY_YEAR:
        months = [f"{y}-{m:02d}" for m in range(1, 13)]
        add(str(y), rng(ylo, yhi), months)
        for m in range(1, 13):
            lo, hi = month_range(y, m)
            mk = f"{y}-{m:02d}"
            total = created.get((lo, hi))
            if total is not None and total >= CAP:
                days = [f"{mk}-{d:02d}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]
                add(mk, rng(lo, hi), days)
                for d in range(1, calendar.monthrange(y, m)[1] + 1):
                    dk = f"{mk}-{d:02d}"
                    ds = f"{dk}"
                    if dk in day_aggs:
                        continue  # built below with its stiType children
                    add(dk, rng(ds, ds))
            else:
                add(mk, rng(lo, hi))
    else:
        add(str(y), rng(ylo, yhi))

# ---- days that are >= cap: stiType, then center / created window, then published year ------
leaf_report = []
for dk in big_days:
    agg = day_aggs[dk]
    day_total = created[(dk, dk)]
    buckets = agg["aggregations"]["stiType"]["buckets"]
    # a truncated aggregation (20-bucket cap) would sum to less than the day total, so the equality below is the proof
    assert sum(b["doc_count"] for b in buckets) == day_total, (dk, "stiType sum != day total")
    types = [b["key"] for b in buckets]
    add(dk, rng(dk, dk), [f"{dk}/{t}" for t in types])
    for b in buckets:
        t, n = b["key"], b["doc_count"]
        tk = f"{dk}/{t}"
        base = {**rng(dk, dk), "stiType": t}
        if n < CAP:
            add(tk, base)
            continue
        combo = combos.get((dk, t))
        assert combo, (dk, t, "no center evidence")
        if combo["centers"]:
            assert combo["centers_sum"] == n == combo["total"], (dk, t)
            centers = [c for c, _ in combo["centers"]]
            add(tk, base, [f"{tk}/{c}" for c in centers])
            for c, cn in combo["centers"]:
                ck = f"{tk}/{c}"
                cbase = {**base, "center": c}
                if cn < CAP:
                    add(ck, cbase)
                    continue
                ev = pubchecks.get((dk, t, c))
                assert ev and ev["ok"], (dk, t, c, "no published evidence")
                ys = [f"{ck}/pub<1900"] + [f"{ck}/pub{y}" for y in range(PUB_LO, PUB_HI + 1)] + [f"{ck}/pub>2027"]
                add(ck, cbase, ys)
                add(ys[0], {**cbase, "published[lte]": "1899-12-31"})
                for y in range(PUB_LO, PUB_HI + 1):
                    add(f"{ck}/pub{y}", {**cbase, "published[gte]": f"{y}-01-01", "published[lte]": f"{y}-12-31"})
                add(ys[-1], {**cbase, "published[gte]": "2028-01-01"})
                leaf_report.append({"leaf": ck, "total": cn, "split_by": "published year"})
        else:
            # no center values at all (2026-10-12): split by creation time instead
            ws = [w for w in windows if w["stiType"] == t]
            assert ws and sum(w["total"] for w in ws) == n, (dk, t, "windows do not cover the type")
            wkeys = [f"{tk}/t{w['lo'][11:16].replace(':', '')}" for w in ws]
            add(tk, base, wkeys)
            for w, wk in zip(ws, wkeys):
                wbase = {**base, "created[gte]": w["lo"], "created[lte]": w["hi"]}
                if w["total"] >= SAFETY_YEAR / 2:
                    mins = []
                    t0 = int(w["lo"][14:16])
                    for k in range(5):
                        mk = f"{wk}/m{t0 + k:02d}"
                        hhmm = w["lo"][:14] + f"{t0 + k:02d}"
                        mins.append(mk)
                    add(wk, wbase, mins)
                    for k in range(5):
                        hhmm = w["lo"][:14] + f"{t0 + k:02d}"
                        add(mins[k], {**base, "created[gte]": hhmm + ":00", "created[lte]": hhmm + ":59"})
                else:
                    add(wk, wbase)

# ---- assemble --------------------------------------------------------------------------------
draft = json.loads((HERE / "plan-second-pass.draft.json").read_text())
sid = "ntrs-nasa-gov-search"
seg = draft["sites"][sid]["segments"]
seg["nodes"] = nodes
seg["server_verified"] = True
for k in ("_draft_status",):
    draft.pop(k, None)
draft["_verification"] = (
    "server_verified rests on count-only requests of 2026-10-15 (counts/results1-4.jsonl): "
    "early+normal=global, 200 year totals, months of 2013/2020/2026, days of 2013-08/09 and 2026-10, "
    "stiType aggregation of each of 25 days >= cap, center aggregation of 9 (day,stiType) pairs, published "
    "aggregation of the 6 center leaves, 5-minute created windows of 2026-10-12. Years >= 7000 "
    "carry month children, and 2.4k near-cap windows carry minute children, without measured counts: the "
    "runtime sum check is their only verification."
)

# duplicate-key and reference check
keys = [n["key"] for n in nodes]
assert len(keys) == len(set(keys)), "duplicate node keys"
by = {n["key"]: n for n in nodes}
for n in nodes:
    for c in n.get("children", []):
        assert c in by, ("missing child", c)
(HERE / "plan-second-pass.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n")

leaves = [n for n in nodes if not n.get("children")]
report = {"nodes": len(nodes), "leaves": len(leaves), "split_nodes": len(nodes) - len(leaves),
          "unmeasured_structure": [n["key"] for n in nodes if n.get("children") and n["key"] in {str(y) for y in years} and n["key"] not in ("2013", "2020", "2026")],
          "published_split_leaves": leaf_report}
(HERE / "partition-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()}, ensure_ascii=False))
