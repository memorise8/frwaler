"""Durable list discovery followed by oldest-first delivery.

An adapter must distinguish a validated source end from a timeout/empty DOM,
provide stable identities and a way to read the newest end. No completion is
inferred from a job returning, a limit, a saved count, or known documents.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
import uuid


class UnsupportedSource(ValueError):
    """A source contract no longer supports safe incremental traversal."""


@dataclass
class Entry:
    key: str
    payload: dict
    date: str = ""
    pinned: bool = False


@dataclass
class Page:
    entries: list[Entry]
    next_cursor: dict | None
    exhausted: bool = False
    evidence: str = ""


@dataclass
class Policy:
    known_run: int = 50
    overlap_pages: int = 1
    full_check_days: float = 30
    max_pages: int = 200
    wall_seconds: float = 1500

    def __post_init__(self):
        if (self.known_run < 1 or self.overlap_pages < 1 or self.max_pages < 1
                or self.wall_seconds <= 0 or self.full_check_days < 0):
            raise ValueError("invalid incremental policy")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """Uses the job's DB, not a per-process cache; supports isolated SQLite tests."""
    def __init__(self, conn, site):
        self.conn, self.site = conn, site
        self.sqlite = isinstance(conn, sqlite3.Connection)

    def execute(self, sql, args=()):
        return self.conn.execute(sql if self.sqlite else sql.replace("?", "%s"), args)

    def row(self, cursor):
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row) if hasattr(row, "keys") else dict(zip([d[0] for d in cursor.description], row))

    def state(self):
        row = self.row(self.execute("SELECT state FROM crawl_incremental_state WHERE site_id=?", (self.site,)))
        return json.loads(row["state"]) if row else {}

    def checkpoint(self, state):
        self.execute("""INSERT INTO crawl_incremental_state(site_id,state) VALUES(?,?)
          ON CONFLICT(site_id) DO UPDATE SET state=excluded.state""", (self.site, encoded(state)))
        self.conn.commit()

    def known(self, key):
        return self.row(self.execute("SELECT * FROM crawl_incremental_known WHERE site_id=? AND item_key=?", (self.site, key)))

    def pending(self, generation, reverse, basis=None):
        order = "DESC" if reverse else "ASC"
        ordering = ("date_value ASC,position ASC" if basis == "date" else "id_value ASC,position ASC" if basis == "id" else f"position {order}")
        return self.row(self.execute(f"""SELECT * FROM crawl_incremental_pending
          WHERE site_id=? AND generation=? ORDER BY {ordering},item_key LIMIT 1""", (self.site, generation)))


