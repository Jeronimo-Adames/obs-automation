#!/usr/bin/env python3
"""Rebuild frame-log CSV timestamps from the timestamp embedded in each filename.

Put this script in a folder of log CSVs, or run it against a folder. It reads each
log filename timestamp as that log's recording start anchor, then recalculates
absolute CSV timestamp columns from the relative frame-time column.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re


TIMESTAMP_RE = re.compile(
    r"(?P<year>\d{4})[-_](?P<month>\d{1,2})[-_](?P<day>\d{1,2})"
    r"T"
    r"(?P<hour>\d{1,2})[_:](?P<minute>\d{2})[_:](?P<second>\d{2})"
    r"(?P<frac_sep>[_.])(?P<fraction>\d{1,6})"
    r"(?P<suffix>Z|-PST|PST|-07_00|-08_00|[+-]\d{2}:?\d{2})?"
)

ISO_COLUMNS = {"iso_timestamp", "iso_stamp", "iso_2025"}
RELATIVE_MS_COLUMNS = ("timestamp_ms", "frame_ms", "relative_ms")


@dataclass
class Result:
    source: Path
    destination: Path | None
    anchor: str | None
    rows: int
    iso_cells_changed: int
    unix_cells_changed: int
    status: str
    error: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recalculate log CSV absolute timestamps from each log filename timestamp."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        type=Path,
        help="Folder or CSV file to process. Defaults to current folder.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("filename_recalibrated_logs"),
        help="Output folder for corrected copies.",
    )
    parser.add_argument("--apply", action="store_true", help="Write corrected copies.")
    parser.add_argument("--recursive", action="store_true", help="Process CSVs recursively.")
    parser.add_argument("--in-place", action="store_true", help="Replace original CSVs after making .bak backups.")
    parser.add_argument("--backup-suffix", default=".filename-recalibration.bak")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument(
        "--local-offset-hours",
        type=Decimal,
        default=Decimal("-7"),
        help="Offset used when rebuilding Unix-second columns. Default -7 matches current HECD/PDT logs.",
    )
    parser.add_argument(
        "--anchor-is",
        choices=("recording-start", "first-frame"),
        default="recording-start",
        help="Use first-frame if filename timestamp is the first logged frame, not recording start.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def parse_filename_anchor(path: Path) -> tuple[datetime, str, int]:
    match = TIMESTAMP_RE.search(path.name)
    if not match:
        raise ValueError("no timestamp with milliseconds found in filename")

    fraction_text = match.group("fraction")
    microsecond = int(fraction_text.ljust(6, "0"))
    anchor = datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second")),
        microsecond,
    )
    return anchor, match.group("suffix") or "", len(fraction_text)


def format_iso_like(anchor: datetime, suffix: str, fraction_digits: int = 3) -> str:
    if fraction_digits <= 3:
        frac = int((Decimal(anchor.microsecond) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if frac >= 1000:
            anchor += timedelta(seconds=1)
            frac = 0
        return anchor.strftime("%Y-%m-%dT%H_%M_%S_") + f"{frac:03d}"[:fraction_digits] + suffix

    return anchor.strftime("%Y-%m-%dT%H_%M_%S_") + f"{anchor.microsecond:06d}"[:fraction_digits] + suffix


def decimal_seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + (Decimal(delta.microseconds) / Decimal(1_000_000))


def unix_seconds_for_local(local_dt: datetime, local_offset_hours: Decimal) -> Decimal:
    utc_dt = local_dt - timedelta(hours=float(local_offset_hours))
    return decimal_seconds(utc_dt - datetime(1970, 1, 1))


def decimal_to_timedelta_seconds(value: Decimal) -> timedelta:
    return timedelta(seconds=float(value))


def relative_ms_field(fieldnames: list[str]) -> str | None:
    normalized = {field.strip().lower(): field for field in fieldnames}
    for candidate in RELATIVE_MS_COLUMNS:
        if candidate in normalized:
            return normalized[candidate]
    return None


def is_iso_column(field: str) -> bool:
    return field.strip().lower() in ISO_COLUMNS


def is_unix_column(field: str) -> bool:
    normalized = field.strip().lower()
    return "unix" in normalized and "ms" not in normalized


def format_unix_like(value: Decimal, template: str) -> str:
    stripped = template.strip()
    decimals = 3
    if "." in stripped:
        decimals = len(stripped.split(".", 1)[1])
    return f"{value:.{decimals}f}"


def csv_files(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    iterator = path.rglob("*.csv") if recursive else path.glob("*.csv")
    return sorted(p for p in iterator if p.is_file())


def destination_for(source: Path, root: Path, args: argparse.Namespace) -> Path:
    if args.in_place:
        return source
    relative = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
    return args.out / root.name / relative


def recalibrate_one(source: Path, destination: Path, args: argparse.Namespace) -> Result:
    anchor, suffix, fraction_digits = parse_filename_anchor(source)
    rows = 0
    iso_cells_changed = 0
    unix_cells_changed = 0

    with source.open("r", newline="", encoding=args.encoding) as fin:
        reader = csv.DictReader(fin)
        if not reader.fieldnames:
            raise ValueError("CSV has no header")

        rel_field = relative_ms_field(reader.fieldnames)
        if not rel_field:
            raise ValueError(f"CSV has no relative-ms column. Expected one of: {', '.join(RELATIVE_MS_COLUMNS)}")

        first_relative_ms: Decimal | None = None
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".tmp")

        with temp.open("w", newline="", encoding="utf-8") as fout:
            writer = csv.DictWriter(fout, fieldnames=reader.fieldnames, lineterminator="\n")
            writer.writeheader()

            for row in reader:
                rows += 1
                try:
                    relative_ms = Decimal((row.get(rel_field) or "").strip())
                except InvalidOperation:
                    writer.writerow(row)
                    continue

                if first_relative_ms is None:
                    first_relative_ms = relative_ms

                elapsed_ms = relative_ms
                if args.anchor_is == "first-frame":
                    elapsed_ms = relative_ms - first_relative_ms

                corrected = anchor + decimal_to_timedelta_seconds(elapsed_ms / Decimal(1000))

                for field in reader.fieldnames:
                    value = row.get(field, "")
                    if is_iso_column(field):
                        row[field] = format_iso_like(corrected, suffix, max(3, fraction_digits))
                        iso_cells_changed += 1
                    elif is_unix_column(field):
                        row[field] = format_unix_like(unix_seconds_for_local(corrected, args.local_offset_hours), value)
                        unix_cells_changed += 1

                writer.writerow(row)

        temp.replace(destination)

    return Result(source, destination, format_iso_like(anchor, suffix, max(3, fraction_digits)), rows, iso_cells_changed, unix_cells_changed, "ok")


def write_manifest(results: list[Result], manifest: Path) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(
            [
                "source",
                "destination",
                "filename_anchor",
                "rows",
                "iso_cells_changed",
                "unix_cells_changed",
                "status",
                "error",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.source,
                    result.destination or "",
                    result.anchor or "",
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

    results: list[Result] = []
    for source in files:
        try:
            anchor, suffix, fraction_digits = parse_filename_anchor(source)
            destination = destination_for(source, root, args)
            anchor_text = format_iso_like(anchor, suffix, max(3, fraction_digits))

            if args.verbose or not args.apply:
                print(f"{source} -> {destination}  [anchor {anchor_text}]")

            if not args.apply:
                results.append(Result(source, destination, anchor_text, 0, 0, 0, "dry-run"))
                continue

            if args.in_place:
                backup = source.with_name(source.name + args.backup_suffix)
                if backup.exists():
                    raise FileExistsError(f"backup already exists: {backup}")
                shutil.copy2(source, backup)

            if destination.exists() and destination.resolve() != source.resolve():
                raise FileExistsError(f"destination already exists: {destination}")

            results.append(recalibrate_one(source, destination, args))
        except Exception as exc:
            print(f"[skip] {source}: {exc}")
            results.append(Result(source, None, None, 0, 0, 0, "error", str(exc)))

    manifest_root = Path(".") if args.in_place else args.out
    manifest = manifest_root / "filename_recalibration_manifest.csv"
    if args.apply:
        write_manifest(results, manifest)
        print(f"Manifest: {manifest}")
        print(f"Processed {sum(1 for r in results if r.status == 'ok')} file(s); errors {sum(1 for r in results if r.status == 'error')}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
