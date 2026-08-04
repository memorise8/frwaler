#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rollout: make the two hardcoded per-crawler caps env-overridable.

Applies the SAME transform validated on crawler/sites/custom/doaj-org-search.py
and crawler/sites/custom/amu-hal-science-search.py to every file matching
crawler/sites/custom/*.py:

    _MAX_PAGES = 200
        -> _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    <var> = 25 * 60      (any variable name, e.g. max_wall, _CRAWL_BUDGET_SECS, ...)
        -> <var> = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

Zero behavior change when the env vars are unset (defaults preserved: 200 pages,
1500s / 25 min wall-clock budget).

The regexes are intentionally conservative:
  - _MAX_PAGES: only matches a bare module-level assignment whose RHS is
    exactly the literal `200` (files with a different default, e.g. 60 or
    2000, are left untouched and reported as deviations).
  - wall-clock: only matches a bare `<identifier> = 25 * 60` (or `25*60`)
    assignment. Inline comparisons such as
    `if time.time() - start_time > 25 * 60:` (no assignment) are NOT
    rewritten and are reported as deviations, since rewriting those would
    require restructuring the comparison rather than a safe substitution.

Idempotent: files that already reference LIBERTREE_MAX_PAGES /
LIBERTREE_MAX_WALL_S for a given cap are skipped for that cap.

Usage:
    python3 scripts/audit/raise_caps_rollout.py              # dry-run (default)
    python3 scripts/audit/raise_caps_rollout.py --dry-run    # explicit dry-run
    python3 scripts/audit/raise_caps_rollout.py --apply      # write changes

This script performs NO crawling, NO DB access, and NO network calls. It only
reads/writes files under crawler/sites/custom/.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CUSTOM_DIR = os.path.join(_PROJECT_ROOT, "crawler", "sites", "custom")

_MAX_PAGES_RE = re.compile(
    r'^(?P<indent>[ \t]*)_MAX_PAGES(?P<sp1>[ \t]*)=(?P<sp2>[ \t]*)200'
    r'(?P<trail>[ \t]*(?:#.*)?)$'
)
_WALL_CLOCK_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?P<sp1>[ \t]*)=(?P<sp2>[ \t]*)'
    r'25[ \t]*\*[ \t]*60(?P<trail>[ \t]*(?:#.*)?)$'
)
_PLAIN_IMPORT_RE = re.compile(r'^import\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$')
_FROM_IMPORT_RE = re.compile(r'^from\s+\S+\s+import\s+')
_TOPLEVEL_DEF_RE = re.compile(r'^(class |def )')

_PAGES_MARKER = "LIBERTREE_MAX_PAGES"
_WALL_MARKER = "LIBERTREE_MAX_WALL_S"


def _ensure_import_os(lines: list[str]) -> tuple[list[str], bool]:
    """Insert 'import os' if no top-level 'import os' statement exists.

    Preserves alphabetical order within the leading plain-import block when
    one is found; otherwise inserts before the first 'from X import Y' line,
    or before the first top-level class/def as a last resort.
    """
    if any(re.match(r'^import\s+os\s*$', l) for l in lines):
        return lines, False

    limit = len(lines)
    for i, l in enumerate(lines):
        if _TOPLEVEL_DEF_RE.match(l):
            limit = i
            break

    plain_import_idxs = [i for i in range(limit) if _PLAIN_IMPORT_RE.match(lines[i])]

    if plain_import_idxs:
        insert_at = plain_import_idxs[-1] + 1
        for i in plain_import_idxs:
            mod = _PLAIN_IMPORT_RE.match(lines[i]).group(1)
            if mod > "os":
                insert_at = i
                break
        new_lines = lines[:insert_at] + ["import os"] + lines[insert_at:]
        return new_lines, True

    for i in range(limit):
        if _FROM_IMPORT_RE.match(lines[i]):
            new_lines = lines[:i] + ["import os", ""] + lines[i:]
            return new_lines, True

    new_lines = lines[:limit] + ["import os", ""] + lines[limit:]
    return new_lines, True


def process_file(path: str) -> dict:
    """Analyze (and optionally transform) one file. Returns a report dict.

    Never writes to disk itself — caller decides whether to persist `new_text`.
    """
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    lines = original.split("\n")
    already_pages = _PAGES_MARKER in original
    already_wall = _WALL_MARKER in original

    edits = []  # (line_no, kind, old, new)
    changed_pages = already_pages
    changed_wall = already_wall

    for i, line in enumerate(lines):
        if not changed_pages:
            m = _MAX_PAGES_RE.match(line)
            if m:
                new_line = (
                    f'{m.group("indent")}_MAX_PAGES{m.group("sp1")}={m.group("sp2")}'
                    f'int(os.environ.get("LIBERTREE_MAX_PAGES", "200")){m.group("trail")}'
                )
                lines[i] = new_line
                edits.append((i + 1, "_MAX_PAGES", line, new_line))
                changed_pages = True
                continue

        if not changed_wall:
            m = _WALL_CLOCK_RE.match(line)
            if m and "os.environ" not in line:
                var = m.group("var")
                new_line = (
                    f'{m.group("indent")}{var}{m.group("sp1")}={m.group("sp2")}'
                    f'int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))){m.group("trail")}'
                )
                lines[i] = new_line
                edits.append((i + 1, var, line, new_line))
                changed_wall = True
                continue

    added_import = False
    if edits:
        lines, added_import = _ensure_import_os(lines)

    new_text = "\n".join(lines)

    # Deviation detection (informational only, does not affect edits made above)
    deviations = []
    if not changed_pages and not already_pages:
        if re.search(r'_MAX_PAGES\s*=', original) or "_MAX_PAGES" not in original:
            if re.search(r'_MAX_PAGES\s*=\s*(?!200\b)\d+', original):
                deviations.append("_MAX_PAGES present with non-200 default")
            elif "_MAX_PAGES" in original:
                deviations.append("_MAX_PAGES present but assignment form not matched")
    if not changed_wall and not already_wall:
        if re.search(r'[A-Za-z_][A-Za-z0-9_]*\s*=\s*25[ \t]*\*[ \t]*60', original):
            deviations.append("wall-clock assignment present but not matched (unexpected)")
        elif re.search(r'25[ \t]*\*[ \t]*60', original):
            deviations.append("wall-clock budget appears only as inline comparison (no assignment)")

    return {
        "path": path,
        "edits": edits,
        "added_import_os": added_import,
        "changed": bool(edits),
        "new_text": new_text if edits else None,
        "deviations": deviations,
        "already_pages": already_pages,
        "already_wall": already_wall,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only, write nothing (default).")
    mode.add_argument("--apply", action="store_true", help="Write changes to disk.")
    args = parser.parse_args()

    apply_changes = bool(args.apply)

    if not os.path.isdir(_CUSTOM_DIR):
        print(f"ERROR: custom crawler dir not found: {_CUSTOM_DIR}", file=sys.stderr)
        return 1

    paths = sorted(
        os.path.join(_CUSTOM_DIR, name)
        for name in os.listdir(_CUSTOM_DIR)
        if name.endswith(".py")
    )

    total_files = len(paths)
    n_changed = 0
    n_pages_fixed = 0
    n_wall_fixed = 0
    n_import_added = 0
    n_already_done = 0
    n_deviations = 0
    deviation_files = []

    for path in paths:
        report = process_file(path)
        rel = os.path.relpath(path, _PROJECT_ROOT)

        if report["already_pages"] and report["already_wall"] and not report["edits"]:
            n_already_done += 1

        if report["deviations"]:
            n_deviations += 1
            deviation_files.append((rel, report["deviations"]))

        if not report["changed"]:
            continue

        n_changed += 1
        for line_no, kind, old, new in report["edits"]:
            if kind == "_MAX_PAGES":
                n_pages_fixed += 1
            else:
                n_wall_fixed += 1
            print(f"{rel}:{line_no}: [{kind}]")
            print(f"  - {old.strip()}")
            print(f"  + {new.strip()}")
        if report["added_import_os"]:
            n_import_added += 1
            print(f"{rel}: + import os")

        if apply_changes:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report["new_text"])

    print()
    print("=" * 70)
    print(f"Mode: {'APPLY (files written)' if apply_changes else 'DRY-RUN (no files written)'}")
    print(f"Scanned files:          {total_files}")
    print(f"Files changed:          {n_changed}")
    print(f"  _MAX_PAGES fixed:     {n_pages_fixed}")
    print(f"  wall-clock fixed:     {n_wall_fixed}")
    print(f"  import os added:      {n_import_added}")
    print(f"Already fully done:     {n_already_done}")
    print(f"Files with deviations:  {n_deviations}")
    if deviation_files:
        print()
        print("Deviations (pattern not matched by conservative regex, needs manual review):")
        for rel, reasons in deviation_files:
            print(f"  {rel}: {', '.join(reasons)}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
