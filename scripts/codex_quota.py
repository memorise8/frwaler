# -*- coding: utf-8 -*-
"""codex_quota.py — Read Codex account quota from local logs_2.sqlite DBs.

Public API
----------
read_latest_quota(home)          -> QuotaSnapshot
pick_account(homes, ...)         -> (Path, QuotaSnapshot)
wait_until_reset(homes, ...)     -> None
format_status(homes)             -> str

CLI subcommands: status | pick | wait
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class QuotaSnapshot:
    home: Path
    plan_type: Optional[str]
    primary_used_percent: Optional[float]
    primary_window_minutes: Optional[int]
    primary_reset_at: Optional[int]       # unix epoch
    secondary_used_percent: Optional[float]
    secondary_window_minutes: Optional[int]
    secondary_reset_at: Optional[int]     # unix epoch
    limit_reached: bool
    measured_at: Optional[int]            # logs.ts
    account_email: Optional[str]


class QuotaExhausted(Exception):
    """Raised when all accounts are at or above max_percent."""

    def __init__(
        self,
        min_reset_at: int,
        min_reset_in_seconds: int,
        snapshots: list[QuotaSnapshot],
    ) -> None:
        self.min_reset_at = min_reset_at
        self.min_reset_in_seconds = min_reset_in_seconds
        self.snapshots = snapshots
        super().__init__(
            f"All accounts at >= max_percent. Earliest reset in {min_reset_in_seconds}s"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_email(home: Path) -> Optional[str]:
    """Extract email from <home>/auth.json id_token (JWT second segment)."""
    auth_path = home / "auth.json"
    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        id_token: str = data["tokens"]["id_token"]
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        # Base64url padding
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        return payload.get("email")
    except Exception:
        return None


def _parse_rate_limits_json(body: str) -> Optional[dict]:
    """Find the last 'codex.rate_limits' JSON object in a log body string."""
    marker = '{"type":"codex.rate_limits"'
    idx = body.rfind(marker)
    if idx == -1:
        return None
    try:
        return json.loads(body[idx:])
    except json.JSONDecodeError:
        # Try to find the end of the JSON object by scanning for balanced braces
        snippet = body[idx:]
        depth = 0
        end = 0
        for i, ch in enumerate(snippet):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                return json.loads(snippet[:end])
            except json.JSONDecodeError:
                pass
        return None


def _empty_snapshot(home: Path) -> QuotaSnapshot:
    """Return a snapshot with all-None usage fields (treat as fresh/unknown)."""
    return QuotaSnapshot(
        home=home,
        plan_type=None,
        primary_used_percent=None,
        primary_window_minutes=None,
        primary_reset_at=None,
        secondary_used_percent=None,
        secondary_window_minutes=None,
        secondary_reset_at=None,
        limit_reached=False,
        measured_at=None,
        account_email=_decode_email(home),
    )


def humanize_duration(seconds: int) -> str:
    """Convert seconds to human-readable string: 5d7h, 1h12m, 29m, 45s."""
    if seconds <= 0:
        return "0s"
    days = seconds // 86400
    remainder = seconds % 86400
    hours = remainder // 3600
    remainder = remainder % 3600
    minutes = remainder // 60
    secs = remainder % 60

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:  # don't show minutes when days present
        parts.append(f"{minutes}m")
    if secs and not days and not hours:
        parts.append(f"{secs}s")

    return "".join(parts) if parts else "0s"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_latest_quota(home: Path) -> QuotaSnapshot:
    """Read the most recent codex.rate_limits event from <home>/logs_2.sqlite.

    If logs_2.sqlite is missing, no rate_limits event found, or JSON parse
    fails, return a snapshot with None percents (treat as 'fresh / unknown').
    Always opens DB in read-only mode. Never raises for missing data.
    """
    home = Path(home).expanduser().resolve()
    email = _decode_email(home)

    db_path = home / "logs_2.sqlite"
    if not db_path.exists():
        snap = _empty_snapshot(home)
        snap.account_email = email
        return snap

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro&immutable=1", uri=True
        )
        try:
            row = conn.execute(
                "SELECT ts, feedback_log_body FROM logs "
                "WHERE feedback_log_body LIKE '%\"codex.rate_limits\"%' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        snap = _empty_snapshot(home)
        snap.account_email = email
        return snap

    if not row:
        snap = _empty_snapshot(home)
        snap.account_email = email
        return snap

    ts, body = row
    parsed = _parse_rate_limits_json(body)
    if not parsed:
        snap = _empty_snapshot(home)
        snap.account_email = email
        snap.measured_at = ts
        return snap

    try:
        rl = parsed.get("rate_limits") or {}
        primary = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}

        # limit_reached: True if top-level OR either sub-window says so
        limit_reached = bool(rl.get("limit_reached", False))
        if primary.get("limit_reached"):
            limit_reached = True
        if secondary.get("limit_reached"):
            limit_reached = True

        return QuotaSnapshot(
            home=home,
            plan_type=parsed.get("plan_type"),
            primary_used_percent=_to_float(primary.get("used_percent")),
            primary_window_minutes=_to_int(primary.get("window_minutes")),
            primary_reset_at=_to_int(primary.get("reset_at")),
            secondary_used_percent=_to_float(secondary.get("used_percent")),
            secondary_window_minutes=_to_int(secondary.get("window_minutes")),
            secondary_reset_at=_to_int(secondary.get("reset_at")),
            limit_reached=limit_reached,
            measured_at=ts,
            account_email=email,
        )
    except Exception:
        snap = _empty_snapshot(home)
        snap.account_email = email
        snap.measured_at = ts
        return snap


def _to_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(val) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _effective_percent(snap: QuotaSnapshot) -> float:
    """Return primary_used_percent, treating None as 0.0 (fresh/unknown)."""
    if snap.primary_used_percent is None:
        return 0.0
    return snap.primary_used_percent


def _default_on_warn(snap: QuotaSnapshot) -> None:
    reset_str = ""
    if snap.primary_reset_at is not None:
        remaining = snap.primary_reset_at - int(time.time())
        reset_str = f" (resets in {humanize_duration(max(0, remaining))})"
    print(
        f"⚠️  account {snap.account_email} ({snap.home}) at "
        f"{snap.primary_used_percent}% primary{reset_str}",
        file=sys.stderr,
    )


def pick_account(
    homes: list[Path],
    *,
    max_percent: float = 95.0,
    warn_percent: float = 90.0,
    on_warn: Optional[Callable[[QuotaSnapshot], None]] = None,
) -> tuple[Path, QuotaSnapshot]:
    """Select the home with the LOWEST primary_used_percent.

    - None percent (fresh / unknown) is treated as 0 for selection.
    - If ALL homes have primary_used_percent >= max_percent, raises QuotaExhausted.
    - If picked home's primary_used_percent >= warn_percent, calls on_warn once.
    """
    if not homes:
        raise ValueError("homes list is empty")

    snapshots = [read_latest_quota(Path(h)) for h in homes]

    # Filter out exhausted accounts
    available = [s for s in snapshots if _effective_percent(s) < max_percent]

    if not available:
        now = int(time.time())
        reset_ats = [
            s.primary_reset_at for s in snapshots if s.primary_reset_at is not None
        ]
        min_reset_at = min(reset_ats) if reset_ats else now + 3600
        min_reset_in_seconds = max(0, min_reset_at - now)
        raise QuotaExhausted(
            min_reset_at=min_reset_at,
            min_reset_in_seconds=min_reset_in_seconds,
            snapshots=snapshots,
        )

    # Pick lowest usage
    best = min(available, key=_effective_percent)

    # Warn if near limit
    if _effective_percent(best) >= warn_percent:
        warn_fn = on_warn if on_warn is not None else _default_on_warn
        warn_fn(best)

    return best.home, best


def wait_until_reset(
    homes: list[Path], *, poll_interval_seconds: int = 30
) -> None:
    """Block until at least one home is no longer >= 95% primary usage.

    Logs status to stderr every ~5 minutes.
    """
    homes = [Path(h).expanduser().resolve() for h in homes]
    last_log_time = 0.0

    while True:
        snapshots = [read_latest_quota(h) for h in homes]
        available = [s for s in snapshots if _effective_percent(s) < 95.0]
        if available:
            best = min(available, key=_effective_percent)
            print(
                f"Account {best.account_email} ({best.home}) now at "
                f"{best.primary_used_percent}% — proceeding.",
                file=sys.stderr,
            )
            return

        now = time.time()
        if now - last_log_time >= 300:
            last_log_time = now
            print("All accounts >= 95%. Waiting for reset...", file=sys.stderr)
            for s in snapshots:
                if s.primary_reset_at is not None:
                    remaining = max(0, s.primary_reset_at - int(now))
                    print(
                        f"  {s.account_email} ({s.home}): "
                        f"{s.primary_used_percent}% — resets in {humanize_duration(remaining)}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"  {s.account_email} ({s.home}): {s.primary_used_percent}% — reset unknown",
                        file=sys.stderr,
                    )

        time.sleep(poll_interval_seconds)


def format_status(homes: list[Path]) -> str:
    """Pretty multi-line table of all homes' quota status."""
    snapshots = [read_latest_quota(Path(h).expanduser().resolve()) for h in homes]

    now = int(time.time())

    def fmt_window(used_pct, window_min, reset_at) -> str:
        if used_pct is None:
            return "—   (no usage)"
        remaining = max(0, reset_at - now) if reset_at is not None else None
        reset_str = humanize_duration(remaining) if remaining is not None else "?"
        return f"{used_pct:>3.0f}% (resets {reset_str})"

    def short_home(home: Path) -> str:
        try:
            rel = "~/" + str(home.relative_to(Path.home()))
            return rel
        except ValueError:
            return str(home)

    lines = [
        f"{'HOME':<20} {'EMAIL':<35} {'PLAN':<6} {'5H WINDOW':<20} {'7D WINDOW':<22} {'LIMIT'}",
    ]

    for s in snapshots:
        home_str = short_home(s.home)
        email_str = s.account_email or "(unknown)"
        plan_str = s.plan_type or "?"
        primary_str = fmt_window(s.primary_used_percent, s.primary_window_minutes, s.primary_reset_at)
        secondary_str = fmt_window(s.secondary_used_percent, s.secondary_window_minutes, s.secondary_reset_at)
        limit_str = "yes" if s.limit_reached else "no"

        lines.append(
            f"{home_str:<20} {email_str:<35} {plan_str:<6} {primary_str:<20} {secondary_str:<22} {limit_str}"
        )

    return "Codex account quota status\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_HOMES = ["~/.codex-a", "~/.codex-b"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex_quota",
        description="Inspect Codex account quota from local logs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p_status = sub.add_parser("status", help="Print quota table for all homes.")
    p_status.add_argument(
        "--homes",
        nargs="+",
        default=_DEFAULT_HOMES,
        metavar="DIR",
        help="CODEX_HOME directories (default: ~/.codex-a ~/.codex-b)",
    )

    # pick
    p_pick = sub.add_parser(
        "pick", help="Print the home path with lowest usage. Exit 1 if all exhausted."
    )
    p_pick.add_argument(
        "--homes",
        nargs="+",
        default=_DEFAULT_HOMES,
        metavar="DIR",
    )
    p_pick.add_argument(
        "--max-percent",
        type=float,
        default=95.0,
        metavar="PCT",
    )

    # wait
    p_wait = sub.add_parser(
        "wait", help="Block until at least one account drops below 95%."
    )
    p_wait.add_argument(
        "--homes",
        nargs="+",
        default=_DEFAULT_HOMES,
        metavar="DIR",
    )
    p_wait.add_argument(
        "--poll",
        type=int,
        default=30,
        metavar="SECS",
        help="Poll interval in seconds (default: 30)",
    )

    return parser


def _main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    homes = [Path(h).expanduser().resolve() for h in args.homes]

    if args.cmd == "status":
        print(format_status(homes))
        return 0

    elif args.cmd == "pick":
        try:
            chosen, snap = pick_account(homes, max_percent=args.max_percent)
            print(chosen)
            return 0
        except QuotaExhausted as exc:
            print(
                f"All accounts exhausted. Earliest reset in "
                f"{humanize_duration(exc.min_reset_in_seconds)}",
                file=sys.stderr,
            )
            return 1

    elif args.cmd == "wait":
        wait_until_reset(homes, poll_interval_seconds=args.poll)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(_main())
