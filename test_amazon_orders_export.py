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


def test_thumbnail_url_requests_small_image_from_amazon_cdn():
    base = "https://m.media-amazon.com/images/I/71abcXYZ"
    assert export.thumbnail_url(f"{base}.jpg") == f"{base}._SL100_.jpg"
    assert export.thumbnail_url(f"{base}._SS142_.jpg") == f"{base}._SL100_.jpg"
    assert export.thumbnail_url(f"{base}._AC_UL600_SR600,400_.png") == f"{base}._SL100_.png"
    routed = "https://m.media-amazon.com/images/W/BW_MEDIAX_AVIF_MEASUREMENT_1306696-T4/images/I/71DUrSuIEgL"
    assert export.thumbnail_url(f"{routed}._SS284_.jpg") == f"{routed}._SL100_.jpg"
    assert export.thumbnail_url("https://example.com/images/I/x.jpg") == "https://example.com/images/I/x.jpg"
    assert export.thumbnail_url("https://m.media-amazon.com/i.jpg") == "https://m.media-amazon.com/i.jpg"


def test_item_image_uses_thumbnail():
    big = export.ItemView("Book", 1, 12.99, None, None, "https://m.media-amazon.com/images/I/71abc._SS500_.jpg")
    md = export.render_markdown(report([charge(o=export.OrderView(ONE, None, 12.99, None, False, [], [big]))]))
    assert "![Book](https://m.media-amazon.com/images/I/71abc._SL100_.jpg)" in md


def test_split_charge_block():
    a, b = charge(amount=12.99, o=order(total=27.98), day=14), charge(amount=14.99, o=order(total=27.98), day=15)
    md = export.render_markdown(report([a, b]))
    assert r"### 2025-03-14 · $12.99 · Visa \*\*\*\*1234 · Split" in md
    assert "Charge 1 of 2 for this order. Charges in this period, on any card, total $27.98." in md
    assert "These items can't be attributed to this specific charge." in md
    assert "**Book**<br>qty 1<br>$12.99" in md and "![Book](https://m.media-amazon.com/i.jpg)" in md


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


def test_items_render_as_image_and_details_table():
    piped = export.ItemView("A | B", 2, 5.0, None, None, None)
    o = export.OrderView(ONE, None, 17.99, None, False, [], [item(), piped])
    md = export.render_markdown(report([charge(amount=17.99, o=o)]))
    assert ("|  |  |\n|---|---|\n"
            "| **Book**<br>qty 1<br>$12.99<br>[product page](https://www.amazon.com/dp/B000000001) | "
            "![Book](https://m.media-amazon.com/i.jpg) |\n"
            r"| **A \| B**<br>qty 2<br>$5.00 |  |") in md


def test_single_item_fills_the_header_row():
    md = export.render_markdown(report([charge()]))
    assert "|  |  |" not in md
    assert "| **Book**<br>qty 1<br>$12.99<br>[product page](https://www.amazon.com/dp/B000000001) | " \
           "![Book](https://m.media-amazon.com/i.jpg) |\n|---|---|" in md


def test_shipment_items_share_one_table_without_shipment_label():
    o = export.OrderView(ONE, date(2025, 3, 1), 25.98, None, False,
                         [export.ShipmentView("Delivered March 15", [item("Book")]),
                          export.ShipmentView("Delivered March 16", [item("Pen")])], [item("Book"), item("Pen")])
    md = export.render_markdown(report([charge(amount=25.98, o=o)]))
    assert "Shipment" not in md and "Delivered" not in md
    assert md.count("**Book**") == 1 and md.count("**Pen**") == 1 and md.count("|---|---|") == 1


def test_refund_notes_its_purchase():
    md = export.render_markdown(report([charge(day=14), charge(refund=True, day=20)]))
    assert "Refund" in md and "2025-03-20" in md and "2025-03-14" in md


def test_refund_heading_shows_negative_amount():
    md = export.render_markdown(report([charge(day=14), charge(refund=True, day=20)]))
    assert "### 2025-03-20 · -$12.99 · " in md and "### 2025-03-14 · $12.99 · " in md


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


