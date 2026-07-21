#!/usr/bin/env python3
"""Subtract the old SEEMA offset from frame-log CSV absolute timestamps.

Use this for logs created when the computer clock was already real-world time
but the old SEEMA offset was added again. Relative frame timing columns are
left untouched.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re


OLD_SEEMA_TIME = Decimal("1752160362")

DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})(?P<time_sep>[_:])(?P<minute>\d{2})(?P=time_sep)(?P<second>\d{2})"
    r"(?:(?P<frac_sep>[_.])(?P<fraction>\d{1,6}))?"
    r"(?P<suffix>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)

COMPACT_RE = re.compile(r"(?<!\d)(?P<stamp>\d{14}(?:\d{3}|\d{6})?)(?!\d)")

ABSOLUTE_ISO_COLUMNS = {"iso_timestamp", "iso_stamp", "iso_2025"}
RELATIVE_MS_COLUMNS = {"timestamp_ms", "frame_ms", "relative_ms"}


@dataclass
class Result:
    source: Path
    destination: Path
    rows: int
    iso_cells_changed: int
    unix_cells_changed: int
    status: str
    error: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subtract old SEEMA time from absolute timestamps inside frame-log CSVs."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("."),
        help="CSV file or folder of CSV logs. Defaults to current folder.",
    )
    parser.add_argument("--seema", type=Decimal, default=OLD_SEEMA_TIME, help="Seconds to subtract.")
    parser.add_argument("--apply", action="store_true", help="Write corrected files.")
    parser.add_argument("--out", type=Path, default=Path("minus_old_seema_logs"), help="Output folder for copies.")
    parser.add_argument("--in-place", action="store_true", help="Modify originals after making .bak backups.")
    parser.add_argument("--backup-suffix", default=".old-seema-added.bak")
    parser.add_argument("--recursive", action="store_true", help="Process CSV files recursively.")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def datetime_from_match(match: re.Match[str]) -> datetime:
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


def format_datetime_like(match: re.Match[str], shifted: datetime) -> str:
    shifted = round_to_millisecond(shifted)
    date_sep = match.group("date_sep")
    time_sep = match.group("time_sep")
    result = (
        f"{shifted.year:04d}{date_sep}{shifted.month:02d}{date_sep}{shifted.day:02d}"
        f"T{shifted.hour:02d}{time_sep}{shifted.minute:02d}{time_sep}{shifted.second:02d}"
    )

    fraction = match.group("fraction")
    if fraction is not None:
        frac_sep = match.group("frac_sep") or "_"
        result += f"{frac_sep}{shifted.microsecond // 1000:03d}"

    return result + (match.group("suffix") or "")


def parse_compact(match: re.Match[str]) -> tuple[datetime, int]:
    stamp = match.group("stamp")
    base = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    fraction_len = len(stamp) - 14
    if fraction_len:
        base = base.replace(microsecond=int(stamp[14:].ljust(6, "0")))
    return base, fraction_len


def format_compact(dt: datetime, fraction_len: int) -> str:
    dt = round_to_millisecond(dt)
    result = dt.strftime("%Y%m%d%H%M%S")
    if fraction_len in (3, 6):
        result += f"{dt.microsecond // 1000:03d}"
    return result


def subtract_from_text(value: str, delta: timedelta) -> tuple[str, int]:
    replacements: list[tuple[int, int, str]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= a or start >= b) for a, b, _ in replacements)

    for match in DATETIME_RE.finditer(value):
        shifted = datetime_from_match(match) - delta
        replacements.append((match.start(), match.end(), format_datetime_like(match, shifted)))

    for match in COMPACT_RE.finditer(value):
        if overlaps(match.start(), match.end()):
            continue
        parsed, fraction_len = parse_compact(match)
        replacements.append((match.start(), match.end(), format_compact(parsed - delta, fraction_len)))

    if not replacements:
        return value, 0

    output = value
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        output = output[:start] + replacement + output[end:]
    return output, len(replacements)


def is_iso_column(name: str) -> bool:
    return name.strip().lower() in ABSOLUTE_ISO_COLUMNS


def is_unix_column(name: str) -> bool:
    normalized = name.strip().lower()
    return "unix" in normalized and "ms" not in normalized


def is_relative_column(name: str) -> bool:
    return name.strip().lower() in RELATIVE_MS_COLUMNS


def subtract_unix(value: str, offset: Decimal) -> tuple[str, bool]:
    stripped = value.strip()
    if not stripped:
        return value, False
    try:
        original = Decimal(stripped)
    except InvalidOperation:
        return value, False

    shifted = original - offset
    decimals = len(stripped.split(".", 1)[1]) if "." in stripped else 0
    if decimals:
        return f"{shifted:.{decimals}f}", True
    return str(int(shifted)), True


def csv_files(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    iterator = path.rglob("*.csv") if recursive else path.glob("*.csv")
    return sorted(p for p in iterator if p.is_file())


def destination_for(source: Path, root: Path, args: argparse.Namespace) -> Path:
    if args.in_place:
        return source
    rel = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
    return args.out / root.name / rel


def fix_one(source: Path, destination: Path, args: argparse.Namespace) -> Result:
    delta = timedelta(seconds=float(args.seema))
    rows = 0
    iso_changed = 0
    unix_changed = 0

    with source.open("r", newline="", encoding=args.encoding) as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames, lineterminator="\n")
            writer.writeheader()

            for row in reader:
                rows += 1
                for field in reader.fieldnames:
                    if is_relative_column(field):
                        continue

                    value = row.get(field, "")
                    if is_iso_column(field):
                        shifted, count = subtract_from_text(value, delta)
                        if count:
                            row[field] = shifted
                            iso_changed += count
                    elif is_unix_column(field):
                        shifted, changed = subtract_unix(value, args.seema)
                        if changed:
                            row[field] = shifted
                            unix_changed += 1

                writer.writerow(row)

        temp.replace(destination)

    return Result(source, destination, rows, iso_changed, unix_changed, "ok")


def write_manifest(results: list[Result], manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["source", "destination", "rows", "iso_cells_changed", "unix_cells_changed", "status", "error"])
        for result in results:
            writer.writerow(
                [
                    result.source,
                    result.destination,
                    result.rows,
                    result.iso_cells_changed,
                    result.unix_cells_changed,
                    result.status,
                    result.error,
                ]
            )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    input_path = args.path.resolve()
    root = input_path.parent if input_path.is_file() else input_path
    files = csv_files(input_path, args.recursive)

    if not files:
        print(f"No CSV files found in {input_path}")
        return 1

    if not args.apply:
        print("Dry run only. Add --apply to write corrected files.")
    elif args.in_place:
        print("Applying in place with backups.")
    else:
        print(f"Writing corrected copies under: {args.out}")

    print(f"Subtracting SEEMA seconds from absolute timestamps: {args.seema}")

    results: list[Result] = []
    for source in files:
        destination = destination_for(source, root, args)
        if args.verbose or not args.apply:
            print(f"{source} -> {destination}")

        if not args.apply:
            results.append(Result(source, destination, 0, 0, 0, "dry-run"))
            continue

        try:
            if args.in_place:
                backup = source.with_name(source.name + args.backup_suffix)
                if backup.exists():
                    raise FileExistsError(f"backup already exists: {backup}")
                shutil.copy2(source, backup)
            elif destination.exists():
                raise FileExistsError(f"destination already exists: {destination}")

            results.append(fix_one(source, destination, args))
        except Exception as exc:
            print(f"[error] {source}: {exc}")
            results.append(Result(source, destination, 0, 0, 0, "error", str(exc)))

    if args.apply:
        manifest = (Path(".") if args.in_place else args.out) / "subtract_old_seema_manifest.csv"
        write_manifest(results, manifest)
        print(f"Manifest: {manifest}")
        print(f"Processed {sum(1 for result in results if result.status == 'ok')} file(s); errors {sum(1 for result in results if result.status == 'error')}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
