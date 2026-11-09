import json
import logging
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from amazonorders.conf import AmazonOrdersConfig
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersAuthRedirectError, AmazonOrdersNotFoundError

import amazon_orders_export as export

ONE = "111-0000000-0000001"


def item(title="Book"):
    return export.ItemView(title, 1, 12.99, "B000000001", "https://www.amazon.com/dp/B000000001",
                           "https://m.media-amazon.com/i.jpg")


def order(total=12.99, items=True, number=ONE):
    return export.OrderView(number, date(2025, 3, 12), total, "https://www.amazon.com/o", False, [],
                            [item()] if items else [])


def charge(amount=12.99, refund=False, number=ONE, o="default", day=14, error=None):
    return export.Charge(date(2025, 3, day), amount, refund, "Visa ****1234", "1234", number, None, None,
                         order=order() if o == "default" else o, lookup_error=error)


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


def test_to_charge_uses_positive_amounts(tmp_path):
    cfg = AmazonOrdersConfig(config_path=str(tmp_path / "c.yml"))
    t = Transaction.from_fields(cfg, date(2025, 3, 14), -12.99, payment_method="Visa ****1234", order_number=ONE)
    c = export.to_charge(t)
    assert (c.amount, c.is_refund, c.last_4, c.order_number) == (12.99, False, "1234", ONE)


def test_to_order_view_keeps_shipments_and_items():
    i = SimpleNamespace(title="T", quantity=None, price=None, asin=None, link=None, image_link=None)
    o = SimpleNamespace(order_number=ONE, order_placed_date=date(2025, 3, 1), grand_total=5.0,
                        order_details_link=None, cancelled=False, items=[i],
                        shipments=[SimpleNamespace(delivery_status="Delivered", items=[i])])
    v = export.to_order_view(o)
    assert v.has_items and v.shipments[0].status == "Delivered" and v.items[0].title == "T"


def test_single_exact_charge_is_matched():
    c = charge()
    export.classify([c])
    assert c.status == export.MATCHED


def test_money_is_compared_in_cents():
    c = charge(amount=0.1 + 0.2, o=order(total=0.3))
    export.classify([c])
    assert c.status == export.MATCHED


def test_two_charges_on_one_order_are_split_and_numbered():
    a, b = charge(amount=10, o=order(total=27.98), day=15), charge(amount=17.98, o=order(total=27.98), day=14)
    export.classify([a, b])
    assert (a.status, b.status) == (export.SPLIT, export.SPLIT)
    assert (b.charge_index, a.charge_index, a.charge_count, a.charges_total) == (1, 2, 2, 27.98)


def test_single_charge_differing_from_total_is_split():
    c = charge(amount=5, o=order(total=20))
    export.classify([c])
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


def report(charges=(), uncharged=(), warnings=()):
    cs = list(charges)
    export.classify(cs)
    return export.Report(date(2025, 3, 1), date(2025, 3, 31), datetime(2025, 4, 1, 9), cs, list(uncharged),
                         list(warnings))


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
    o = export.OrderView(ONE, date(2025, 3, 1), 12.99, None, False,
                         [export.ShipmentView("Delivered March 15", [item()])], [item()])
    md = export.render_markdown(report([charge(o=o)]))
    assert "**Shipment: Delivered March 15**" in md and md.count("**Book**") == 1


def test_refund_notes_its_purchase():
    md = export.render_markdown(report([charge(day=14), charge(refund=True, day=20)]))
    assert "Refund" in md and "2025-03-20" in md and "2025-03-14" in md


def test_order_line_and_statement_text():
    c = charge()
    c.seller = "AMZN Digital"
    c.order_link = "https://www.amazon.com/o1"
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


def txn(day, number=ONE, total=-12.99):
    return SimpleNamespace(completed_date=day, grand_total=total, is_refund=total > 0,
                           payment_method="Visa ****1234", payment_method_last_4="1234", order_number=number,
                           order_details_link=None, seller=None)


def patch_txns(monkeypatch, fn):
    monkeypatch.setattr(export, "AmazonTransactions", lambda s: SimpleNamespace(get_transactions=fn))


def patch_orders(monkeypatch, **methods):
    monkeypatch.setattr(export, "AmazonOrders", lambda s: SimpleNamespace(**methods))


def lib_order(number=ONE, placed=date(2025, 3, 2)):
    return SimpleNamespace(order_number=number, order_placed_date=placed, grand_total=1.0, order_details_link=None,
                           cancelled=False, items=[], shipments=[])


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
    export.fetch_charges(SimpleNamespace(login=lambda: logins.append(1)), date(2025, 3, 1), date(2025, 3, 2),
                         date(2025, 3, 3))
    assert (len(calls), len(logins)) == (2, 1)


def test_attach_orders_looks_up_each_order_once_and_survives_any_error(monkeypatch):
    calls = []

    def get_order(n):
        calls.append(n)
        if n == "bad1":
            raise AmazonOrdersNotFoundError("gone")
        if n == "bad2":
            raise TypeError("title was None")
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
        if order_filter:
            raise RuntimeError("page changed")
        return []

    patch_orders(monkeypatch, get_orders=get_orders)
    _, warnings = export.fetch_uncharged_orders(object(), [], date(2025, 3, 1), date(2025, 3, 31))
    assert len(warnings) == 1 and "digital" in warnings[0] and "2025" in warnings[0]
