"""Durable enumeration state: site status, segments, item and file ledgers.

A separate SQLite file (not the documents DB) so it works unchanged with the
sqlite and Postgres document backends. Document rows are written first and
checkpoints second; a crash in between only causes a re-read that the item
ledger and the documents dedup key absorb.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS enum_site(
      site_id TEXT PRIMARY KEY, plan_fp TEXT NOT NULL, strategy TEXT NOT NULL,
      status TEXT NOT NULL, stop_reason TEXT, state TEXT NOT NULL DEFAULT '{}',
      started_at TEXT, updated_at TEXT, completed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS enum_segment(
      site_id TEXT NOT NULL, plan_fp TEXT NOT NULL, segment_key TEXT NOT NULL,
      parent_key TEXT, ordinal INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
      cursor TEXT, reported_total INTEGER, listed INTEGER NOT NULL DEFAULT 0,
      evidence TEXT, detail TEXT NOT NULL DEFAULT '{}', updated_at TEXT,
      PRIMARY KEY(site_id, plan_fp, segment_key))""",
    """CREATE TABLE IF NOT EXISTS enum_item(
      site_id TEXT NOT NULL, item_key TEXT NOT NULL, status TEXT NOT NULL,
      seq_id INTEGER, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      url TEXT, segment_key TEXT, item_date TEXT, payload TEXT, updated_at TEXT,
      PRIMARY KEY(site_id, item_key))""",
    "CREATE INDEX IF NOT EXISTS idx_enum_item_status ON enum_item(site_id, status)",
    """CREATE TABLE IF NOT EXISTS enum_listed(
      site_id TEXT NOT NULL, plan_fp TEXT NOT NULL, item_key TEXT NOT NULL,
      segment_key TEXT NOT NULL, PRIMARY KEY(site_id, plan_fp, segment_key, item_key))""",
    """CREATE TABLE IF NOT EXISTS enum_file(
      site_id TEXT NOT NULL, seq_id INTEGER NOT NULL, item_key TEXT, url TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'pdf', status TEXT NOT NULL, size INTEGER, sha256 TEXT,
      pages INTEGER, content_type TEXT, attempts INTEGER NOT NULL DEFAULT 0,
      reason TEXT, path TEXT, updated_at TEXT,
      PRIMARY KEY(site_id, seq_id, url))""",
    """CREATE TABLE IF NOT EXISTS enum_host_hold(
      host TEXT PRIMARY KEY, reason TEXT NOT NULL, url TEXT, site_id TEXT, at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS enum_event(
      id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, at TEXT NOT NULL,
      kind TEXT NOT NULL, detail TEXT)""",
)

