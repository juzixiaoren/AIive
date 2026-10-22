"""Command-line entrypoint: python -m aiive.eval run [--json] [--out PATH]."""
from __future__ import annotations

import argparse
import json
import sys

from aiive.eval.replay import run_replay_isolated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiive.eval", description="AIive self-evolution eval harness")
    sub = parser.add_subparsers(dest="cmd")
    runp = sub.add_parser("run", help="run the offline replay eval")
    runp.add_argument("--json", action="store_true", help="emit JSON instead of a human summary")
    runp.add_argument("--out", default=None, help="write JSON result to this path")
    args = parser.parse_args(argv)

    if args.cmd != "run":
        parser.print_help()
        return 2

    result = run_replay_isolated()
    if args.json:
        text = json.dumps(result.to_summary(), ensure_ascii=False, indent=2)
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        print(f"Replay Eval: passed={result.passed} failed={result.failed} error={result.error or '-'}")
        for c in result.cases:
            mark = "OK  " if c.ok else "FAIL"
            print(f"  [{mark}] {c.scenario_id} : {c.description}")
            for a in c.assertions:
                if not a.ok:
                    print(f"        - {a.message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
