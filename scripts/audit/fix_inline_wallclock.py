#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-off (non-idempotent-safe re-run) fixer for the ~71 deviation files where
the 25*60 wall-clock budget appears only as an inline comparison/expression
(no bare assignment), e.g.:

    if time.time() - start_time > 25 * 60:
    if elapsed > 25 * 60:
    deadline = time.time() + 25 * 60  # comment

Rewrites the literal `25 * 60` (with flexible whitespace) on those lines to:

    int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

Ensures `import os` is present (reuses the same insertion logic as
raise_caps_rollout.py). Idempotent: skips lines that already contain
os.environ.get("LIBERTREE_MAX_WALL_S" or lines that already use LIBERTREE_MAX_WALL_S.

Only touches the exact numeric literal `25 * 60` / `25*60` (word-boundary
protected so it cannot partially match a longer number). Does NOT touch
module-level bare assignments (those are handled by raise_caps_rollout.py) --
this script skips any line matching that assignment form to avoid double edits.

Usage:
    python3 scripts/audit/fix_inline_wallclock.py --dry-run   # default
    python3 scripts/audit/fix_inline_wallclock.py --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CUSTOM_DIR = os.path.join(_PROJECT_ROOT, "crawler", "sites", "custom")

_WALL_MARKER = "LIBERTREE_MAX_WALL_S"

_BARE_ASSIGN_RE = re.compile(
    r'^[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*=[ \t]*25[ \t]*\*[ \t]*60[ \t]*(?:#.*)?$'
)
# Matches the literal `25 * 60` (or `25*60`) as a standalone numeric expression,
# not part of a larger number/identifier.
_INLINE_LITERAL_RE = re.compile(r'(?<![\w.])25[ \t]*\*[ \t]*60(?!\w)')

_PLAIN_IMPORT_RE = re.compile(r'^import\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$')
_FROM_IMPORT_RE = re.compile(r'^from\s+\S+\s+import\s+')
_TOPLEVEL_DEF_RE = re.compile(r'^(class |def )')

_REPLACEMENT = 'int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))'


def _ensure_import_os(lines: list[str]) -> tuple[list[str], bool]:
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
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    lines = original.split("\n")
    edits = []

    for i, line in enumerate(lines):
        if _WALL_MARKER in line:
            continue
        if "os.environ" in line:
            continue
        if _BARE_ASSIGN_RE.match(line):
            # handled by raise_caps_rollout.py, not this script
            continue
        if _INLINE_LITERAL_RE.search(line):
            new_line = _INLINE_LITERAL_RE.sub(_REPLACEMENT, line)
            lines[i] = new_line
            edits.append((i + 1, line, new_line))

    added_import = False
    if edits:
        lines, added_import = _ensure_import_os(lines)

    new_text = "\n".join(lines)

    return {
        "path": path,
        "edits": edits,
        "added_import_os": added_import,
        "changed": bool(edits),
        "new_text": new_text if edits else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="Specific files to process (relative or absolute). If omitted, no files are processed.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only, write nothing (default).")
    mode.add_argument("--apply", action="store_true", help="Write changes to disk.")
    args = parser.parse_args()

    apply_changes = bool(args.apply)

    if not args.files:
        print("ERROR: no files specified", file=sys.stderr)
        return 1

    paths = []
    for f in args.files:
        p = f if os.path.isabs(f) else os.path.join(_PROJECT_ROOT, f)
        paths.append(p)

    n_changed = 0
    n_lines_fixed = 0
    n_import_added = 0

    for path in paths:
        report = process_file(path)
        rel = os.path.relpath(path, _PROJECT_ROOT)

        if not report["changed"]:
            print(f"{rel}: NO CHANGE (no matching inline literal found)")
            continue

        n_changed += 1
        for line_no, old, new in report["edits"]:
            n_lines_fixed += 1
            print(f"{rel}:{line_no}:")
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
    print(f"Files processed:   {len(paths)}")
    print(f"Files changed:     {n_changed}")
    print(f"Lines fixed:       {n_lines_fixed}")
    print(f"import os added:   {n_import_added}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
