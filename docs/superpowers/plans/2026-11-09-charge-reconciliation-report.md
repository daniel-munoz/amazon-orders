# Charge Reconciliation Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI that lists every Amazon charge and refund in a date range, tied to its order and items, as a Markdown or JSON report.

**Architecture:** One script. Library objects are converted at the boundary into plain dataclasses, so classification and rendering are pure functions tested without a network. Fetching is thin and tested with fake library classes.

**Tech Stack:** Python 3.9+, `amazon-orders==5.3.0`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-09-charge-reconciliation-report-design.md`

## Global Constraints

- Files: `amazon_orders_export.py`, `test_amazon_orders_export.py`, `requirements.txt`, `README.md`, `.gitignore`. No others.
- `amazon-orders==5.3.0` stays pinned.
- Credentials come only from `AMAZON_USERNAME`, `AMAZON_PASSWORD`, `AMAZON_OTP_SECRET_KEY`. Never pass them in code, call `config.save()`, or log them.
- Config data is exactly `{"warn_on_missing_required_field": True, "max_auth_attempts": 3}`.
- Use `from __future__ import annotations` (Python 3.9 floor).
- Compare money rounded to cents.
- Run tests with `.venv/bin/python -m pytest`. Unit tests never touch the network or real Amazon.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A saved session Amazon has expired: the run logs in again once and retries, rather than failing with a confusing error.
2. `get_order` raising something other than a library error, such as `TypeError` from the library sorting items with a missing title: only that charge becomes Unresolved.
3. A range with no charges: a report saying so, exit 0.
4. Titles containing Markdown characters or newlines, and URLs containing parentheses: the report still renders correctly.
5. No credentials and no saved session: a clear message naming the variables, exit 1, no hanging prompt.

---

### Task 1: CLI arguments and date helpers

**Files:**
- Create: `amazon_orders_export.py`, `test_amazon_orders_export.py`
- Modify: `.gitignore`, `requirements.txt`

**Interfaces:**
- Produces: `parse_args(argv: list[str] | None = None) -> argparse.Namespace` with `.start: date`, `.end: date`, `.out: Path | None`, `.json: bool`; `default_out(start: date, end: date, as_json: bool) -> Path`; `lookback_days(start: date, today: date) -> int`; `years_in_range(start: date, end: date) -> list[int]`

- [ ] **Step 1: Set up.** Append `reports/` to `.gitignore` under the exported-history section. Add `pytest>=8,<9` to `requirements.txt`. Run `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. Expected: installs cleanly.

- [ ] **Step 2: Write the failing tests**

```python
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
    assert export.default_out(date(2025, 1, 1), date(2025, 6, 30), False) == Path("reports/amazon-charges_2025-01-01_2025-06-30.md")
    assert export.default_out(date(2025, 1, 1), date(2025, 6, 30), True).suffix == ".json"

def test_lookback_days_includes_start_and_never_drops_below_one():
    assert export.lookback_days(date(2025, 1, 1), date(2025, 1, 11)) == 11
    assert export.lookback_days(date(2025, 2, 1), date(2025, 1, 11)) == 1

def test_years_in_range():
    assert export.years_in_range(date(2023, 12, 1), date(2025, 1, 1)) == [2023, 2024, 2025]
```

- [ ] **Step 3: Run tests.** `.venv/bin/python -m pytest -q`. Expected: FAIL, `ModuleNotFoundError` for `amazon_orders_export`.

- [ ] **Step 4: Implement the four functions** with `argparse`. Parse dates with `date.fromisoformat` in a `type=` function; report `end < start` with `parser.error`.

- [ ] **Step 5: Run tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git add .gitignore requirements.txt amazon_orders_export.py test_amazon_orders_export.py && git commit -m "Add CLI arguments and date helpers"`

---

### Task 2: Data model and charge classification

**Files:** Modify `amazon_orders_export.py`, `test_amazon_orders_export.py`

**Interfaces:** Produces these dataclasses and functions, used by every later task:

```python
MATCHED, SPLIT, REFUND, UNRESOLVED = "Matched", "Split", "Refund", "Unresolved"

@dataclass
class ItemView: title: str | None; quantity: int | None; price: float | None; asin: str | None; link: str | None; image_link: str | None
@dataclass
class ShipmentView: status: str | None; items: list[ItemView]
@dataclass
class OrderView:
    order_number: str | None; placed_date: date | None; grand_total: float | None; link: str | None
    cancelled: bool; shipments: list[ShipmentView]; items: list[ItemView]
    # property has_items: True if self.items or any shipment has items