ITEM_FAILED = ("failed_permanent", "failed_transient", "failed_retries_exhausted")
SEGMENT_DONE = ("complete", "split", "mismatch", "cap_unsplittable")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.lock = threading.RLock()
        with self.lock:
            for sql in SCHEMA:
                self.conn.execute(sql)
            self.conn.commit()

    def close(self):
        self.conn.close()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # -- site ------------------------------------------------------------
    def site(self, site_id):
        row = self._one("SELECT * FROM enum_site WHERE site_id=?", (site_id,))
        if row:
            row["state"] = json.loads(row["state"] or "{}")
        return row

    def start_site(self, site_id, plan_fp, strategy, limits=None):
        """Begin a run. A changed plan starts a new segment generation; it never blocks.

        ``limits`` (the plan's page caps, kept out of plan_fp so a cap change does not restart every
        site): a segment whose cursor fixed its last page at the cap (end_kind "page_cap") resumes that
        cursor only under the same caps; with other (or unrecorded) caps the listing is read again from
        the start, so a raised cap reaches the pages the old cap cut off -- whatever stopped the last
        run. (Forward pagers check the cap against the live cursor and need no reopen.) Saved items
        are skipped through the item ledger."""
        current = self.site(site_id)
        if current and current["plan_fp"] == plan_fp:
            state = current["state"]
            reopen = (limits is not None and state.get("limits") != limits
                      and any((s["cursor"] or {}).get("end_kind") == "page_cap" for s in self.segments(site_id, plan_fp)))
            if reopen:
                self.event(site_id, "page_cap_reopened", {"from": state.get("limits"), "to": limits})
            if reopen or current["status"] in ("complete", "complete_with_mismatch", "complete_unverified",
                                               "complete_with_review"):
                # A completed enumeration re-runs from scratch as a fresh pass;
                # saved items are skipped through the item ledger. Segments found on a hub page
                # (ext:multi_listing) are remembered, so one missing later is still read.
                state = {k: state[k] for k in ("discovered_segments", "discovered_keys") if k in state}
                self._exec("DELETE FROM enum_segment WHERE site_id=? AND plan_fp=?", (site_id, plan_fp))
                self._exec("DELETE FROM enum_listed WHERE site_id=? AND plan_fp=?", (site_id, plan_fp))
        else:
            # Only the keys of discovered segments outlive a plan change (their parameters may not).
            kept = ((current or {}).get("state") or {}).get("discovered_keys")
            state = {"discovered_keys": kept} if kept else {}
            if current:
                self.event(site_id, "plan_changed", {"from": current["plan_fp"], "to": plan_fp})
        if limits is not None:
            state = dict(state, limits=limits)
        self._exec("""INSERT INTO enum_site(site_id,plan_fp,strategy,status,stop_reason,state,started_at,updated_at,completed_at)
          VALUES(?,?,?,?,NULL,?,?,?,NULL) ON CONFLICT(site_id) DO UPDATE SET plan_fp=excluded.plan_fp,
          strategy=excluded.strategy,status='running',stop_reason=NULL,state=excluded.state,
          started_at=excluded.started_at,updated_at=excluded.updated_at,completed_at=NULL""",
                   (site_id, plan_fp, strategy, "running", _dumps(state), now(), now()))
        return state

    def save_state(self, site_id, state):
        self._exec("UPDATE enum_site SET state=?,updated_at=? WHERE site_id=?", (_dumps(state), now(), site_id))

    def finish_site(self, site_id, status, stop_reason=None, state=None):
        completed = now() if status in ("complete", "complete_with_mismatch", "complete_with_review") else None
        if state is not None:
            self.save_state(site_id, state)
        self._exec("UPDATE enum_site SET status=?,stop_reason=?,updated_at=?,completed_at=? WHERE site_id=?",
                   (status, stop_reason, now(), completed, site_id))

    # -- segments ----------------------------------------------------------
    def segment(self, site_id, plan_fp, key):
        row = self._one("SELECT * FROM enum_segment WHERE site_id=? AND plan_fp=? AND segment_key=?",
                        (site_id, plan_fp, key))
        if row:
            row["cursor"] = json.loads(row["cursor"]) if row["cursor"] else None
            row["detail"] = json.loads(row["detail"] or "{}")
        return row

    def segments(self, site_id, plan_fp):
        rows = self._all("SELECT * FROM enum_segment WHERE site_id=? AND plan_fp=? ORDER BY ordinal, segment_key",
                         (site_id, plan_fp))
        for row in rows:
            row["cursor"] = json.loads(row["cursor"]) if row["cursor"] else None
            row["detail"] = json.loads(row["detail"] or "{}")
        return rows

    def ensure_segment(self, site_id, plan_fp, key, *, ordinal=0, parent=None):
        self._exec("""INSERT INTO enum_segment(site_id,plan_fp,segment_key,parent_key,ordinal,status,updated_at)
          VALUES(?,?,?,?,?,'pending',?) ON CONFLICT DO NOTHING""", (site_id, plan_fp, key, parent, ordinal, now()))
        return self.segment(site_id, plan_fp, key)

    def update_segment(self, site_id, plan_fp, key, **fields):
        allowed = {"status", "cursor", "reported_total", "evidence", "detail", "parent_key", "ordinal"}
        if set(fields) - allowed:
            raise ValueError("unknown segment field")
        sets, args = [], []
        for name, value in fields.items():
            if name in ("cursor", "detail"):
                value = None if value is None else _dumps(value)
            sets.append(f"{name}=?")
            args.append(value)
        sets.append("updated_at=?")
        args.append(now())
        self._exec(f"UPDATE enum_segment SET {','.join(sets)} WHERE site_id=? AND plan_fp=? AND segment_key=?",
                   (*args, site_id, plan_fp, key))

    def mark_listed(self, site_id, plan_fp, segment_key, keys):
        """Record keys listed in this generation; returns the segment's unique listed count."""
        with self.lock:
            self.conn.executemany("INSERT INTO enum_listed(site_id,plan_fp,item_key,segment_key) VALUES(?,?,?,?) "
                                  "ON CONFLICT DO NOTHING", [(site_id, plan_fp, k, segment_key) for k in keys])
            count = self.conn.execute("SELECT COUNT(*) FROM enum_listed WHERE site_id=? AND plan_fp=? AND segment_key=?",
                                      (site_id, plan_fp, segment_key)).fetchone()[0]
            self.conn.execute("UPDATE enum_segment SET listed=?,updated_at=? WHERE site_id=? AND plan_fp=? AND segment_key=?",
                              (count, now(), site_id, plan_fp, segment_key))
            self.conn.commit()
            return count

    def listed_in_segment(self, site_id, plan_fp, segment_key, keys):
        """Keys among ``keys`` already listed in this segment (used for segment-local counting)."""
        if not keys:
            return set()
        marks = ",".join("?" * len(keys))
        rows = self._all(f"SELECT item_key FROM enum_listed WHERE site_id=? AND plan_fp=? AND segment_key=? "
                         f"AND item_key IN ({marks})", (site_id, plan_fp, segment_key, *keys))
        return {r["item_key"] for r in rows}

    def keys_in_several_segments(self, site_id, plan_fp, segments):
        """Number of keys listed in more than one of ``segments``."""
        if len(segments) < 2:
            return 0
        marks = ",".join("?" * len(segments))
        row = self._one(f"SELECT COUNT(*) AS n FROM (SELECT item_key FROM enum_listed WHERE site_id=? AND plan_fp=? "
                        f"AND segment_key IN ({marks}) GROUP BY item_key HAVING COUNT(DISTINCT segment_key)>1) t",
                        (site_id, plan_fp, *segments))
        return row["n"]

    def listed_count(self, site_id, plan_fp):
        return self._one("SELECT COUNT(DISTINCT item_key) AS n FROM enum_listed WHERE site_id=? AND plan_fp=?",
                         (site_id, plan_fp))["n"]

    # -- items -------------------------------------------------------------
    def item(self, site_id, key):
        return self._one("SELECT * FROM enum_item WHERE site_id=? AND item_key=?", (site_id, key))

    def known_keys(self, site_id, keys):
        if not keys:
            return set()
        marks = ",".join("?" * len(keys))
        rows = self._all(f"SELECT item_key FROM enum_item WHERE site_id=? AND item_key IN ({marks})", (site_id, *keys))
        return {r["item_key"] for r in rows}

    def record_item(self, site_id, key, status, *, seq_id=None, reason=None, url=None, segment=None,
                    date=None, payload=None, attempt=False):
        self._exec("""INSERT INTO enum_item(site_id,item_key,status,seq_id,reason,attempts,url,segment_key,item_date,payload,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(site_id,item_key) DO UPDATE SET status=excluded.status,
          seq_id=COALESCE(excluded.seq_id,enum_item.seq_id),reason=excluded.reason,
          attempts=enum_item.attempts+?,url=COALESCE(excluded.url,enum_item.url),
          segment_key=COALESCE(excluded.segment_key,enum_item.segment_key),
          item_date=COALESCE(excluded.item_date,enum_item.item_date),
          payload=COALESCE(excluded.payload,enum_item.payload),updated_at=excluded.updated_at""",
                   (site_id, key, status, seq_id, reason, 1 if attempt else 0, url, segment, date,
                    None if payload is None else _dumps(payload), now(), 1 if attempt else 0))

    def retryable_items(self, site_id, max_attempts):
        rows = self._all("SELECT * FROM enum_item WHERE site_id=? AND status='failed_transient' AND attempts<? "
                         "ORDER BY item_date, updated_at", (site_id, max_attempts))
        for row in rows:
            row["payload"] = json.loads(row["payload"]) if row["payload"] else None
        return rows

    def exhaust_items(self, site_id, max_attempts):
        self._exec("UPDATE enum_item SET status='failed_retries_exhausted',updated_at=? "
                   "WHERE site_id=? AND status='failed_transient' AND attempts>=?", (now(), site_id, max_attempts))

    REQUEUE_FORBIDDEN = ("identity_", "listing_identity_conflict")

    def requeue_failed(self, site_id, reason_prefixes):
        """Explicit reprocessing (after a fixed adapter/parser): failed items whose reason
        starts with a prefix get a fresh attempt budget; saved items are never touched.
        Identity conflicts/collisions are excluded: they need a data decision."""
        total = 0
        for prefix in reason_prefixes:
            if any(prefix.startswith(f) or f.startswith(prefix) for f in self.REQUEUE_FORBIDDEN):
                raise ValueError(f"identity failures cannot be requeued: {prefix}")
            cur = self._exec("UPDATE enum_item SET status='failed_transient', attempts=0, updated_at=? "
                             "WHERE site_id=? AND status IN ('failed_permanent','failed_retries_exhausted') "
                             "AND reason LIKE ?", (now(), site_id, prefix.replace("%", "") + "%"))
            total += cur.rowcount
        if total:
            self.event(site_id, "items_requeued", {"prefixes": list(reason_prefixes), "count": total})
        return total

    def item_counts(self, site_id):
        rows = self._all("SELECT status, reason, COUNT(*) AS n FROM enum_item WHERE site_id=? GROUP BY status, reason",
                         (site_id,))
        saved = sum(r["n"] for r in rows if r["status"] == "saved")
        failed = {}
        for r in rows:
            if r["status"] in ITEM_FAILED:
                key = f"{r['status']}:{r['reason']}"
                failed[key] = failed.get(key, 0) + r["n"]
        return saved, failed

    def oldest_saved(self, site_id):
        return self._one("SELECT item_key, item_date, url FROM enum_item WHERE site_id=? AND status='saved' "
                         "AND item_date IS NOT NULL AND item_date<>'' ORDER BY item_date LIMIT 1", (site_id,))

    # -- files -------------------------------------------------------------
    def enqueue_file(self, site_id, seq_id, url, *, item_key=None, role="pdf"):
        self._exec("""INSERT INTO enum_file(site_id,seq_id,item_key,url,role,status,updated_at)
          VALUES(?,?,?,?,?,'pending',?) ON CONFLICT DO NOTHING""", (site_id, seq_id, item_key, url, role, now()))

    def pending_files(self, site_id, max_attempts):
        return self._all("SELECT * FROM enum_file WHERE site_id=? AND (status='pending' OR "
                         "(status='failed_transient' AND attempts<?)) ORDER BY seq_id, url", (site_id, max_attempts))

    def update_file(self, site_id, seq_id, url, *, status, attempt=True, **fields):
        allowed = {"size", "sha256", "pages", "content_type", "reason", "path"}
        if set(fields) - allowed:
            raise ValueError("unknown file field")
        sets = ["status=?", "attempts=attempts+?", "updated_at=?"] + [f"{k}=?" for k in fields]
        self._exec(f"UPDATE enum_file SET {','.join(sets)} WHERE site_id=? AND seq_id=? AND url=?",
                   (status, 1 if attempt else 0, now(), *fields.values(), site_id, seq_id, url))

    def file_counts(self, site_id):
        rows = self._all("SELECT status, COUNT(*) AS n FROM enum_file WHERE site_id=? GROUP BY status", (site_id,))
        return {r["status"]: r["n"] for r in rows}

    def files(self, site_id):
        return self._all("SELECT * FROM enum_file WHERE site_id=? ORDER BY seq_id, url", (site_id,))

    # -- host holds (shared by every site id on the host) ------------------
    def hold_host(self, host, reason, url=None, site_id=None):
        self._exec("INSERT INTO enum_host_hold(host,reason,url,site_id,at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(host) DO NOTHING", (host, reason, url, site_id, now()))

    def host_hold(self, host):
        """Exact host, else a ``*.<suffix>`` entry for any parent domain (subdomains only)."""
        host = (host or "").lower()
        labels = host.split(".")
        candidates = [host] + ["*." + ".".join(labels[i:]) for i in range(1, len(labels))]
        for candidate in candidates:
            row = self._one("SELECT * FROM enum_host_hold WHERE host=?", (candidate,))
            if row:
                return row
        return None

    def seed_hold(self, host, reason):
        """Pre-register an access-restricted host (CLI --seed-holds). A host a person
        released (--release-host) is not seeded again; an existing hold is kept."""
        host = host.strip().lower()
        released = self._one("SELECT 1 AS x FROM enum_event WHERE site_id='*' AND kind='host_hold_released' "
                             "AND lower(json_extract(detail,'$.host'))=? LIMIT 1", (host,))
        if released:
            return False
        self.hold_host(host, f"seeded:{reason}")
        return True

    def release_host(self, host):
        """Only for a person who reviewed the refusal (CLI --release-host)."""
        self._exec("DELETE FROM enum_host_hold WHERE host=?", (host,))

    # -- events ------------------------------------------------------------
    def event(self, site_id, kind, detail=None):
        self._exec("INSERT INTO enum_event(site_id,at,kind,detail) VALUES(?,?,?,?)",
                   (site_id, now(), kind, _dumps(detail or {})))

    def events(self, site_id, kind=None):
        sql = "SELECT * FROM enum_event WHERE site_id=?" + (" AND kind=?" if kind else "") + " ORDER BY id"
        rows = self._all(sql, (site_id, kind) if kind else (site_id,))
        for row in rows:
            row["detail"] = json.loads(row["detail"] or "{}")
        return rows
