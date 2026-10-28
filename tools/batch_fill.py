"""
Batch pre-fill: open many 'queued' applications, each in its own Chrome tab,
and fill what the profile explicitly answers. NEVER submits -- every tab is
left open for Noah to review and click Submit himself. 'applied' (already
applied) and every other non-queued status are skipped. No status is changed.

Usage:
    python tools/batch_fill.py --app-ids 12 34 56
    python tools/batch_fill.py --top 10
    python tools/batch_fill.py --top 10 --dry-run   # list only, no browser
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from applicator.prefill.runner import (
    DEFAULT_MAX_TABS, FILLABLE_STATUSES, load_apps, partition_apps, run_batch, top_queued_ids,
)


def main(argv=None) -> None:
    """
    desc: CLI entry point -- resolve the app ids (explicit or top-N queued),
        then run the batch pre-fill (or just list them with --dry-run).
    args:
        argv (list[str] | None): CLI args; None uses sys.argv.
    output:
        None.
    """
    parser = argparse.ArgumentParser(description="Pre-fill queued applications in Chrome tabs (never submits).")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--app-ids", type=int, nargs="+", help="application ids to fill")
    src.add_argument("--top", type=int, help="fill the top N queued applications")
    parser.add_argument("--max-tabs", type=int, default=DEFAULT_MAX_TABS)
    parser.add_argument("--dry-run", action="store_true", help="list what would be opened; no browser")
    args = parser.parse_args(argv)

    ids = args.app_ids if args.app_ids else top_queued_ids(args.top)
    if not ids:
        print("[batch_fill] No queued applications to fill.")
        return

    if args.dry_run:
        todo, skipped = partition_apps(ids, load_apps(ids), args.max_tabs, FILLABLE_STATUSES)
        for a in todo:
            print(f"  would open #{a['id']} {a['company']} -- {a['title']}")
        for e in skipped:
            print(f"  skip #{e['app_id']}: {e['notes']}")
        return

    run_batch(ids, max_tabs=args.max_tabs)
    print("[batch_fill] Done. Nothing was submitted -- review each tab and submit yourself.")


if __name__ == "__main__":
    main()