def test_every_entry_ends_with_a_separator():
    o = export.OrderView("D01-1", date(2025, 3, 2), None, None, True, [], [item()])
    md = export.render_markdown(report([charge(day=1), charge(o=order(total=None), day=20)], uncharged=[o]))
    entries = [e.split("\n\n## ")[0].rstrip("\n") for e in md.split("### ")[1:]]
    assert len(entries) == 3
    # The blank line matters: "---" directly under text would turn that text into a heading.
    assert all(e.endswith("\n\n---") for e in entries)


def test_json_round_trips():
    data = json.loads(export.render_json(report([charge()])))
    assert data["charges"][0]["status"] == "Matched" and data["start"] == "2025-03-01"


def txn(day, number=ONE, total=-12.99, card="1234"):
    return SimpleNamespace(completed_date=day, grand_total=total, is_refund=total > 0,
                           payment_method=f"Visa ****{card}" if card else "Amazon Gift Card",
                           payment_method_last_4=card, order_number=number,
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


def test_credentials_problem():
    signed_out = SimpleNamespace(auth_cookies_stored=lambda: False)
    assert "AMAZON_USERNAME" in export.credentials_problem(signed_out, {})
    assert export.credentials_problem(signed_out, {"AMAZON_USERNAME": "u", "AMAZON_PASSWORD": "p"}) is None
    assert export.credentials_problem(SimpleNamespace(auth_cookies_stored=lambda: True), {}) is None


def test_cookie_file_without_sign_in_cookies_is_not_a_saved_session(tmp_path):
    jar = tmp_path / "jar.json"
    jar.write_text("[]")
    config = export.AmazonOrdersConfig(config_path=str(tmp_path / "c.yml"), data={"cookie_jar_path": str(jar)})
    assert "AMAZON_USERNAME" in export.credentials_problem(export.AmazonSession(config=config), {})


def test_fetch_charges_filters_range_and_uses_lookback(monkeypatch):
    seen = {}

    def get(days):
        seen["days"] = days
        return [txn(date(2025, 2, 28)), txn(date(2025, 3, 1)), txn(date(2025, 3, 31)), txn(date(2025, 4, 1))]

    patch_txns(monkeypatch, get)
    charges, skipped, _ = export.fetch_charges(object(), date(2025, 3, 1), date(2025, 3, 31), date(2025, 4, 10))
    assert seen["days"] == 41 and [c.date.day for c in charges] == [1, 31] and skipped == 0


def test_fetch_charges_reports_order_numbers_charged_outside_the_range(monkeypatch):
    patch_txns(monkeypatch, lambda days: [txn(date(2025, 4, 2), number="LATER"), txn(date(2025, 3, 5))])
    _, _, seen = export.fetch_charges(object(), date(2025, 3, 1), date(2025, 3, 31), date(2025, 4, 10))
    assert seen == {"LATER", ONE}


def test_fetch_charges_counts_rows_the_library_skipped(monkeypatch):
    def get(days):
        logging.getLogger("amazonorders.transactions").warning("skipped a row")
        return []

    patch_txns(monkeypatch, get)
    assert export.fetch_charges(object(), date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)) == ([], 1, set())


def set_credentials(monkeypatch):
    monkeypatch.setenv("AMAZON_USERNAME", "u")
    monkeypatch.setenv("AMAZON_PASSWORD", "p")


def test_expired_session_without_credentials_is_a_clear_error_and_does_not_log_in(monkeypatch):
    monkeypatch.delenv("AMAZON_USERNAME", raising=False)
    monkeypatch.delenv("AMAZON_PASSWORD", raising=False)
    logins = []

    def get(days):
        raise AmazonOrdersAuthRedirectError("expired")

    patch_txns(monkeypatch, get)
    with pytest.raises(export.LoginFailed, match="AMAZON_USERNAME"):
        export.fetch_charges(SimpleNamespace(login=lambda: logins.append(1)), date(2025, 3, 1), date(2025, 3, 2),
                             date(2025, 3, 3))
    assert logins == []


