"""Export Amazon.com charges for a date range, each tied to the order and items it paid for."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid YYYY-MM-DD date")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_iso_date, required=True, help="first charge date, YYYY-MM-DD")
    parser.add_argument("--end", type=_iso_date, required=True, help="last charge date, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, help="output path (default: under reports/)")
    parser.add_argument("--json", action="store_true", help="write JSON instead of Markdown")
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end must not be before --start")
    return args


def default_out(start: date, end: date, as_json: bool) -> Path:
    extension = "json" if as_json else "md"
    return Path("reports") / f"amazon-charges_{start}_{end}.{extension}"


def lookback_days(start: date, today: date) -> int:
    """Days of history to request; the library's window counts back from today."""
    return max(1, (today - start).days + 1)


def years_in_range(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))
