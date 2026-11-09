from datetime import date
from pathlib import Path

import pytest

import amazon_orders_export as export


def test_parse_args_reads_dates_and_flags():
    a = export.parse_args(["--start", "2025-01-01", "--end", "2025-06-30", "--out", "x.md", "--json"])
    assert (a.start, a.end, a.out, a.json) == (date(2025, 1, 1), date(2025, 6, 30), Path("x.md"), True)


@pytest.mark.parametrize("argv", [
    ["--start", "2025-13-01", "--end", "2025-06-30"],
    ["--start", "2025-06-30", "--end", "2025-01-01"],
])
def test_parse_args_rejects_bad_ranges(argv):
    with pytest.raises(SystemExit) as e:
        export.parse_args(argv)
    assert e.value.code == 2


def test_default_out():
    assert export.default_out(date(2025, 1, 1), date(2025, 6, 30), False) == Path(
        "reports/amazon-charges_2025-01-01_2025-06-30.md")
    assert export.default_out(date(2025, 1, 1), date(2025, 6, 30), True).suffix == ".json"


def test_lookback_days_includes_start_and_never_drops_below_one():
    assert export.lookback_days(date(2025, 1, 1), date(2025, 1, 11)) == 11
    assert export.lookback_days(date(2025, 2, 1), date(2025, 1, 11)) == 1


def test_years_in_range():
    assert export.years_in_range(date(2023, 12, 1), date(2025, 1, 1)) == [2023, 2024, 2025]