def test_skipped_row_count_is_reset_for_the_retry_after_login(monkeypatch):
    set_credentials(monkeypatch)
    calls = []

    def get(days):
        calls.append(1)
        if len(calls) == 1:
            logging.getLogger("amazonorders.transactions").warning("skipped before the redirect")
            raise AmazonOrdersAuthRedirectError("expired")
        return []

    patch_txns(monkeypatch, get)
    result = export.fetch_charges(SimpleNamespace(login=lambda: None), date(2025, 3, 1), date(2025, 3, 2),
                                  date(2025, 3, 3))
    assert result[1] == 0


def test_attach_orders_logs_in_again_once_when_redirected(monkeypatch):
    set_credentials(monkeypatch)
    calls, logins = [], []

    def get_order(n):
        calls.append(n)
        if len(calls) == 1:
            raise AmazonOrdersAuthRedirectError("sign in again")
        return lib_order(n)

    patch_orders(monkeypatch, get_order=get_order)
    c = charge(o=None)
    export.attach_orders(SimpleNamespace(login=lambda: logins.append(1)), [c])
    assert c.order is not None and c.lookup_error is None and len(logins) == 1


def test_attach_orders_stops_when_still_redirected_after_login(monkeypatch):
    set_credentials(monkeypatch)
    logins = []

    def get_order(n):
        raise AmazonOrdersAuthRedirectError("sign in again")

    patch_orders(monkeypatch, get_order=get_order)
    with pytest.raises(export.LoginFailed):
        export.attach_orders(SimpleNamespace(login=lambda: logins.append(1)),
                             [charge(number="A", o=None), charge(number="B", o=None)])
    assert len(logins) == 1


def test_uncharged_orders_stops_logging_in_after_a_failed_login_and_warns(monkeypatch):
    monkeypatch.delenv("AMAZON_USERNAME", raising=False)
    monkeypatch.delenv("AMAZON_PASSWORD", raising=False)
    calls = []

    def get_orders(year, order_filter=None):
        calls.append(year)
        raise AmazonOrdersAuthRedirectError("sign in again")

    patch_orders(monkeypatch, get_orders=get_orders)
    _, warnings = export.fetch_uncharged_orders(SimpleNamespace(login=lambda: None), [], date(2024, 3, 1),
                                                date(2025, 3, 31))
    assert len(calls) == 1 and len(warnings) == 1 and "AMAZON_USERNAME" in warnings[0]


def test_uncharged_orders_excludes_orders_charged_outside_the_range(monkeypatch):
    patch_orders(monkeypatch, get_orders=lambda year, order_filter=None: [lib_order("LATER"), lib_order("NONE")])
    found, _ = export.fetch_uncharged_orders(object(), [], date(2025, 3, 1), date(2025, 3, 31), {"LATER"})
    assert [o.order_number for o in found] == ["NONE"]


def test_fetch_charges_logs_in_again_once_when_session_expired(monkeypatch):
    set_credentials(monkeypatch)
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


ARGS = ["--start", "2025-03-01", "--end", "2025-03-31"]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("AMAZON_USERNAME", "u")
    monkeypatch.setenv("AMAZON_PASSWORD", "p")
    monkeypatch.setattr(export, "build_config", lambda: export.AmazonOrdersConfig(
        config_path=str(tmp_path / "c.yml"), data={"cookie_jar_path": str(tmp_path / "jar.json")}))
    monkeypatch.setattr(export, "AmazonSession", lambda config: SimpleNamespace(
        login=lambda: None, auth_cookies_stored=lambda: False))
    patch_txns(monkeypatch, lambda days: [txn(date(2025, 3, 14))])
    patch_orders(monkeypatch, get_order=lambda n: lib_order(n), get_orders=lambda year, order_filter=None: [])
    return tmp_path / "sub" / "out.md"


def run(out, today=date(2025, 4, 1), extra=()):
    return export.main([*ARGS, "--out", str(out), *extra], today=today)


def test_main_writes_report_creating_parent_folder(wired):
    assert run(wired) == 0 and "### 2025-03-14" in wired.read_text(encoding="utf-8")


def test_main_json(wired):
    assert run(wired, extra=["--json"]) == 0 and "charges" in json.loads(wired.read_text(encoding="utf-8"))


