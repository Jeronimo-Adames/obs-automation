#!/usr/bin/env python3
"""Batch-fix frame log timestamps using the correct timestamp in each filename.

Drop this script into the folder that contains the bad log CSVs and run:

    python fix_logs_from_filename_anchor_batch.py

It expects log names like:

    L4_W09D03_REC7-13_2026_07_06T15_59_56_951-PDT.csv

The filename timestamp is treated as the true first logged timestamp. Each row's
absolute ISO timestamp is rebuilt from that anchor plus the row's timestamp_ms
delta from the first valid row.

Original CSVs are moved into ./wrong_logs and corrected CSVs are written back
beside this script. Existing unrelated files are never overwritten.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import os
import re
import sys


WRONG_LOGS_DIR = "wrong_logs"
MANIFEST_NAME = "filename_anchor_fix_manifest.csv"

ISO_COLUMNS = {"iso_timestamp", "iso_stamp", "iso_2025"}
RELATIVE_MS_COLUMNS = ("timestamp_ms", "frame_ms", "relative_ms")

FILENAME_TIMESTAMP_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})(?P<time_sep>[_:])(?P<minute>\d{2})(?P=time_sep)(?P<second>\d{2})"
    r"(?P<frac_sep>[_.])(?P<fraction>\d{1,6})"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)

ISO_VALUE_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})[_:](?P<minute>\d{2})[_:](?P<second>\d{2})"
    r"(?:(?P<frac_sep>[_.])(?P<fraction>\d{1,6}))?"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)


@dataclass
class Plan:
    source: Path
    destination: Path
    archive: Path
    temp: Path
    add_hours: Decimal
    rows: int = 0
    iso_cells_changed: int = 0
    invalid_relative_rows: int = 0
    filename_anchor_before: str = ""
    filename_anchor_after: str = ""
    relative_ms_column: str = ""
    first_relative_ms: str = ""
    first_iso_before: str = ""
    first_iso_after: str = ""
    last_iso_before: str = ""
    last_iso_after: str = ""
    offset_seconds_from_first_iso: str = ""
    status: str = "pending"
    error: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fix log CSV timestamps using each log filename as the timestamp anchor."
    )
    parser.add_argument(
        "--add-hours",
        default="0",
        help="Hours to add to the filename anchor before rebuilding timestamps. Use 1 to add one hour.",
    )
    return parser.parse_args(argv)


def parse_timestamp_match(match: re.Match[str]) -> datetime:
    fraction = match.group("fraction") or ""
    microsecond = int(fraction.ljust(6, "0")) if fraction else 0
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
    )


def format_filename_timestamp_like(match: re.Match[str], dt: datetime) -> str:
    date_sep = match.group("date_sep")
    time_sep = match.group("time_sep")
    frac_sep = match.group("frac_sep")
    dt = round_to_millisecond(dt)

    return (
        f"{dt.year:04d}{date_sep}{dt.month:02d}{date_sep}{dt.day:02d}"
        f"T{dt.hour:02d}{time_sep}{dt.minute:02d}{time_sep}{dt.second:02d}"
        f"{frac_sep}{dt.microsecond // 1000:03d}"
        f"{match.group('suffix') or ''}"
    )


def parse_filename_anchor(path: Path) -> tuple[datetime, str, int, str, str]:
    match = FILENAME_TIMESTAMP_RE.search(path.name)
    if not match:
        raise ValueError("no filename timestamp with milliseconds found")

    anchor = parse_timestamp_match(match)
    return anchor, match.group("suffix") or "", len(match.group("fraction")), match.group(0), format_filename_timestamp_like(match, anchor)


def destination_name_for(source: Path, add_hours: Decimal) -> tuple[str, str, str]:
    match = FILENAME_TIMESTAMP_RE.search(source.name)
    if not match:
        raise ValueError("no filename timestamp with milliseconds found")

    anchor = parse_timestamp_match(match)
    shifted_anchor = anchor + decimal_hours_to_timedelta(add_hours)
    before = match.group(0)
    after = format_filename_timestamp_like(match, shifted_anchor)
    return source.name[: match.start()] + after + source.name[match.end() :], before, after


def parse_iso_value(value: str) -> datetime | None:
    match = ISO_VALUE_RE.search(value)
    if not match:
        return None
    return parse_timestamp_match(match)


def format_iso(dt: datetime, suffix: str, fraction_digits: int) -> str:
    dt = round_to_millisecond(dt)
    return dt.strftime("%Y-%m-%dT%H_%M_%S_") + f"{dt.microsecond // 1000:03d}" + suffix

def round_to_millisecond(dt: datetime) -> datetime:
    milliseconds = int(
        (Decimal(dt.microsecond) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if milliseconds >= 1000:
        dt += timedelta(seconds=1)
        milliseconds = 0
    return dt.replace(microsecond=milliseconds * 1000)


def decimal_ms_to_timedelta(milliseconds: Decimal) -> timedelta:
    return timedelta(seconds=float(milliseconds / Decimal(1000)))


def decimal_hours_to_timedelta(hours: Decimal) -> timedelta:
    return timedelta(seconds=float(hours * Decimal(3600)))


def decimal_seconds(delta: timedelta) -> Decimal:
    total = Decimal(delta.days * 86400 + delta.seconds)
    return total + (Decimal(delta.microseconds) / Decimal(1_000_000))


def is_iso_column(column: str) -> bool:
    return column.strip().lower() in ISO_COLUMNS


def relative_ms_field(fieldnames: list[str]) -> str | None:
    normalized = {field.strip().lower(): field for field in fieldnames}
    for candidate in RELATIVE_MS_COLUMNS:
        if candidate in normalized:
            return normalized[candidate]
    return None


def first_iso_column(fieldnames: list[str]) -> str | None:
    for field in fieldnames:
        if is_iso_column(field):
            return field
    return None


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


def build_plans(root: Path, add_hours: Decimal) -> list[Plan]:
    wrong_logs = root / WRONG_LOGS_DIR
    sources = [path for path in sorted(root.glob("*.csv")) if not should_skip_csv(path, root)]
    source_set = {path.resolve() for path in sources}
    destination_counts: dict[Path, int] = {}
    plans: list[Plan] = []

    for index, source in enumerate(sources):
        archive = unique_path(wrong_logs / source.name)
        temp = root / f".filename-anchor-fix-{os.getpid()}-{index:04d}.tmp"
        try:
            destination_name, filename_before, filename_after = destination_name_for(source, add_hours)
            destination = root / destination_name
            plan = Plan(
                source=source,
                destination=destination,
                archive=archive,
                temp=temp,
                add_hours=add_hours,
                filename_anchor_before=filename_before,
                filename_anchor_after=filename_after,
            )
        except Exception as exc:
            plan = Plan(
                source=source,
                destination=root / source.name,
                archive=archive,
                temp=temp,
                add_hours=add_hours,
                status="error",
                error=str(exc),
            )

        destination_counts[plan.destination.resolve()] = destination_counts.get(plan.destination.resolve(), 0) + 1
        plans.append(plan)

    for plan in plans:
        if plan.status == "error":
            continue
        if destination_counts[plan.destination.resolve()] > 1:
            plan.status = "error"
            plan.error = f"multiple logs would create {plan.destination.name}"
        elif plan.destination.exists() and plan.destination.resolve() not in source_set:
            plan.status = "error"
            plan.error = f"destination already exists: {plan.destination}"

    return plans


def write_rebuilt_temp(plan: Plan) -> None:
    filename_anchor, suffix, fraction_digits, _, _ = parse_filename_anchor(plan.source)
    anchor = filename_anchor + decimal_hours_to_timedelta(plan.add_hours)

    with plan.source.open("r", newline="", encoding="utf-8-sig") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")

        rel_field = relative_ms_field(reader.fieldnames)
        if not rel_field:
            expected = ", ".join(RELATIVE_MS_COLUMNS)
            raise ValueError(f"CSV has no relative millisecond column. Expected one of: {expected}")

        iso_field = first_iso_column(reader.fieldnames)
        if not iso_field:
            raise ValueError("CSV has no ISO timestamp column")

        plan.relative_ms_column = rel_field

        with plan.temp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames, lineterminator="\n")
            writer.writeheader()

            first_relative_ms: Decimal | None = None
            for row in reader:
                plan.rows += 1
                before = row.get(iso_field, "")

                try:
                    relative_ms = Decimal((row.get(rel_field) or "").strip())
                except InvalidOperation:
                    plan.invalid_relative_rows += 1
                    writer.writerow(row)
                    continue

                if first_relative_ms is None:
                    first_relative_ms = relative_ms
                    plan.first_relative_ms = str(relative_ms)
                    parsed_first_iso = parse_iso_value(before)
                    if parsed_first_iso is not None:
                        plan.offset_seconds_from_first_iso = str(decimal_seconds(anchor - parsed_first_iso))

                elapsed_from_first_row = relative_ms - first_relative_ms
                corrected = anchor + decimal_ms_to_timedelta(elapsed_from_first_row)
                after = format_iso(corrected, suffix, fraction_digits)

                for field in reader.fieldnames:
                    if is_iso_column(field):
                        row[field] = after
                        plan.iso_cells_changed += 1

                if not plan.first_iso_before:
                    plan.first_iso_before = before
                    plan.first_iso_after = after
                plan.last_iso_before = before
                plan.last_iso_after = after

                writer.writerow(row)


def process_plans(plans: list[Plan]) -> None:
    for plan in plans:
        if plan.status == "error":
            continue
        try:
            write_rebuilt_temp(plan)
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
                "filename_anchor_before",
                "filename_anchor_after",
                "method",
                "relative_ms_column",
                "first_relative_ms",
                "add_hours",
                "add_seconds",
                "offset_seconds_from_first_iso",
                "rows",
                "iso_cells_changed",
                "invalid_relative_rows",
                "first_iso_before",
                "first_iso_after",
                "last_iso_before",
                "last_iso_after",
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
                    plan.filename_anchor_before,
                    plan.filename_anchor_after,
                    "filename_anchor_plus_timestamp_ms_delta_from_first_valid_row",
                    plan.relative_ms_column,
                    plan.first_relative_ms,
                    plan.add_hours,
                    plan.add_hours * Decimal(3600),
                    plan.offset_seconds_from_first_iso,
                    plan.rows,
                    plan.iso_cells_changed,
                    plan.invalid_relative_rows,
                    plan.first_iso_before,
                    plan.first_iso_after,
                    plan.last_iso_before,
                    plan.last_iso_after,
                    plan.status,
                    plan.error,
                ]
            )
    return manifest


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        add_hours = Decimal(args.add_hours)
    except InvalidOperation:
        print(f"Invalid --add-hours value: {args.add_hours}")
        return 2

    root = Path(__file__).resolve().parent
    plans = build_plans(root, add_hours)

    print(f"Folder: {root}")
    print("Action: rebuild CSV ISO timestamps from each log filename timestamp")
    print("Archive folder: wrong_logs")
    print(f"Extra time added to filename anchors: {add_hours} hour(s)")
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
    raise SystemExit(main(sys.argv[1:]))