@dataclass
class Related: date: date; amount: float; is_refund: bool
@dataclass
class Charge:
    date: date; amount: float; is_refund: bool   # amount is always positive
    payment_method: str | None; last_4: str | None; order_number: str | None; order_link: str | None; seller: str | None
    order: OrderView | None = None; lookup_error: str | None = None
    status: str = ""; reason: str | None = None
    charge_index: int | None = None; charge_count: int | None = None; charges_total: float | None = None
    related: list[Related] = field(default_factory=list)
@dataclass
class Report: start: date; end: date; generated_at: datetime; charges: list[Charge]; uncharged_orders: list[OrderView]; warnings: list[str]

def to_charge(txn) -> Charge
def to_order_view(order) -> OrderView
def classify(charges: list[Charge]) -> None   # mutates each charge
```

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace
from amazonorders.conf import AmazonOrdersConfig
from amazonorders.entity.transaction import Transaction

ONE = "111-0000000-0000001"

def item(title="Book"):
    return export.ItemView(title, 1, 12.99, "B000000001", "https://www.amazon.com/dp/B000000001", "https://m.media-amazon.com/i.jpg")

def order(total=12.99, items=True, number=ONE):
    return export.OrderView(number, date(2025, 3, 12), total, "https://www.amazon.com/o", False, [], [item()] if items else [])

def charge(amount=12.99, refund=False, number=ONE, o="default", day=14, error=None):
    return export.Charge(date(2025, 3, day), amount, refund, "Visa ****1234", "1234", number, None, None,
                         order=order() if o == "default" else o, lookup_error=error)

def test_to_charge_uses_positive_amounts(tmp_path):
    cfg = AmazonOrdersConfig(config_path=str(tmp_path / "c.yml"))
    t = Transaction.from_fields(cfg, date(2025, 3, 14), -12.99, payment_method="Visa ****1234", order_number=ONE)
    c = export.to_charge(t)
    assert (c.amount, c.is_refund, c.last_4, c.order_number) == (12.99, False, "1234", ONE)

def test_to_order_view_keeps_shipments_and_items():
    i = SimpleNamespace(title="T", quantity=None, price=None, asin=None, link=None, image_link=None)
    o = SimpleNamespace(order_number=ONE, order_placed_date=date(2025, 3, 1), grand_total=5.0, order_details_link=None,
                        cancelled=False, items=[i], shipments=[SimpleNamespace(delivery_status="Delivered", items=[i])])
    v = export.to_order_view(o)
    assert v.has_items and v.shipments[0].status == "Delivered" and v.items[0].title == "T"

def test_single_exact_charge_is_matched():
    c = charge(); export.classify([c])
    assert c.status == export.MATCHED

def test_money_is_compared_in_cents():
    c = charge(amount=0.1 + 0.2, o=order(total=0.3)); export.classify([c])
    assert c.status == export.MATCHED

def test_two_charges_on_one_order_are_split_and_numbered():
    a, b = charge(amount=10, o=order(total=27.98), day=15), charge(amount=17.98, o=order(total=27.98), day=14)
    export.classify([a, b])
    assert (a.status, b.status) == (export.SPLIT, export.SPLIT)
    assert (b.charge_index, a.charge_index, a.charge_count, a.charges_total) == (1, 2, 2, 27.98)

def test_single_charge_differing_from_total_is_split():
    c = charge(amount=5, o=order(total=20)); export.classify([c])
    assert c.status == export.SPLIT

def test_refund_does_not_split_purchase_and_they_reference_each_other():
    p, r = charge(day=14), charge(refund=True, day=20)
    export.classify([p, r])
    assert (p.status, r.status, p.charge_count) == (export.MATCHED, export.REFUND, 1)
    assert p.related[0].date == date(2025, 3, 20) and r.related[0].date == date(2025, 3, 14)

@pytest.mark.parametrize("c,reason", [
    (charge(number=None, o=None), "Amazon did not link this charge to an order"),
    (charge(o=None, error="boom"), "Order lookup failed: boom"),
    (charge(o=order(items=False)), "Order has no items"),
    (charge(o=order(total=None)), "Order total not available"),
])
def test_unresolved_reasons(c, reason):
    export.classify([c])
    assert (c.status, c.reason) == (export.UNRESOLVED, reason)
```

- [ ] **Step 2: Run tests.** Expected: FAIL, `AttributeError` on `export`.