def test_main_empty_period_writes_report(wired, monkeypatch):
    patch_txns(monkeypatch, lambda days: [])
    assert run(wired) == 0 and "No charges found" in wired.read_text(encoding="utf-8")


def test_main_reports_skipped_rows_at_top(wired, monkeypatch):
    def get(days):
        logging.getLogger("amazonorders.transactions").warning("x")
        return []

    patch_txns(monkeypatch, get)
    assert run(wired) == 0 and "1 row" in wired.read_text(encoding="utf-8")


def test_main_login_failure_exits_without_report(wired, monkeypatch, capsys):
    from amazonorders.exception import AmazonOrdersAuthError

    def fail():
        raise AmazonOrdersAuthError("bad login")

    monkeypatch.setattr(export, "AmazonSession", lambda config: SimpleNamespace(
        login=fail, auth_cookies_stored=lambda: False))
    assert run(wired) == 1 and not wired.exists() and "bad login" in capsys.readouterr().err


def test_main_reports_failed_relogin_as_a_login_failure(wired, monkeypatch, capsys):
    from amazonorders.exception import AmazonOrdersAuthError
    attempts = []

    def login():
        attempts.append(1)
        if len(attempts) > 1:
            raise AmazonOrdersAuthError("wrong password")

    def expired(days):
        raise AmazonOrdersAuthRedirectError("expired")

    monkeypatch.setattr(export, "AmazonSession", lambda config: SimpleNamespace(
        login=login, auth_cookies_stored=lambda: True))
    patch_txns(monkeypatch, expired)
    err_code = run(wired)
    err = capsys.readouterr().err
    assert err_code == 1 and not wired.exists() and "Could not log in" in err and "transactions" not in err


def test_refund_with_failed_lookup_says_so():
    md = export.render_markdown(report([charge(refund=True, o=None, error="page changed")]))
    assert "Order details not available: page changed" in md


def test_view_order_falls_back_when_transaction_link_is_relative():
    c = charge()
    c.order_link = "/gp/your-account/order-details?orderID=1"
    assert "[View order](https://www.amazon.com/o)" in export.render_markdown(report([c]))


def test_main_feed_failure_exits_without_report(wired, monkeypatch):
    def boom(days):
        raise TypeError("missing total")

    patch_txns(monkeypatch, boom)
    assert run(wired) == 1 and not wired.exists()


def test_main_no_credentials_is_a_clear_error(wired, monkeypatch, capsys):
    monkeypatch.delenv("AMAZON_USERNAME")
    monkeypatch.delenv("AMAZON_PASSWORD")
    assert run(wired) == 1 and "AMAZON_USERNAME" in capsys.readouterr().err


def test_main_unwritable_output_is_a_clear_error(wired, tmp_path, capsys):
    (tmp_path / "blocker").write_text("")
    assert run(tmp_path / "blocker" / "out.md") == 1 and "Could not write" in capsys.readouterr().err


TWO = "111-0000000-0000002"
MARCH_14, MARCH_15 = date(2025, 3, 14), date(2025, 3, 15)


def lib_order_with_item(number):
    book = SimpleNamespace(title="Book", quantity=1, price=15.0, asin="B1", link=None, image_link=None)
    return SimpleNamespace(order_number=number, order_placed_date=date(2025, 3, 2), grand_total=15.0,
                           order_details_link=None, cancelled=False, items=[book], shipments=[])


def card_report(out, monkeypatch, txns):
    patch_txns(monkeypatch, lambda days: txns)
    assert run(out, extra=["--card", "1234"]) == 0
    return out.read_text(encoding="utf-8")


def test_parse_args_reads_card_and_keeps_leading_zeros():
    assert export.parse_args([*ARGS, "--card", "0042"]).card == "0042"


def test_parse_args_card_is_optional():
    assert export.parse_args(ARGS).card is None


@pytest.mark.parametrize("card", ["123", "12345", "12a4", "", "١٢٣٤"])
def test_parse_args_rejects_anything_but_four_digits(card):
    with pytest.raises(SystemExit) as e:
        export.parse_args([*ARGS, "--card", card])
    assert e.value.code == 2


