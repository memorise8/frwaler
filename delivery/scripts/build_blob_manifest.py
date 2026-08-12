#!/usr/bin/env python3
"""Build a deterministic blob transfer manifest without modifying the source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def digest(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--no-hash", action="store_true", help="inventory rehearsal only")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    output = args.output.resolve()
    if not root.is_dir() or output == root or root in output.parents:
        parser.error("output must be outside the verified blob root")
    files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for index, path in enumerate(files, 1):
            stat = path.stat()
            row = {"path": path.relative_to(root).as_posix(), "size": stat.st_size,
                   "sha256": None if args.no_hash else digest(path)}
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            if index % 10000 == 0:
                print(f"{index:,}/{len(files):,}", flush=True)
        handle.flush(); os.fsync(handle.fileno())
    temporary.replace(output)
    print(f"manifest: {len(files):,} files -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
