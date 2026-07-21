#!/usr/bin/env python3
"""Repair OBS recording/log timestamps when an external time anchor is known.

This tool cannot infer the real date for a PTP-outage recording by itself.
It can only apply a known offset, or rebuild CSV timestamps from a known
recording anchor. By default this is a dry-run. Use --apply to write copies.
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


DEFAULT_OLD_SEEMA = 1752160362
DEFAULT_NEW_SEEMA = 1783354049
DEFAULT_GLOB = "*"
DEFAULT_EXTENSIONS = ".csv,.mp4,.mkv,.mov"

DATETIME_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})(?P<time_sep>[_:])(?P<minute>\d{2})(?P=time_sep)(?P<second>\d{2})"
    r"(?:(?P<frac_sep>[_.])(?P<fraction>\d{1,6}))?"
    r"(?P<tz>Z|-PST|-PDT|PST|PDT|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
    r"(?!\d)"
)

COMPACT_RE = re.compile(r"(?<!\d)(?P<stamp>\d{14}(?:\d{3}|\d{6})?)(?!\d)")

DATE_ONLY_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>\d{4})(?P<date_sep>[-_])(?P<month>\d{1,2})(?P=date_sep)(?P<day>\d{1,2})"
    r"(?![T\d])"
)

ISO_COLUMNS = {"iso_timestamp", "iso_stamp", "iso_2025"}
RELATIVE_MS_COLUMNS = {"timestamp_ms", "frame_ms", "relative_ms"}


@dataclass
class FileResult:
    source: Path
    destination: Path
    changed: bool
    rows: int
    timestamp_cells_changed: int
    unix_cells_changed: int


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair OBS/video/log timestamps after old-SEEMA or PTP-outage clock errors.",
        epilog=(
            "Important: for PTP-outage recordings where both video and log names are wrong, "
            "you must provide an external truth anchor with --actual-start or --anchor-start. "
            "Without that Unix-time anchor, only relative timing can be preserved."
        ),
    )
    parser.add_argument("paths", nargs="+", type=Path, help="File(s) or folder(s) to repair.")
    parser.add_argument("--old-seema", type=int, default=DEFAULT_OLD_SEEMA, help="Old SEEMA/Meinberg delta.")
    parser.add_argument("--new-seema", type=int, default=DEFAULT_NEW_SEEMA, help="New SEEMA delta.")
    parser.add_argument(
        "--offset-seconds",
        type=Decimal,
        default=None,
        help="Explicit seconds to add. Overrides --old-seema/--new-seema.",
    )
    parser.add_argument(
        "--fake-start",
        help="Bad timestamp produced by the broken clock/script. Used with --actual-start to compute the offset.",
    )
    parser.add_argument(
        "--actual-start",
        help="Externally verified correct timestamp for the same event as --fake-start.",
    )
    parser.add_argument(
        "--anchor-start",
        help="Externally verified correct recording start timestamp. Rebuilds CSV ISO/Unix columns from relative frame ms.",
    )
    parser.add_argument(
        "--anchor-is",
        choices=("recording-start", "first-frame"),
        default="recording-start",
        help="Whether --anchor-start is recording start time or the first logged frame time.",
    )
    parser.add_argument(
        "--iso-suffix",
        default="-PST",
        help="Suffix used when rebuilding ISO columns from --anchor-start.",
    )
    parser.add_argument(
        "--local-offset-hours",
        type=Decimal,
        default=Decimal("-7"),
        help="Local display offset used to rebuild Unix columns from --anchor-start.",
    )
    parser.add_argument("--glob", default=DEFAULT_GLOB, help="File glob used for folder inputs.")
    parser.add_argument(
        "--extensions",
        default=DEFAULT_EXTENSIONS,
        help="Comma-separated file extensions to repair when folder inputs are used.",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Only scan the top level of folder inputs.")
    parser.add_argument("--out", type=Path, default=Path("seema_corrected"), help="Output folder for corrected copies.")
    parser.add_argument("--apply", action="store_true", help="Actually write repaired files.")
    parser.add_argument("--in-place", action="store_true", help="Replace files in place and keep a backup.")
    parser.add_argument("--backup-suffix", default=".old-seema.bak", help="Backup suffix for --in-place.")
    parser.add_argument(
        "--no-filename",
        action="store_true",
        help="Do not shift timestamps found in destination filenames/path parts.",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Do not shift timestamps inside CSV contents; only rename/copy files.",
    )
    parser.add_argument("--encoding", default="utf-8-sig", help="CSV input encoding.")
    parser.add_argument("--verbose", action="store_true", help="Print every file mapping.")
    return parser.parse_args(argv)


def offset_seconds(args: argparse.Namespace) -> Decimal:
    if args.offset_seconds is not None:
        return args.offset_seconds
    if args.fake_start or args.actual_start:
        if not (args.fake_start and args.actual_start):
            raise ValueError("--fake-start and --actual-start must be provided together")
        return decimal_seconds(parse_timestamp(args.actual_start) - parse_timestamp(args.fake_start))
    if args.anchor_start:
        return Decimal(0)
    return Decimal(args.new_seema - args.old_seema)


def decimal_seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + (Decimal(delta.microseconds) / Decimal(1_000_000))


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

    return result + (match.group("tz") or "")


def compact_datetime_from_match(match: re.Match[str]) -> tuple[datetime, int]:
    stamp = match.group("stamp")
    base = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    fraction_len = len(stamp) - 14
    if fraction_len:
        fraction = stamp[14:]
        microsecond = int(fraction.ljust(6, "0"))
        base = base.replace(microsecond=microsecond)
    return base, fraction_len


def format_compact_like(shifted: datetime, fraction_len: int) -> str:
    shifted = round_to_millisecond(shifted)
    result = shifted.strftime("%Y%m%d%H%M%S")
    if fraction_len in (3, 6):
        result += f"{shifted.microsecond // 1000:03d}"
    return result


def parse_timestamp(value: str) -> datetime:
    match = DATETIME_RE.search(value)
    if match:
        return datetime_from_match(match)

    match = COMPACT_RE.search(value)
    if match:
        parsed, _ = compact_datetime_from_match(match)
        return parsed

    raise ValueError(f"Could not parse timestamp: {value}")


def format_rebased_iso(dt: datetime, suffix: str) -> str:
    dt = round_to_millisecond(dt)
    return dt.strftime("%Y-%m-%dT%H_%M_%S_") + f"{dt.microsecond // 1000:03d}{suffix}"


def shift_date_only(match: re.Match[str], delta: timedelta) -> str:
    dt = datetime(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    shifted = dt + delta
    sep = match.group("date_sep")
    return f"{shifted.year:04d}{sep}{shifted.month:02d}{sep}{shifted.day:02d}"


def collect_text_replacements(text: str, delta: timedelta, include_date_only: bool) -> list[tuple[int, int, str]]:
    replacements: list[tuple[int, int, str]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= existing_start or start >= existing_end) for existing_start, existing_end, _ in replacements)

    for match in DATETIME_RE.finditer(text):
        shifted = datetime_from_match(match) + delta
        replacements.append((match.start(), match.end(), format_datetime_like(match, shifted)))

    for match in COMPACT_RE.finditer(text):
        if overlaps(match.start(), match.end()):
            continue
        shifted_base, fraction_len = compact_datetime_from_match(match)
        replacements.append((match.start(), match.end(), format_compact_like(shifted_base + delta, fraction_len)))

    if include_date_only:
        for match in DATE_ONLY_RE.finditer(text):
            if overlaps(match.start(), match.end()):
                continue
            replacements.append((match.start(), match.end(), shift_date_only(match, delta)))

    return sorted(replacements, key=lambda item: item[0], reverse=True)


def shift_text_timestamps(text: str, delta: timedelta, include_date_only: bool = False) -> tuple[str, int]:
    replacements = collect_text_replacements(text, delta, include_date_only)
    if not replacements:
        return text, 0

    out = text
    for start, end, replacement in replacements:
        out = out[:start] + replacement + out[end:]
    return out, len(replacements)


def shift_path_parts(path: Path, delta: timedelta) -> Path:
    shifted_parts = [shift_text_timestamps(part, delta, include_date_only=True)[0] for part in path.parts]
    return Path(*shifted_parts)


def is_iso_column(fieldname: str) -> bool:
    return fieldname.strip().lower() in ISO_COLUMNS


def is_unix_column(fieldname: str) -> bool:
    normalized = fieldname.strip().lower()
    return "unix" in normalized and "ms" not in normalized


def relative_ms_column(fieldnames: list[str]) -> str | None:
    for fieldname in fieldnames:
        if fieldname.strip().lower() in RELATIVE_MS_COLUMNS:
            return fieldname
    return None


def datetime_to_unix_seconds(dt: datetime, local_offset_hours: Decimal) -> Decimal:
    epoch = datetime(1970, 1, 1)
    utc_dt = dt - timedelta(hours=float(local_offset_hours))
    return decimal_seconds(utc_dt - epoch)


def format_decimal_like(value: Decimal, template: str, default_decimals: int = 3) -> str:
    stripped = template.strip()
    decimals = default_decimals
    if "." in stripped:
        decimals = len(stripped.split(".", 1)[1])
    return f"{value:.{decimals}f}"


def shift_unix_seconds(value: str, offset: Decimal) -> tuple[str, bool]:
    stripped = value.strip()
    if not stripped:
        return value, False

    try:
        original = Decimal(stripped)
    except InvalidOperation:
        return value, False

    shifted = original + offset
    decimals = 0
    if "." in stripped:
        decimals = len(stripped.split(".", 1)[1])
    if decimals:
        return f"{shifted:.{decimals}f}", True
    return str(int(shifted)), True


def repair_rows(
    source: Path,
    destination: Path,
    delta: timedelta,
    offset: Decimal,
    args: argparse.Namespace,
) -> FileResult:
    rows = 0
    timestamp_cells_changed = 0
    unix_cells_changed = 0
    changed = source != destination
    anchor_start = parse_timestamp(args.anchor_start) if args.anchor_start else None

    with source.open("r", newline="", encoding=args.encoding) as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError(f"{source} has no CSV header")
        relative_field = relative_ms_column(reader.fieldnames)
        first_relative_ms: Decimal | None = None

        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames, lineterminator="\n")
            writer.writeheader()

            for row in reader:
                rows += 1
                for field in reader.fieldnames:
                    value = row.get(field, "")
                    if anchor_start and relative_field and (is_iso_column(field) or is_unix_column(field)):
                        try:
                            rel_ms = Decimal((row.get(relative_field) or "").strip())
                        except InvalidOperation:
                            rel_ms = None

                        if rel_ms is not None:
                            if first_relative_ms is None:
                                first_relative_ms = rel_ms
                            elapsed_ms = rel_ms - first_relative_ms if args.anchor_is == "first-frame" else rel_ms
                            corrected_dt = anchor_start + timedelta(seconds=float(elapsed_ms / Decimal(1000)))
                            if is_iso_column(field):
                                row[field] = format_rebased_iso(corrected_dt, args.iso_suffix)
                                timestamp_cells_changed += 1
                                changed = True
                            elif is_unix_column(field):
                                unix_seconds = datetime_to_unix_seconds(corrected_dt, args.local_offset_hours)
                                row[field] = format_decimal_like(unix_seconds, value)
                                unix_cells_changed += 1
                                changed = True
                    elif is_iso_column(field):
                        shifted, count = shift_text_timestamps(value, delta)
                        if count:
                            row[field] = shifted
                            timestamp_cells_changed += count
                            changed = True
                    elif is_unix_column(field):
                        shifted, did_shift = shift_unix_seconds(value, offset)
                        if did_shift:
                            row[field] = shifted
                            unix_cells_changed += 1
                            changed = True

                writer.writerow(row)

        if changed:
            tmp.replace(destination)
        else:
            tmp.unlink(missing_ok=True)

    return FileResult(source, destination, changed, rows, timestamp_cells_changed, unix_cells_changed)


def copy_or_repair_file(
    source: Path,
    destination: Path,
    delta: timedelta,
    offset: Decimal,
    args: argparse.Namespace,
) -> FileResult:
    if args.no_csv:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination:
            shutil.copy2(source, destination)
        return FileResult(source, destination, source != destination, 0, 0, 0)

    if source.suffix.lower() != ".csv":
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination:
            shutil.copy2(source, destination)
        return FileResult(source, destination, source != destination, 0, 0, 0)

    return repair_rows(source, destination, delta, offset, args)


def allowed_extensions(args: argparse.Namespace) -> set[str]:
    return {ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}" for ext in args.extensions.split(",") if ext.strip()}


def iter_supported_files(path: Path, pattern: str, recursive: bool, extensions: set[str]) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    iterator = path.rglob(pattern) if recursive else path.glob(pattern)
    return sorted(p for p in iterator if p.is_file() and p.suffix.lower() in extensions)


def destination_for(source: Path, input_root: Path, args: argparse.Namespace, delta: timedelta) -> Path:
    if args.in_place:
        rel = source.relative_to(input_root) if source.is_relative_to(input_root) else Path(source.name)
        shifted_name = shift_path_parts(Path(rel.name), delta) if not args.no_filename else Path(rel.name)
        return source.with_name(str(shifted_name))

    rel = source.relative_to(input_root)
    if not args.no_filename:
        rel = shift_path_parts(rel, delta)
    return args.out / input_root.name / rel


def per_file_offset(source: Path, args: argparse.Namespace, default_offset: Decimal) -> tuple[Decimal, timedelta]:
    if args.anchor_start and not (args.offset_seconds is not None or args.fake_start or args.actual_start):
        try:
            offset = decimal_seconds(parse_timestamp(args.anchor_start) - parse_timestamp(source.name))
        except ValueError:
            offset = Decimal(0)
        return offset, timedelta(seconds=float(offset))

    return default_offset, timedelta(seconds=float(default_offset))


def repair_input_path(path: Path, args: argparse.Namespace, delta: timedelta, offset: Decimal) -> list[FileResult]:
    input_root = path.parent if path.is_file() else path
    files = iter_supported_files(path, args.glob, not args.no_recursive, allowed_extensions(args))
    results: list[FileResult] = []

    for source in files:
        file_offset, file_delta = per_file_offset(source, args, offset)
        destination = destination_for(source, input_root, args, file_delta)
        if args.verbose or not args.apply:
            print(f"{source} -> {destination}")

        if not args.apply:
            continue

        if args.in_place:
            backup = source.with_name(source.name + args.backup_suffix)
            if backup.exists():
                raise FileExistsError(f"Backup already exists: {backup}")
            shutil.copy2(source, backup)

        if destination.exists() and destination.resolve() != source.resolve():
            raise FileExistsError(f"Destination already exists: {destination}")

        results.append(copy_or_repair_file(source, destination, file_delta, file_offset, args))

        if args.in_place and destination != source:
            source.unlink()

    return results


def write_manifest(results: list[FileResult], args: argparse.Namespace) -> None:
    if not args.apply or not results:
        return

    manifest = Path("seema_repair_manifest.csv") if args.in_place else args.out / "seema_repair_manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["source", "destination", "changed", "rows", "timestamp_cells_changed", "unix_cells_changed"])
        for result in results:
            writer.writerow(
                [
                    result.source,
                    result.destination,
                    result.changed,
                    result.rows,
                    result.timestamp_cells_changed,
                    result.unix_cells_changed,
                ]
            )
    print(f"Manifest: {manifest}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    offset = offset_seconds(args)
    delta = timedelta(seconds=float(offset))

    print(f"SEEMA repair offset: {offset} seconds")
    if not args.apply:
        print("Dry run only. Add --apply to write corrected files.")
    elif args.in_place:
        print("Applying in place with backups.")
    else:
        print(f"Writing corrected copies under: {args.out}")

    all_results: list[FileResult] = []
    for path in args.paths:
        all_results.extend(repair_input_path(path.resolve(), args, delta, offset))

    write_manifest(all_results, args)
    if args.apply:
        changed = sum(1 for result in all_results if result.changed)
        print(f"Processed {len(all_results)} file(s); changed {changed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
