#!/usr/bin/env python3
"""Pass3: exhaustive retry for pass2 misses (viewCount=500, 10 pages, both collections)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fino_search import (
    CHECKPOINT, search_pass3, run_pass,
    save_checkpoint, write_outputs, load_checkpoint,
)


def main():
    state = load_checkpoint()
    unmatched = state.get("unmatched", [])
    print(f"pass3 대상: {len(unmatched)}건", flush=True)
    if not unmatched:
        print("미매치 없음, 종료.")
        return

    run_pass(unmatched, state, search_pass3,
             workers=2, delay=0.1, save_every=50,
             pass_name="pass3", batch_size=40)

    state["unmatched"] = [k for k in unmatched if k not in state["matched"]]
    save_checkpoint(state)
    write_outputs(state)

    print(f"\n=== pass3 최종 ===")
    print(f"총 매치: {len(state['matched'])}")
    print(f"남은 미매치: {len(state['unmatched'])}")


if __name__ == "__main__":
    main()
