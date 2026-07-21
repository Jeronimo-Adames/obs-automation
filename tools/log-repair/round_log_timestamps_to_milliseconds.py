#!/usr/bin/env python3
"""Round log filename and CSV timestamps to exactly three millisecond digits.

Drop this script into the folder with the logs and run:

    python round_log_timestamps_to_milliseconds.py

Original CSVs are moved into ./wrong_logs and corrected CSVs are written back
beside this script. Existing unrelated files are never overwritten.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import os
import re


WRONG_LOGS_DIR = "wrong_logs"
MANIFEST_NAME = "round_log_timestamps_to_milliseconds_manifest.csv"

DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})(?P<time_sep>[_:])(?P<minute>\d{2})(?P=time_sep)(?P<second>\d{2})"
    r"(?P<frac_sep>[_.])(?P<fraction>\d{1,6})"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)


@dataclass
class Plan:
    source: Path
    destination: Path
    archive: Path
    temp: Path
    status: str = "pending"
    error: str = ""
    rows: int = 0
    filename_timestamps_changed: int = 0
    timestamp_cells_changed: int = 0
    first_timestamp_before: str = ""
    first_timestamp_after: str = ""
    last_timestamp_before: str = ""
    last_timestamp_after: str = ""


def parse_datetime_match(match: re.Match[str]) -> datetime:
    fraction = match.group("fraction") or "0"
    microsecond = int(fraction.ljust(6, "0")[:6])
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
    )


def round_to_millisecond(dt: datetime) -> datetime:
    milliseconds = (dt.microsecond + 500) // 1000
    if milliseconds >= 1000:
        dt += timedelta(seconds=1)
        milliseconds = 0
    return dt.replace(microsecond=milliseconds * 1000)


def format_datetime_like(match: re.Match[str], dt: datetime) -> str:
    dt = round_to_millisecond(dt)
    date_sep = match.group("date_sep")
    time_sep = match.group("time_sep")
    frac_sep = match.group("frac_sep")
    return (
        f"{dt.year:04d}{date_sep}{dt.month:02d}{date_sep}{dt.day:02d}"
        f"T{dt.hour:02d}{time_sep}{dt.minute:02d}{time_sep}{dt.second:02d}"
        f"{frac_sep}{dt.microsecond // 1000:03d}"
        f"{match.group('suffix') or ''}"
    )


def normalize_text(text: str) -> tuple[str, int, str, str]:
    first_before = ""
    first_after = ""
    changed = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal first_before, first_after, changed
        before = match.group(0)
        after = format_datetime_like(match, parse_datetime_match(match))
        if after != before:
            changed += 1
            if not first_before:
                first_before = before
                first_after = after
        return after

    return DATETIME_RE.sub(repl, text), changed, first_before, first_after


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem}.{index:04d}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not create a unique path for {path}")


def should_skip_csv(path: Path, root: Path) -> bool:
    if path.parent != root:
        return True
    lower = path.name.lower()
    return lower.endswith("_manifest.csv") or lower == MANIFEST_NAME


def build_plans(root: Path) -> list[Plan]:
    wrong_logs = root / WRONG_LOGS_DIR
    sources = [path for path in sorted(root.glob("*.csv")) if not should_skip_csv(path, root)]
    source_set = {path.resolve() for path in sources}
    destination_counts: dict[Path, int] = {}
    plans: list[Plan] = []

    for index, source in enumerate(sources):
        corrected_name, filename_changes, _, _ = normalize_text(source.name)
        destination = root / corrected_name
        archive = unique_path(wrong_logs / source.name)
        temp = root / f".round-ms-{os.getpid()}-{index:04d}.tmp"
        plan = Plan(
            source=source,
            destination=destination,
            archive=archive,
            temp=temp,
            filename_timestamps_changed=filename_changes,
        )
        destination_counts[destination.resolve()] = destination_counts.get(destination.resolve(), 0) + 1
        plans.append(plan)

    for plan in plans:
        if destination_counts[plan.destination.resolve()] > 1:
            plan.status = "error"
            plan.error = f"multiple logs would create {plan.destination.name}"
        elif plan.destination.exists() and plan.destination.resolve() not in source_set:
            plan.status = "error"
            plan.error = f"destination already exists: {plan.destination}"

    return plans


def write_normalized_temp(plan: Plan) -> None:
    with plan.source.open("r", newline="", encoding="utf-8-sig") as fin:
        reader = csv.reader(fin)
        with plan.temp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.writer(fout, lineterminator="\n")
            for row_index, row in enumerate(reader):
                if row_index > 0:
                    plan.rows += 1

                normalized_row: list[str] = []
                for cell in row:
                    normalized, count, first_before, first_after = normalize_text(cell)
                    if count:
                        plan.timestamp_cells_changed += count
                        if not plan.first_timestamp_before:
                            plan.first_timestamp_before = first_before
                            plan.first_timestamp_after = first_after
                        plan.last_timestamp_before = first_before
                        plan.last_timestamp_after = first_after
                    normalized_row.append(normalized)

                writer.writerow(normalized_row)


def process_plans(plans: list[Plan]) -> None:
    for plan in plans:
        if plan.status == "error":
            continue
        try:
            write_normalized_temp(plan)
            plan.status = "ready"
        except Exception as exc:
            plan.status = "error"
            plan.error = str(exc)
            if plan.temp.exists():
                plan.temp.unlink()

    wrong_logs_needed = any(plan.status == "ready" for plan in plans)
    if wrong_logs_needed:
        (plans[0].source.parent / WRONG_LOGS_DIR).mkdir(exist_ok=True)

    for plan in plans:
        if plan.status != "ready":
            continue
        try:
            plan.source.replace(plan.archive)
            plan.status = "archived"
        except Exception as exc:
            plan.status = "error"
            plan.error = str(exc)

    for plan in plans:
        if plan.status != "archived":
            continue
        try:
            plan.temp.replace(plan.destination)
            plan.status = "ok"
        except Exception as exc:
            plan.status = "error"
            plan.error = str(exc)


def write_manifest(root: Path, plans: list[Plan]) -> Path:
    manifest = unique_path(root / MANIFEST_NAME)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(
            [
                "source_name",
                "corrected_name",
                "archived_wrong_log",
                "rows",
                "filename_timestamps_changed",
                "timestamp_cells_changed",
                "first_timestamp_before",
                "first_timestamp_after",
                "last_timestamp_before",
                "last_timestamp_after",
                "status",
                "error",
            ]
        )
        for plan in plans:
            writer.writerow(
                [
                    plan.source.name,
                    plan.destination.name,
                    plan.archive,
                    plan.rows,
                    plan.filename_timestamps_changed,
                    plan.timestamp_cells_changed,
                    plan.first_timestamp_before,
                    plan.first_timestamp_after,
                    plan.last_timestamp_before,
                    plan.last_timestamp_after,
                    plan.status,
                    plan.error,
                ]
            )
    return manifest


def main() -> int:
    root = Path(__file__).resolve().parent
    plans = build_plans(root)

    print(f"Folder: {root}")
    print("Action: round log filename and CSV timestamps to exactly 3 millisecond digits")
    print("Archive folder: wrong_logs")
    print(f"CSV logs found: {len(plans)}")

    if not plans:
        print("No CSV logs found beside this script.")
        return 1

    process_plans(plans)
    manifest = write_manifest(root, plans)

    print(f"Manifest: {manifest}")
    print(f"Fixed logs: {sum(1 for plan in plans if plan.status == 'ok')}")
    print(f"Errors: {sum(1 for plan in plans if plan.status == 'error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