- [ ] **Step 3: Implement.** `to_charge` sets `amount=abs(round(txn.grand_total, 2))`. `classify` checks in this order: Refund, Unresolved, then Split or Matched. Number non-refund charges per order by date, and sum `charges_total` from them, rounded to cents.

- [ ] **Step 4: Run tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -am "Add charge model and classification"`

---

### Task 3: Markdown and JSON rendering

**Files:** Modify `amazon_orders_export.py`, `test_amazon_orders_export.py`

**Interfaces:**
- Consumes: Task 2 dataclasses.
- Produces: `escape_md(text: str | None) -> str`; `safe_url(url: str | None) -> str | None`; `render_markdown(report: Report) -> str`; `render_json(report: Report) -> str`

Structure: `#` title; `##` sections "Unresolved charges", "Charges", "Orders with no charge found"; `###` one per charge. Within a section, oldest first. Under each charge heading: `Order <number>, placed <date>, order total $<total>`; `Statement text: <seller>` when a seller exists; the status text; `[View order](<order_link or order.link>)`. Group items under `**Shipment: <status>**` when any shipment has items, otherwise list `order.items` flat. Missing values render as "not available".

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime
import json

def report(charges=(), uncharged=(), warnings=()):
    cs = list(charges); export.classify(cs)
    return export.Report(date(2025, 3, 1), date(2025, 3, 31), datetime(2025, 4, 1, 9), cs, list(uncharged), list(warnings))

def test_escape_md_handles_markup_and_newlines():
    assert export.escape_md("A [b] *c*\n_d_") == r"A \[b\] \*c\* \_d\_"
    assert export.escape_md(None) == "not available"

def test_safe_url():
    assert export.safe_url("https://a.com/x(1) y") == "https://a.com/x%281%29%20y"
    assert export.safe_url("javascript:alert(1)") is None and export.safe_url(None) is None

def test_split_charge_block():
    a, b = charge(amount=12.99, o=order(total=27.98), day=14), charge(amount=14.99, o=order(total=27.98), day=15)
    md = export.render_markdown(report([a, b]))
    assert r"### 2025-03-14 · $12.99 · Visa \*\*\*\*1234 · Split" in md
    assert "Charge 1 of 2 for this order. Charges in this report total $27.98." in md
    assert "These items can't be attributed to this specific charge." in md
    assert "**Book**, qty 1, $12.99" in md and "![Book](https://m.media-amazon.com/i.jpg)" in md

def test_single_split_explains_the_difference():
    md = export.render_markdown(report([charge(amount=5, o=order(total=20))]))
    assert "This is the only charge found for this order, and it differs from the order total." in md

def test_unresolved_comes_first_and_shows_reason():
    md = export.render_markdown(report([charge(day=1), charge(o=order(total=None), day=20)]))
    assert md.index("## Unresolved charges") < md.index("## Charges")
    assert "Order total not available" in md and "amount not verified" in md

def test_missing_image_and_fields_still_render():
    bare = export.OrderView(ONE, None, 12.99, None, False, [],
                            [export.ItemView(None, None, None, None, None, None)])
    md = export.render_markdown(report([charge(o=bare)]))
    assert "![" not in md and "not available" in md

def test_shipments_group_items():
    o = export.OrderView(ONE, date(2025, 3, 1), 12.99, None, False, [export.ShipmentView("Delivered March 15", [item()])], [item()])
    md = export.render_markdown(report([charge(o=o)]))
    assert "**Shipment: Delivered March 15**" in md and md.count("**Book**") == 1

def test_refund_notes_its_purchase():
    md = export.render_markdown(report([charge(day=14), charge(refund=True, day=20)]))
    assert "Refund" in md and "2025-03-20" in md and "2025-03-14" in md

def test_order_line_and_statement_text():
    c = charge(); c.seller = "AMZN Digital"; c.order_link = "https://www.amazon.com/o1"
    md = export.render_markdown(report([c]))
    assert f"Order {ONE}, placed 2025-03-12, order total $12.99" in md
    assert "Statement text: AMZN Digital" in md and "[View order](https://www.amazon.com/o1)" in md

def test_warnings_and_counts_at_top():
    md = export.render_markdown(report([charge()], warnings=["2 rows could not be read"]))
    assert md.index("2 rows could not be read") < md.index("### 2025-03-14") and "Matched: 1" in md

def test_empty_period_says_so():
    assert "No charges found in this period." in export.render_markdown(report())