def init_schema(conn):
    # Explicit migration only. Called by delivery schema migration and test setup.
    for sql in (
        "CREATE TABLE IF NOT EXISTS crawl_incremental_state(site_id TEXT PRIMARY KEY,state TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS crawl_incremental_pending(
          site_id TEXT NOT NULL,generation TEXT NOT NULL,item_key TEXT NOT NULL,
          position BIGINT NOT NULL,payload TEXT NOT NULL,date_value DOUBLE PRECISION,id_value NUMERIC,
          PRIMARY KEY(site_id,generation,item_key))""",
        """CREATE INDEX IF NOT EXISTS idx_incremental_pending_position
          ON crawl_incremental_pending(site_id,generation,position)""",
        """CREATE INDEX IF NOT EXISTS idx_incremental_pending_date
          ON crawl_incremental_pending(site_id,generation,date_value,position)""",
        """CREATE INDEX IF NOT EXISTS idx_incremental_pending_id
          ON crawl_incremental_pending(site_id,generation,id_value,position)""",
        """CREATE TABLE IF NOT EXISTS crawl_incremental_known(
          site_id TEXT NOT NULL,item_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
          last_seen TEXT NOT NULL,missing INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(site_id,item_key))""",
        """CREATE INDEX IF NOT EXISTS idx_incremental_known_seen
          ON crawl_incremental_known(site_id,last_seen)""",
        """CREATE TABLE IF NOT EXISTS crawl_incremental_changes(
          site_id TEXT NOT NULL,generation TEXT NOT NULL,item_key TEXT NOT NULL,
          kind TEXT NOT NULL,payload TEXT,PRIMARY KEY(site_id,generation,item_key,kind))""",
    ):
        conn.execute(sql)
    conn.commit()


def _date(value):
    try:
        # Partial years/months do not establish a chronological boundary.
        if len(value) < 10:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def observe_order(proof, entries, numeric_ids):
    """Constant-memory monotonic evidence across *all* pages, excluding pins."""
    for item in entries:
        if item.pinned:
            continue
        for name, val in (("date", _date(item.date)),
                          ("id", int(item.key) if numeric_ids and item.key.isdecimal() else None)):
            p = proof.setdefault(name, {"first": None, "last": None, "up": 0, "down": 0, "missing": 0})
            if val is None:
                p["missing"] += 1
                continue
            if p["first"] is None:
                p["first"] = val
            if p["last"] is not None:
                p["up"] += val > p["last"]
                p["down"] += val < p["last"]
            p["last"] = val


def infer_order(proof):
    candidates = []
    for name, p in proof.items():
        if p["missing"] or p["first"] == p["last"]:
            continue
        if p["up"] and not p["down"]:
            candidates.append(("oldest_first", name))
        if p["down"] and not p["up"]:
            candidates.append(("newest_first", name))
    if not candidates or len({p[0] for p in candidates}) != 1:
        return "arbitrary", "order_ambiguous"
    return candidates[0]


class Collector:
    def __init__(self, crawler, adapter, policy=None):
        self.crawler, self.adapter = crawler, adapter
        self.policy = policy or Policy()
        self.store = Store(crawler._conn, crawler.site_id)
        self.stats = {"list_pages": 0, "http_requests": 0, "detail_requests": 0, "listed": 0,
                      "saved_new": 0, "save_attempts": 0, "stop_reason": None}

    def run(self, limit=None):
        c, a, db, p = self.crawler, self.adapter, self.store, self.policy
        started = time.monotonic()
        state = db.state()
        # Scope/adapter versions are part of the contract. Never reuse a baseline
        # for a changed query, identity mapping or parser.
        if state and state.get("contract") != a.contract:
            raise ValueError("incremental contract changed; explicit state reset required")
        if not state or state.get("phase") == "idle":
            now = time.time()
            due = (p.full_check_days > 0 and now - state.get("last_full_at", 0) >= p.full_check_days * 86400)
            full = (not state.get("baseline_complete") or c.delivery_mode in ("full", "backfill")
                    or state.get("support") != "supported" or due)
            previous = dict(state)
            state.update(contract=a.contract, generation=uuid.uuid4().hex, phase="discover",
                         scan="full" if full else "incremental", cursor=None, position=0,
                         proof={}, streak=0, overlap=None, discovery_exhausted=False, delivered=0,
                         candidate_baseline=None, stop_reason=None)
            # Previous completed baseline remains valid until a new full scan
            # AND its delivery queue finish successfully.
            state["scan_direction"] = "native" if full else "newest"
            state["previous_order"] = previous.get("order", "arbitrary")
            db.checkpoint(state)

        c.delivery_incremental_pending = True
        c.DELIVERY_STOP_ON_KNOWN = False  # list phase owns early stopping

        def expired():
            c._check_cancelled()
            return time.monotonic() - started >= p.wall_seconds

        def checkpoint():
            self.stats["http_requests"] = getattr(a, "http_requests", 0)
            state["last_run"] = dict(self.stats)
            db.checkpoint(state)
            c._advance_cursor({"version": "incremental-v1", "generation": state["generation"],
                               "phase": state["phase"], "position": state["position"],
                               "delivered": state.get("delivered", 0)},
                              items_done=1)
            c.delivery_incremental_state = dict(state)

        try:
            if state["phase"] == "discover":
                # Durable keyset adapters can resume their cursor. Offset/HTML
                # adapters must validate their own anchors before accepting one.
                for _ in range(p.max_pages):
                    if expired():
                        break
                    self.stats["list_pages"] += 1
                    page = a.fetch(state["cursor"], state["scan_direction"])
                    if not isinstance(page, Page) or (page.exhausted and not page.evidence):
                        raise ValueError("unverified source exhaustion")
                    if not page.exhausted and (page.next_cursor is None or page.next_cursor == state["cursor"]):
                        raise ValueError("listing cursor did not advance")
                    keys = [e.key for e in page.entries]
                    if any(not isinstance(k, str) or not k.strip() for k in keys) or len(keys) != len(set(keys)):
                        raise UnsupportedSource("unstable_list_identities")
                    if not page.entries and not page.exhausted:
                        raise ValueError("empty listing without exhaustion evidence")
                    ordinary = [e for e in page.entries if not e.pinned]
                    all_known = bool(ordinary)
                    found_new = False
                    for entry in page.entries:
                        self.stats["listed"] += 1
                        known = db.known(entry.key)
                        exists = bool(known) and a.exists(entry)
                        if not entry.pinned:
                            state["streak"] = state["streak"] + 1 if exists else 0
                            all_known = all_known and exists
                        if not exists:
                            found_new = True
                        # Full checks stage known entries too, to inspect edits.
                        if state["scan"] == "full" or not exists:
                            db.execute("""INSERT INTO crawl_incremental_pending
                              (site_id,generation,item_key,position,payload,date_value,id_value) VALUES(?,?,?,?,?,?,?)
                              ON CONFLICT(site_id,generation,item_key) DO NOTHING""",
                                       (c.site_id, state["generation"], entry.key, state["position"], encoded(entry.payload), _date(entry.date),
                                        int(entry.key) if a.numeric_ids and entry.key.isdecimal() else None))
                        if known:
                            db.execute("UPDATE crawl_incremental_known SET last_seen=?,missing=0 WHERE site_id=? AND item_key=?",
                                       (state["generation"], c.site_id, entry.key))
                        state["position"] += 1
                    observe_order(state["proof"], page.entries, a.numeric_ids)
                    if state["scan"] == "incremental":
                        order_proof = state["proof"].get(state.get("order_evidence"), {})
                        if order_proof.get("up") or order_proof.get("missing"):
                            raise UnsupportedSource("newest_listing_order_changed")
                    if state["candidate_baseline"] is None and ordinary:
                        state["candidate_baseline"] = {"id": ordinary[0].key, "date": ordinary[0].date}
                    if ordinary:
                        state["last_item"] = {"id": ordinary[-1].key, "date": ordinary[-1].date}
                    boundary = False
                    if state["scan"] == "incremental":
                        # A new item in the overlap invalidates the tentative
                        # boundary. Always consume another complete overlap page.
                        if found_new:
                            state["overlap"] = None
                        elif state["overlap"] is not None:
                            state["overlap"] -= 1
                            boundary = state["overlap"] <= 0
                        if state["overlap"] is None and (all_known or state["streak"] >= p.known_run):
                            state["overlap"] = p.overlap_pages
                    state["cursor"] = page.next_cursor
                    if page.exhausted or boundary:
                        state["discovery_exhausted"] = page.exhausted
                        state["stop_reason"] = "source_exhausted" if page.exhausted else "known_boundary_plus_overlap"
                        state["phase"] = "deliver"
                        if state["scan"] == "full":
                            order, reason = infer_order(state["proof"])
                            state["order"] = order
                            state["order_evidence"] = reason
                            state["support"] = "supported"
                            state["unsupported_reason"] = None
                            if state.get("force_full_rescan") or not a.stable_ids or order == "arbitrary" or not a.can_read_newest:
                                state["support"] = "full_rescan_required"
                                state["unsupported_reason"] = (state.get("force_full_rescan") or ("unstable_ids" if not a.stable_ids else
                                    "order_ambiguous" if order == "arbitrary" else "newest_end_unavailable"))
                        checkpoint()
                        break
                    checkpoint()

            if state["phase"] == "deliver":
                reverse = state["scan"] == "incremental" or state.get("order") == "newest_first"
                basis = state.get("order_evidence") if state.get("order") != "arbitrary" else None
                while not expired():
                    row = db.pending(state["generation"], reverse, basis)
                    if row is None:
                        break
                    if limit is not None and self.stats["saved_new"] >= limit:
                        break
                    payload = json.loads(row["payload"])
                    paper = a.materialize(payload)
                    self.stats["detail_requests"] = getattr(a, "detail_requests", 0)
                    if not paper:
                        raise ValueError("detail failed; pending item retained")
                    fingerprint = hashlib.sha256(encoded(paper).encode()).hexdigest()
                    old = db.known(row["item_key"])
                    self.stats["save_attempts"] += 1
                    c._save_paper(paper)
                    self.stats["saved_new"] += bool(c._last_save_created)
                    if old and old["fingerprint"] != fingerprint:
                        db.execute("""INSERT INTO crawl_incremental_changes
                          (site_id,generation,item_key,kind,payload) VALUES(?,?,?,'modified',?)
                          ON CONFLICT(site_id,generation,item_key,kind) DO NOTHING""",
                                   (c.site_id, state["generation"], row["item_key"], encoded(paper)))
                    db.execute("""INSERT INTO crawl_incremental_known(site_id,item_key,fingerprint,last_seen,missing)
                      VALUES(?,?,?,?,0) ON CONFLICT(site_id,item_key) DO UPDATE SET
                      fingerprint=excluded.fingerprint,last_seen=excluded.last_seen,missing=0""",
                               (c.site_id, row["item_key"], fingerprint, state["generation"]))
                    db.execute("DELETE FROM crawl_incremental_pending WHERE site_id=? AND generation=? AND item_key=?",
                               (c.site_id, state["generation"], row["item_key"]))
                    state["delivered"] += 1
                    checkpoint()
                if db.pending(state["generation"], reverse, basis) is None:
                    if state["scan"] == "full" and state["discovery_exhausted"]:
                        db.execute("""INSERT INTO crawl_incremental_changes(site_id,generation,item_key,kind,payload)
                          SELECT site_id,?,item_key,'missing',NULL FROM crawl_incremental_known
                          WHERE site_id=? AND last_seen<>? AND missing=0
                          ON CONFLICT(site_id,generation,item_key,kind) DO NOTHING""",
                                   (state["generation"], c.site_id, state["generation"]))
                        db.execute("UPDATE crawl_incremental_known SET missing=1 WHERE site_id=? AND last_seen<>?",
                                   (c.site_id, state["generation"]))
                        state["baseline_complete"] = True
                        state["last_full_at"] = time.time()
                        state["baseline"] = (state.get("last_item") if state.get("order") == "oldest_first"
                                             else state["candidate_baseline"])
                        c._mark_exhausted()
                    # An incremental boundary never certifies a full backfill.
                    state["phase"] = "idle"
                    c.delivery_incremental_pending = False
            self.stats["stop_reason"] = state.get("stop_reason") if state["phase"] == "idle" else state["phase"] + "_checkpoint"
            checkpoint()
            return self.stats["saved_new"]
        except BaseException as exc:
            db.conn.rollback()  # don't commit half a discovered page
            durable = db.state()
            if isinstance(exc, UnsupportedSource):
                durable.update(support="full_rescan_required", unsupported_reason=str(exc),
                               force_full_rescan=str(exc), scan="full", scan_direction="native",
                               cursor=None, proof={}, streak=0, overlap=None,
                               discovery_exhausted=False, candidate_baseline=None, stop_reason=None)
                # Keep staged items. A subsequent full pass can reconcile them;
                # the failed incremental prefix never becomes a new baseline.
                db.checkpoint(durable)
            c.delivery_incremental_state = durable
            raise
