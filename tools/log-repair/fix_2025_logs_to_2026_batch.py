#!/usr/bin/env python3
"""Batch-fix old-SEEMA 2025 frame logs into the current SEEMA timeline.

Drop this script into the folder that contains the log CSVs and run:

    python fix_2025_logs_to_2026_batch.py

It writes corrected copies under:

    fixed_seema_logs/

Original files are never overwritten.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re


OLD_SEEMA_TIME = 1752160362
NEW_SEEMA_TIME = 1783354049
OFFSET_SECONDS = NEW_SEEMA_TIME - OLD_SEEMA_TIME

OUTPUT_DIR_NAME = "fixed_seema_logs"
MANIFEST_NAME = "seema_batch_fix_manifest.csv"

ISO_COLUMNS = {"iso_timestamp", "iso_stamp", "iso_2025"}
RELATIVE_COLUMNS = {"timestamp_ms", "frame_ms", "relative_ms"}

DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})(?P<time_sep>[_:])(?P<minute>\d{2})(?P=time_sep)(?P<second>\d{2})"
    r"(?:(?P<frac_sep>[_.])(?P<fraction>\d{1,6}))?"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)

DATE_ONLY_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<sep>[-_])(?P<month>\d{1,2})(?P=sep)(?P<day>\d{1,2})"
    r"(?![T\d])"
)


@dataclass
class FileResult:
    source: Path
    destination: Path
    rows: int
    iso_cells_changed: int
    unix_cells_changed: int
    first_iso_before: str
    first_iso_after: str
    last_iso_before: str
    last_iso_after: str
    status: str
    error: str = ""


def parse_datetime_match(match: re.Match[str]) -> datetime:
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
    result = (
        f"{dt.year:04d}{date_sep}{dt.month:02d}{date_sep}{dt.day:02d}"
        f"T{dt.hour:02d}{time_sep}{dt.minute:02d}{time_sep}{dt.second:02d}"
    )

    fraction = match.group("fraction")
    if fraction is not None:
        frac_sep = match.group("frac_sep") or "_"
        result += f"{frac_sep}{dt.microsecond // 1000:03d}"

    return result + (match.group("suffix") or "")


def shift_datetime_text(text: str, delta: timedelta) -> tuple[str, int]:
    replacements: list[tuple[int, int, str]] = []
    for match in DATETIME_RE.finditer(text):
        shifted = parse_datetime_match(match) + delta
        replacements.append((match.start(), match.end(), format_datetime_like(match, shifted)))

    if not replacements:
        return text, 0

    output = text
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        output = output[:start] + replacement + output[end:]
    return output, len(replacements)


def shift_date_only_text(text: str, delta: timedelta) -> str:
    def repl(match: re.Match[str]) -> str:
        dt = datetime(int(match.group("year")), int(match.group("month")), int(match.group("day"))) + delta
        sep = match.group("sep")
        return f"{dt.year:04d}{sep}{dt.month:02d}{sep}{dt.day:02d}"

    return DATE_ONLY_RE.sub(repl, text)


def shift_path_part(part: str, delta: timedelta) -> str:
    shifted, count = shift_datetime_text(part, delta)
    if count:
        return shifted
    return shift_date_only_text(part, delta)


def is_iso_column(column: str) -> bool:
    return column.strip().lower() in ISO_COLUMNS


def is_unix_column(column: str) -> bool:
    normalized = column.strip().lower()
    return "unix" in normalized and normalized not in RELATIVE_COLUMNS and "ms" not in normalized


def shift_unix_text(value: str) -> tuple[str, bool]:
    stripped = value.strip()
    if not stripped:
        return value, False

    try:
        original = Decimal(stripped)
    except InvalidOperation:
        return value, False

    shifted = original + Decimal(OFFSET_SECONDS)
    decimals = len(stripped.split(".", 1)[1]) if "." in stripped else 0
    if decimals:
        return f"{shifted:.{decimals}f}", True
    return str(int(shifted)), True


def csv_files(root: Path, output_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.csv"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts[:-1]
        if any(part.lower().startswith("fixed_") for part in relative_parts):
            continue
        try:
            path.relative_to(output_root)
            continue
        except ValueError:
            pass
        if path.name == MANIFEST_NAME or path.name.lower().endswith("_manifest.csv"):
            continue
        files.append(path)
    return sorted(files)


def destination_for(source: Path, root: Path, output_root: Path, delta: timedelta) -> Path:
    relative = source.relative_to(root)
    shifted_parts = [shift_path_part(part, delta) for part in relative.parts]
    return output_root / Path(*shifted_parts)


def fix_csv(source: Path, destination: Path, delta: timedelta) -> FileResult:
    rows = 0
    iso_cells_changed = 0
    unix_cells_changed = 0
    first_iso_before = ""
    first_iso_after = ""
    last_iso_before = ""
    last_iso_after = ""

    destination.parent.mkdir(parents=True, exist_ok=True)

    with source.open("r", newline="", encoding="utf-8-sig") as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")

        temp = destination.with_name(destination.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames, lineterminator="\n")
            writer.writeheader()

            for row in reader:
                rows += 1
                row_first_before = ""
                row_first_after = ""

                for field in reader.fieldnames:
                    value = row.get(field, "")
                    if is_iso_column(field):
                        shifted, count = shift_datetime_text(value, delta)
                        if count:
                            row[field] = shifted
                            iso_cells_changed += count
                            if not row_first_before:
                                row_first_before = value
                                row_first_after = shifted
                    elif is_unix_column(field):
                        shifted, changed = shift_unix_text(value)
                        if changed:
                            row[field] = shifted
                            unix_cells_changed += 1

                if row_first_before:
                    if not first_iso_before:
                        first_iso_before = row_first_before
                        first_iso_after = row_first_after
                    last_iso_before = row_first_before
                    last_iso_after = row_first_after

                writer.writerow(row)

        temp.replace(destination)

    return FileResult(
        source=source,
        destination=destination,
        rows=rows,
        iso_cells_changed=iso_cells_changed,
        unix_cells_changed=unix_cells_changed,
        first_iso_before=first_iso_before,
        first_iso_after=first_iso_after,
        last_iso_before=last_iso_before,
        last_iso_after=last_iso_after,
        status="ok",
    )


def write_manifest(results: list[FileResult], manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(
            [
                "source",
                "destination",
                "old_seema",
                "new_seema",
                "offset_seconds",
                "rows",
                "iso_cells_changed",
                "unix_cells_changed",
                "first_iso_before",
                "first_iso_after",
                "last_iso_before",
                "last_iso_after",
                "status",
                "error",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.source,
                    result.destination,
                    OLD_SEEMA_TIME,
                    NEW_SEEMA_TIME,
                    OFFSET_SECONDS,
                    result.rows,
                    result.iso_cells_changed,
                    result.unix_cells_changed,
                    result.first_iso_before,
                    result.first_iso_after,
                    result.last_iso_before,
                    result.last_iso_after,
                    result.status,
                    result.error,
                ]
            )


def choose_output_root(root: Path) -> Path:
    output_root = root / OUTPUT_DIR_NAME
    if not output_root.exists():
        return output_root

    for index in range(1, 1000):
        candidate = root / f"{OUTPUT_DIR_NAME}_{index:02d}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("Could not find an unused fixed_seema_logs output folder")


def main() -> int:
    root = Path.cwd()
    output_root = choose_output_root(root)
    delta = timedelta(seconds=OFFSET_SECONDS)
    files = csv_files(root, output_root)

    print(f"Old SEEMA: {OLD_SEEMA_TIME}")
    print(f"New SEEMA: {NEW_SEEMA_TIME}")
    print(f"Offset seconds applied: {OFFSET_SECONDS}")
    print(f"Input folder: {root}")
    print(f"Output folder: {output_root}")
    print(f"CSV files found: {len(files)}")

    if not files:
        print("No CSV logs found.")
        return 1

    results: list[FileResult] = []
    for source in files:
        try:
            destination = destination_for(source, root, output_root, delta)
            if destination.exists():
                raise FileExistsError(f"destination already exists: {destination}")
            result = fix_csv(source, destination, delta)
            results.append(result)
            print(f"[ok] {source.name} -> {destination.relative_to(output_root)}")
        except Exception as exc:
            destination = output_root / source.relative_to(root)
            results.append(
                FileResult(source, destination, 0, 0, 0, "", "", "", "", "error", str(exc))
            )
            print(f"[error] {source}: {exc}")

    manifest = output_root / MANIFEST_NAME
    write_manifest(results, manifest)
    print(f"Manifest: {manifest}")
    print(f"Fixed files: {sum(1 for result in results if result.status == 'ok')}")
    print(f"Errors: {sum(1 for result in results if result.status == 'error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