def test_card_report_keeps_only_charges_on_that_card(wired, monkeypatch):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE), txn(MARCH_15, TWO, card="9999")])
    assert ONE in md and TWO not in md


def test_card_report_includes_refunds_on_that_card(wired, monkeypatch):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE, total=5.0)])
    assert "Refund: 1" in md


def test_card_report_counts_only_that_card(wired, monkeypatch, capsys):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE, total=5.0), txn(MARCH_15, TWO, total=6.0, card="9999")])
    assert "Refund: 1" in md and "Refund: 1" in capsys.readouterr().err


def test_card_report_keeps_numbering_of_an_order_split_across_cards(wired, monkeypatch):
    patch_orders(monkeypatch, get_order=lib_order_with_item, get_orders=lambda year, order_filter=None: [])
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE, total=-10.0), txn(MARCH_15, ONE, total=-5.0, card="9999")])
    assert "Charge 1 of 2" in md and "on any card, total $15.00" in md
    assert "### 2025-03-15" not in md


def test_card_report_only_looks_up_orders_on_that_card(wired, monkeypatch):
    looked_up = []
    patch_orders(monkeypatch, get_order=lambda n: looked_up.append(n) or lib_order(n),
                 get_orders=lambda year, order_filter=None: [])
    card_report(wired, monkeypatch, [txn(MARCH_14, ONE), txn(MARCH_15, TWO, card="9999")])
    assert looked_up == [ONE]


def test_card_report_warns_about_charges_with_no_card_number(wired, monkeypatch):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE), txn(MARCH_15, TWO, card=None)])
    assert TWO not in md
    assert "**Warning:** 1 charge had no card number" in md


def test_card_report_has_no_warning_when_every_charge_has_a_card_number(wired, monkeypatch):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE), txn(MARCH_15, TWO, card="9999")])
    assert "no card number" not in md


def test_card_report_skips_orders_with_no_charge(wired, monkeypatch):
    checked = []
    patch_orders(monkeypatch, get_order=lambda n: lib_order(n),
                 get_orders=lambda year, order_filter=None: checked.append(year) or [lib_order(TWO)])
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE)])
    assert "Orders with no charge found" not in md and checked == []


def test_card_report_title_names_the_card(wired, monkeypatch):
    md = card_report(wired, monkeypatch, [txn(MARCH_14, ONE)])
    assert md.splitlines()[0] == "# Amazon charges, 2025-03-01 to 2025-03-31, card ending 1234"


def test_card_report_json_names_the_card(wired, monkeypatch):
    patch_txns(monkeypatch, lambda days: [txn(MARCH_14, ONE)])
    assert run(wired, extra=["--card", "1234", "--json"]) == 0
    assert json.loads(wired.read_text(encoding="utf-8"))["card"] == "1234"


def test_card_report_default_filename_does_not_overwrite_the_full_report(wired, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert export.main([*ARGS, "--card", "1234"], today=date(2025, 4, 1)) == 0
    assert (tmp_path / "reports" / "amazon-charges_2025-03-01_2025-03-31_card-1234.md").exists()
    assert not (tmp_path / "reports" / "amazon-charges_2025-03-01_2025-03-31.md").exists()


def test_report_without_card_keeps_every_charge_and_checks_uncharged_orders(wired, monkeypatch):
    patch_orders(monkeypatch, get_order=lambda n: lib_order(n),
                 get_orders=lambda year, order_filter=None: [lib_order("111-0000000-0000003")])
    patch_txns(monkeypatch, lambda days: [txn(MARCH_14, ONE), txn(MARCH_15, TWO, card=None)])
    assert run(wired) == 0
    md = wired.read_text(encoding="utf-8")
    assert ONE in md and TWO in md
    assert "no card number" not in md and "Orders with no charge found" in md
    assert md.splitlines()[0] == "# Amazon charges, 2025-03-01 to 2025-03-31"


def test_main_prints_summary(wired, capsys):
    run(wired)
    err = capsys.readouterr().err
    assert "Unresolved: 1" in err and str(wired) in err
