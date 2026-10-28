"""
Export a sanitized local CSV of applied-and-later applications (applied,
interviewing, offer, rejected), including submitted_at. Safe columns only —
no cover letters, form data, or personal info. Local file only.

Usage:
    python export/csv_export.py
    python export/csv_export.py --out path/to/file.csv
"""

import sys
import csv
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_all_applications

# Only applications Noah has already submitted himself (Applied and later).
EXPORT_STATUSES = ("applied", "interviewing", "offer", "rejected")

EXPORT_COLUMNS = [
    "company", "title", "location", "platform",
    "status", "url", "submitted_at", "created_at",
]

DEFAULT_OUT = Path(__file__).parent / "snapshot.csv"


def export(out_path: Path = DEFAULT_OUT) -> int:
    """
    desc: Write a sanitized local CSV of applications Noah has already applied
        to (status applied / interviewing / offer / rejected) to out_path.
        Queued/discovered/dismissed rows are left out. Safe columns only: no
        cover letters, form data, or personal info. Nothing is uploaded.
    args:
        out_path (Path): destination CSV path; parent dirs are created if
            needed.
    output (int):
        the number of application rows written.
    """
    apps = [a for a in get_all_applications() if a.get("status") in EXPORT_STATUSES]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for app in apps:
            writer.writerow({col: app.get(col) or "" for col in EXPORT_COLUMNS})
    return len(apps)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    n = export(args.out)
    print(f"Exported {n} applications to {args.out}")