def test_uncharged_orders_section_marks_cancelled():
    o = export.OrderView("D01-1", date(2025, 3, 2), None, None, True, [], [item()])
    md = export.render_markdown(report(uncharged=[o]))
    assert "## Orders with no charge found" in md and "D01-1" in md and "cancelled" in md

def test_json_round_trips():
    data = json.loads(export.render_json(report([charge()])))
    assert data["charges"][0]["status"] == "Matched" and data["start"] == "2025-03-01"
```

- [ ] **Step 2: Run tests.** Expected: FAIL, `AttributeError` on `export`.

- [ ] **Step 3: Implement.** `escape_md` collapses whitespace, then backslash-escapes `` \ ` * _ [ ] < > ``. `safe_url` allows only `http://` and `https://`, and percent-encodes spaces and parentheses. `render_json` is `json.dumps(asdict(report), default=str, indent=2)`. Item line: `- ![title](image)` then an indented `**title**, qty N, $P, [product page](link)`; with no image, the line starts `- **title**`.

- [ ] **Step 4: Run tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -am "Add Markdown and JSON rendering"`

---

### Task 4: Fetching from Amazon

**Files:** Modify `amazon_orders_export.py`, `test_amazon_orders_export.py`

**Interfaces:**
- Consumes: `lookback_days`, `years_in_range`, `to_charge`, `to_order_view`.
- Produces: `build_config(config_path: str | None = None) -> AmazonOrdersConfig`; `credentials_problem(config, environ: Mapping[str, str]) -> str | None`; `fetch_charges(session, start: date, end: date, today: date) -> tuple[list[Charge], int]` (second value is skipped rows); `attach_orders(session, charges: list[Charge]) -> None`; `fetch_uncharged_orders(session, charges: list[Charge], start: date, end: date) -> tuple[list[OrderView], list[str]]` (second value is warnings)

Import `AmazonSession`, `AmazonOrders`, `AmazonTransactions`, `AmazonOrdersConfig` by name at module level so tests can patch and reach them. Construct them as `AmazonTransactions(session)` and `AmazonOrders(session)`.

- [ ] **Step 1: Write the failing tests**

```python
import logging
from amazonorders.exception import AmazonOrdersAuthRedirectError, AmazonOrdersNotFoundError

def txn(day, number=ONE, total=-12.99):
    return SimpleNamespace(completed_date=day, grand_total=total, is_refund=total > 0, payment_method="Visa ****1234",
                           payment_method_last_4="1234", order_number=number, order_details_link=None, seller=None)

def patch_txns(monkeypatch, fn):
    monkeypatch.setattr(export, "AmazonTransactions", lambda s: SimpleNamespace(get_transactions=fn))

def test_build_config(tmp_path):
    c = export.build_config(str(tmp_path / "c.yml"))
    assert c.warn_on_missing_required_field is True and c.max_auth_attempts == 3

def test_credentials_problem(tmp_path):
    c_jar = SimpleNamespace(cookie_jar_path=str(tmp_path / "missing.json"))
    assert "AMAZON_USERNAME" in export.credentials_problem(c_jar, {})
    assert export.credentials_problem(c_jar, {"AMAZON_USERNAME": "u", "AMAZON_PASSWORD": "p"}) is None
    (tmp_path / "jar.json").write_text("[]")
    assert export.credentials_problem(SimpleNamespace(cookie_jar_path=str(tmp_path / "jar.json")), {}) is None

def test_fetch_charges_filters_range_and_uses_lookback(monkeypatch):
    seen = {}
    def get(days):
        seen["days"] = days
        return [txn(date(2025, 2, 28)), txn(date(2025, 3, 1)), txn(date(2025, 3, 31)), txn(date(2025, 4, 1))]
    patch_txns(monkeypatch, get)
    charges, skipped = export.fetch_charges(object(), date(2025, 3, 1), date(2025, 3, 31), date(2025, 4, 10))
    assert seen["days"] == 41 and [c.date.day for c in charges] == [1, 31] and skipped == 0

def test_fetch_charges_counts_rows_the_library_skipped(monkeypatch):
    def get(days):
        logging.getLogger("amazonorders.transactions").warning("skipped a row")
        return []
    patch_txns(monkeypatch, get)
    assert export.fetch_charges(object(), date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)) == ([], 1)

