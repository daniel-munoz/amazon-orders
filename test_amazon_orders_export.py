from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from amazonorders.conf import AmazonOrdersConfig
from amazonorders.entity.transaction import Transaction

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
