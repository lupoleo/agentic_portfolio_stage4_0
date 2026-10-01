"""E2E-S4.0B shadow ledger CLI.

    python -m tools.shadow_ledger export --source-db PATH --label LABEL
    python -m tools.shadow_ledger measure
    python -m tools.shadow_ledger report [--all-policies] [--complete-only]

The ledger lives in data/state/shadow_ledger.db (git-ignored). Source
databases are opened read-only; nothing here touches portfolio, proposal,
opportunity or execution state.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.e2e.shadow_ledger import (
    DEFAULT_LEDGER_PATH,
    ShadowLedgerStore,
    build_report,
    export_to_ledger,
    measure_ledger,
    report_markdown,
)

CURRENT_POLICIES = {"ai-8c3-directional-scoring-v3"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-db", type=Path, required=True)
    export.add_argument("--label", required=True)
    commands.add_parser("measure")
    report = commands.add_parser("report")
    report.add_argument("--all-policies", action="store_true",
                        help="include scores from earlier directional policies")
    report.add_argument("--complete-only", action="store_true")
    report.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    ledger = ShadowLedgerStore(args.ledger)
    if args.command == "export":
        result = export_to_ledger(args.source_db, ledger, args.label)
        print(json.dumps(result))
        return 0
    if args.command == "measure":
        print(json.dumps(measure_ledger(ledger)))
        return 0
    value = build_report(
        ledger,
        policies=None if args.all_policies else CURRENT_POLICIES,
        complete_only=args.complete_only,
    )
    text = report_markdown(value)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        args.out.with_suffix(".md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