def test_fetch_charges_logs_in_again_once_when_session_expired(monkeypatch):
    calls, logins = [], []
    def get(days):
        calls.append(1)
        if len(calls) == 1:
            raise AmazonOrdersAuthRedirectError("expired")
        return []
    patch_txns(monkeypatch, get)
    export.fetch_charges(SimpleNamespace(login=lambda: logins.append(1)), date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3))
    assert (len(calls), len(logins)) == (2, 1)

def patch_orders(monkeypatch, **methods):
    monkeypatch.setattr(export, "AmazonOrders", lambda s: SimpleNamespace(**methods))

def lib_order(number=ONE, placed=date(2025, 3, 2)):
    return SimpleNamespace(order_number=number, order_placed_date=placed, grand_total=1.0, order_details_link=None,
                           cancelled=False, items=[], shipments=[])

def test_attach_orders_looks_up_each_order_once_and_survives_any_error(monkeypatch):
    calls = []
    def get_order(n):
        calls.append(n)
        if n == "bad1": raise AmazonOrdersNotFoundError("gone")
        if n == "bad2": raise TypeError("title was None")
        return lib_order(n)
    patch_orders(monkeypatch, get_order=get_order)
    a, b, c, d, e = (charge(number=n, o=None) for n in [ONE, ONE, "bad1", "bad2", None])
    export.attach_orders(object(), [a, b, c, d, e])
    assert calls == [ONE, "bad1", "bad2"] and a.order is b.order is not None
    assert "gone" in c.lookup_error and "title was None" in d.lookup_error and e.lookup_error is None

def test_uncharged_orders_checks_both_histories_dedupes_and_filters(monkeypatch):
    kinds = []
    def get_orders(year, order_filter=None):
        kinds.append((year, order_filter))
        return [lib_order("A"), lib_order(ONE), lib_order("OLD", date(2024, 1, 1)), lib_order("NODATE", None)]
    patch_orders(monkeypatch, get_orders=get_orders)
    found, warnings = export.fetch_uncharged_orders(object(), [charge()], date(2025, 3, 1), date(2025, 3, 31))
    assert kinds == [(2025, None), (2025, "digital-orders")] and warnings == []
    assert [o.order_number for o in found] == ["A", "NODATE"]

def test_uncharged_orders_warns_about_the_part_that_failed(monkeypatch):
    def get_orders(year, order_filter=None):
        if order_filter: raise RuntimeError("page changed")
        return []
    patch_orders(monkeypatch, get_orders=get_orders)
    _, warnings = export.fetch_uncharged_orders(object(), [], date(2025, 3, 1), date(2025, 3, 31))
    assert len(warnings) == 1 and "digital" in warnings[0] and "2025" in warnings[0]
```

- [ ] **Step 2: Run tests.** Expected: FAIL, `AttributeError` on `export`.

- [ ] **Step 3: Implement.** `credentials_problem` returns `None` if `config.cookie_jar_path` exists or both username and password are in `environ`; otherwise a message naming `AMAZON_USERNAME` and `AMAZON_PASSWORD`. `fetch_charges` attaches a `logging.Handler` at WARNING to the `amazonorders.transactions` logger, removes it in `finally`, and resets the count before the single retry. On `AmazonOrdersAuthRedirectError` it calls `session.login()` and retries once. `attach_orders` catches `Exception` per order. `fetch_uncharged_orders` calls `get_orders(year=Y)` and `get_orders(year=Y, order_filter="digital-orders")`, catches `Exception` per call, keeps orders with an unknown placed date, and sorts by date with unknown dates last.

- [ ] **Step 4: Run tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -am "Add Amazon fetching with per-order failure handling"`

---

### Task 5: Wire up `main` and document it

**Files:** Modify `amazon_orders_export.py`, `test_amazon_orders_export.py`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None, today: date | None = None) -> int`, invoked via `sys.exit(main())` under `if __name__ == "__main__"`.

- [ ] **Step 1: Write the failing tests**

```python
ARGS = ["--start", "2025-03-01", "--end", "2025-03-31"]

@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("AMAZON_USERNAME", "u"); monkeypatch.setenv("AMAZON_PASSWORD", "p")
    monkeypatch.setattr(export, "build_config", lambda: export.AmazonOrdersConfig(
        config_path=str(tmp_path / "c.yml"), data={"cookie_jar_path": str(tmp_path / "jar.json")}))
    monkeypatch.setattr(export, "AmazonSession", lambda config: SimpleNamespace(login=lambda: None))
    patch_txns(monkeypatch, lambda days: [txn(date(2025, 3, 14))])
    patch_orders(monkeypatch, get_order=lambda n: lib_order(n), get_orders=lambda year, order_filter=None: [])
    return tmp_path / "sub" / "out.md"

