"""Export Amazon.com charges for a date range, each tied to the order and items it paid for."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

MATCHED, SPLIT, REFUND, UNRESOLVED = "Matched", "Split", "Refund", "Unresolved"


@dataclass
class ItemView:
    title: str | None
    quantity: int | None
    price: float | None
    asin: str | None
    link: str | None
    image_link: str | None


@dataclass
class ShipmentView:
    status: str | None
    items: list[ItemView]


@dataclass
class OrderView:
    order_number: str | None
    placed_date: date | None
    grand_total: float | None
    link: str | None
    cancelled: bool
    shipments: list[ShipmentView]
    items: list[ItemView]

    @property
    def has_items(self) -> bool:
        return bool(self.items) or any(shipment.items for shipment in self.shipments)


@dataclass
class Related:
    date: date
    amount: float
    is_refund: bool


@dataclass
class Charge:
    date: date
    amount: float  # always positive, as on a statement
    is_refund: bool
    payment_method: str | None
    last_4: str | None
    order_number: str | None
    order_link: str | None
    seller: str | None
    order: OrderView | None = None
    lookup_error: str | None = None
    status: str = ""
    reason: str | None = None
    charge_index: int | None = None
    charge_count: int | None = None
    charges_total: float | None = None
    related: list[Related] = field(default_factory=list)


@dataclass
class Report:
    start: date
    end: date
    generated_at: datetime
    charges: list[Charge]
    uncharged_orders: list[OrderView]
    warnings: list[str]


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


def to_charge(txn) -> Charge:
    return Charge(
        date=txn.completed_date,
        amount=abs(round(txn.grand_total, 2)),
        is_refund=txn.is_refund,
        payment_method=txn.payment_method,
        last_4=txn.payment_method_last_4,
        order_number=txn.order_number,
        order_link=txn.order_details_link,
        seller=txn.seller,
    )


def _to_item_view(item) -> ItemView:
    return ItemView(item.title, item.quantity, item.price, item.asin, item.link, item.image_link)


def to_order_view(order) -> OrderView:
    return OrderView(
        order_number=order.order_number,
        placed_date=order.order_placed_date,
        grand_total=order.grand_total,
        link=order.order_details_link,
        cancelled=order.cancelled,
        shipments=[ShipmentView(s.delivery_status, [_to_item_view(i) for i in s.items]) for s in order.shipments],
        items=[_to_item_view(i) for i in order.items],
    )


def _same_cents(a: float, b: float) -> bool:
    return round(a, 2) == round(b, 2)


def _status(charge: Charge) -> tuple[str, str | None]:
    if charge.is_refund:
        return REFUND, None
    if not charge.order_number:
        return UNRESOLVED, "Amazon did not link this charge to an order"
    if charge.lookup_error:
        return UNRESOLVED, f"Order lookup failed: {charge.lookup_error}"
    if charge.order is None:
        return UNRESOLVED, "Order details were not loaded"
    if not charge.order.has_items:
        return UNRESOLVED, "Order has no items"
    if charge.order.grand_total is None:
        return UNRESOLVED, "Order total not available"
    if (charge.charge_count or 0) > 1 or not _same_cents(charge.amount, charge.order.grand_total):
        return SPLIT, None
    return MATCHED, None


def classify(charges: list[Charge]) -> None:
    """Set each charge's status, its numbering within its order, and links between purchases and refunds."""
    purchases: dict[str, list[Charge]] = defaultdict(list)
    refunds: dict[str, list[Charge]] = defaultdict(list)
    for charge in charges:
        if charge.order_number:
            (refunds if charge.is_refund else purchases)[charge.order_number].append(charge)

    for number, group in purchases.items():
        group.sort(key=lambda c: c.date)
        total = round(sum(c.amount for c in group), 2)
        for index, charge in enumerate(group, start=1):
            charge.charge_index, charge.charge_count, charge.charges_total = index, len(group), total
            charge.related = [Related(r.date, r.amount, True) for r in refunds.get(number, [])]

    for number, group in refunds.items():
        for refund in group:
            refund.related = [Related(p.date, p.amount, False) for p in purchases.get(number, [])]

    for charge in charges:
        charge.status, charge.reason = _status(charge)