def run(out, today=date(2025, 4, 1), extra=()):
    return export.main([*ARGS, "--out", str(out), *extra], today=today)

def test_main_writes_report_creating_parent_folder(wired):
    assert run(wired) == 0 and "### 2025-03-14" in wired.read_text()

def test_main_json(wired):
    assert run(wired, extra=["--json"]) == 0 and "charges" in json.loads(wired.read_text())

def test_main_empty_period_writes_report(wired, monkeypatch):
    patch_txns(monkeypatch, lambda days: [])
    assert run(wired) == 0 and "No charges found" in wired.read_text()

def test_main_reports_skipped_rows_at_top(wired, monkeypatch):
    def get(days):
        logging.getLogger("amazonorders.transactions").warning("x")
        return []
    patch_txns(monkeypatch, get)
    assert run(wired) == 0 and "1 row" in wired.read_text()

def test_main_login_failure_exits_without_report(wired, monkeypatch, capsys):
    from amazonorders.exception import AmazonOrdersAuthError
    def fail(): raise AmazonOrdersAuthError("bad login")
    monkeypatch.setattr(export, "AmazonSession", lambda config: SimpleNamespace(login=fail))
    assert run(wired) == 1 and not wired.exists() and "bad login" in capsys.readouterr().err

def test_main_feed_failure_exits_without_report(wired, monkeypatch):
    def boom(days): raise TypeError("missing total")
    patch_txns(monkeypatch, boom)
    assert run(wired) == 1 and not wired.exists()

def test_main_no_credentials_is_a_clear_error(wired, monkeypatch, capsys):
    monkeypatch.delenv("AMAZON_USERNAME"); monkeypatch.delenv("AMAZON_PASSWORD")
    assert run(wired) == 1 and "AMAZON_USERNAME" in capsys.readouterr().err

def test_main_unwritable_output_is_a_clear_error(wired, tmp_path, capsys):
    (tmp_path / "blocker").write_text("")
    assert run(tmp_path / "blocker" / "out.md") == 1 and "Could not write" in capsys.readouterr().err

def test_main_prints_summary(wired, capsys):
    run(wired)
    err = capsys.readouterr().err
    assert "Matched: 1" in err and str(wired) in err
```

- [ ] **Step 2: Run tests.** Expected: FAIL, `AttributeError` on `main`.

- [ ] **Step 3: Implement `main`.** Order: parse args, `build_config()`, `credentials_problem(config, os.environ)`, `AmazonSession(config=config)`, `login()`, `fetch_charges`, `attach_orders`, `classify`, `fetch_uncharged_orders`, render, write to `args.out or default_out(...)`. On success, print the status counts (`Matched: N` and so on) and the output path to stderr. Turn a non-zero skipped count into a warning such as `"1 row of Amazon's transactions could not be read, so charges may be missing."`. Catch `Exception` around login and `fetch_charges`: print to stderr, return 1. Create parent folders before writing, and report `OSError` as `Could not write <path>: <error>`, returning 1.

- [ ] **Step 4: Run tests.** Run the whole suite: `.venv/bin/python -m pytest -q`. Expected: PASS.

- [ ] **Step 5: Update `README.md`.** Replace the status line. Add: usage; the three environment variables; that the first login may need her present for a code; that `AMAZON_COOKIE_JAR_PATH` must be set if the machine also uses another Amazon login; that `reports/` is git-ignored but a custom `--out` is the user's responsibility; that images only appear in a viewer that renders Markdown and loads remote images.

- [ ] **Step 6: Commit.** `git commit -am "Wire up report command and document usage"`

---

### Task 6: 30-day trial on the real account

The project owner must be present. Do not delegate this to a subagent, and stop after Step 2.

**Files:** Modify the spec only if the results change it.

- [ ] **Step 1: Run for the last 30 days.** With the environment variables set by the owner, run `.venv/bin/python amazon_orders_export.py --start $(date -v-30d +%F) --end $(date +%F)`. Expected: exit 0, and a report under `reports/`. Confirm `git status` doesn't list it.

- [ ] **Step 2: Show the owner the report and stop before any longer range.** Answer each "Must be confirmed on her real account" question in the spec from this run, and settle the spec's open question about who runs it and how she reads it.

- [ ] **Step 3: Record the answers in the spec and commit.** `git commit -am "Record results of 30-day trial"`. Never commit the report.
