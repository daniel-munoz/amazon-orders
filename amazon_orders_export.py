"""Export Amazon.com charges for a date range, each tied to the order and items it paid for."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersAuthRedirectError
from amazonorders.orders import AmazonOrders
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions

logger = logging.getLogger("amazon_orders_export")

MATCHED, SPLIT, REFUND, UNRESOLVED = "Matched", "Split", "Refund", "Unresolved"
DIGITAL_ORDERS_FILTER = "digital-orders"


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


def escape_md(text: str | None) -> str:
    if text is None:
        return "not available"
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", " ".join(text.split()))


def safe_url(url: str | None) -> str | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _money(amount: float | None) -> str:
    return "not available" if amount is None else f"${amount:,.2f}"


def _item_lines(item: ItemView) -> list[str]:
    title = escape_md(item.title or "Title not available")
    quantity = "qty not available" if item.quantity is None else f"qty {item.quantity}"
    price = "price not available" if item.price is None else _money(item.price)
    details = f"**{title}**, {quantity}, {price}"
    product_page = safe_url(item.link)
    if product_page:
        details += f", [product page]({product_page})"
    image = safe_url(item.image_link)
    return [f"- ![{title}]({image})\n  {details}"] if image else [f"- {details}"]


def _items_block(order: OrderView) -> list[str]:
    shipments = [s for s in order.shipments if s.items]
    if not shipments:
        return ["\n".join(line for item in order.items for line in _item_lines(item))] if order.items else []
    blocks: list[str] = []
    for shipment in shipments:
        blocks.append(f"**Shipment: {escape_md(shipment.status)}**")
        blocks.append("\n".join(line for item in shipment.items for line in _item_lines(item)))
    return blocks


def _order_summary(order: OrderView) -> str:
    placed = order.placed_date or "not available"
    summary = f"Order {escape_md(order.order_number)}, placed {placed}, order total {_money(order.grand_total)}"
    return f"{summary} (cancelled)" if order.cancelled else summary


def _status_lines(charge: Charge) -> list[str]:
    lines: list[str] = []
    if charge.status == UNRESOLVED:
        lines.append(f"**Unresolved:** {escape_md(charge.reason)}")
        if charge.order and charge.order.has_items:
            lines.append("The items below come from the linked order, amount not verified.")
    elif charge.status == SPLIT:
        if (charge.charge_count or 0) > 1:
            lines.append(f"Charge {charge.charge_index} of {charge.charge_count} for this order. "
                         f"Charges in this report total {_money(charge.charges_total)}.")
        else:
            lines.append("This is the only charge found for this order, and it differs from the order total.")
        lines.append("These items can't be attributed to this specific charge.")
    for other in charge.related:
        if other.is_refund:
            lines.append(f"Refund of {_money(other.amount)} on {other.date} for this order.")
        else:
            lines.append(f"Refunds a charge of {_money(other.amount)} on {other.date}.")
    return lines


def _charge_block(charge: Charge) -> str:
    heading = f"### {charge.date} · {_money(charge.amount)} · {escape_md(charge.payment_method)} · {charge.status}"
    parts = [heading]
    if charge.order:
        parts.append(_order_summary(charge.order))
    elif charge.order_number:
        parts.append(f"Order {escape_md(charge.order_number)}")
    if charge.seller:
        parts.append(f"Statement text: {escape_md(charge.seller)}")
    parts.extend(_status_lines(charge))
    order_link = safe_url(charge.order_link or (charge.order.link if charge.order else None))
    if order_link:
        parts.append(f"[View order]({order_link})")
    if charge.order:
        parts.extend(_items_block(charge.order))
    return "\n\n".join(parts)


def _uncharged_block(order: OrderView) -> str:
    parts = [f"### {_order_summary(order)}"]
    order_link = safe_url(order.link)
    if order_link:
        parts.append(f"[View order]({order_link})")
    parts.extend(_items_block(order))
    return "\n\n".join(parts)


def status_counts(charges: list[Charge]) -> str:
    return " · ".join(f"{s}: {sum(c.status == s for c in charges)}" for s in (MATCHED, SPLIT, REFUND, UNRESOLVED))


def render_markdown(report: Report) -> str:
    charges = sorted(report.charges, key=lambda c: (c.date, c.order_number or ""))
    unresolved = [c for c in charges if c.status == UNRESOLVED]
    resolved = [c for c in charges if c.status != UNRESOLVED]

    parts = [
        f"# Amazon charges, {report.start} to {report.end}",
        f"Generated {report.generated_at:%Y-%m-%d %H:%M}",
        status_counts(charges),
    ]
    parts.extend(f"> **Warning:** {escape_md(warning)}" for warning in report.warnings)
    if not charges:
        parts.append("No charges found in this period.")
    if unresolved:
        parts.append("## Unresolved charges")
        parts.extend(_charge_block(c) for c in unresolved)
    if resolved:
        parts.append("## Charges")
        parts.extend(_charge_block(c) for c in resolved)
    if report.uncharged_orders:
        parts.append("## Orders with no charge found")
        parts.extend(_uncharged_block(o) for o in report.uncharged_orders)
    return "\n\n".join(parts) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(asdict(report), default=str, indent=2)


def build_config(config_path: str | None = None) -> AmazonOrdersConfig:
    return AmazonOrdersConfig(config_path=config_path,
                              data={"warn_on_missing_required_field": True, "max_auth_attempts": 3})


def credentials_problem(config, environ: Mapping[str, str]) -> str | None:
    """Explain what's missing when there's neither a saved session nor credentials to log in with."""
    if os.path.exists(config.cookie_jar_path) or (environ.get("AMAZON_USERNAME") and environ.get("AMAZON_PASSWORD")):
        return None
    return "No saved Amazon session found. Set AMAZON_USERNAME and AMAZON_PASSWORD to log in."


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def fetch_charges(session, start: date, end: date, today: date) -> tuple[list[Charge], int]:
    """Return the charges in range, and how many transaction rows the library skipped as unreadable."""
    days = lookback_days(start, today)
    counter = _WarningCounter()
    library_logger = logging.getLogger("amazonorders.transactions")
    library_logger.addHandler(counter)
    try:
        try:
            transactions = AmazonTransactions(session).get_transactions(days=days)
        except AmazonOrdersAuthRedirectError:
            # The saved session expired; login() starts fresh after a redirect to sign-in.
            counter.count = 0
            session.login()
            transactions = AmazonTransactions(session).get_transactions(days=days)
    finally:
        library_logger.removeHandler(counter)
    charges = [to_charge(t) for t in transactions if start <= t.completed_date <= end]
    return charges, counter.count


def attach_orders(session, charges: list[Charge]) -> None:
    """Look up each distinct order once; a failure only affects the charges on that order."""
    amazon_orders = AmazonOrders(session)
    results: dict[str, OrderView | str] = {}
    for charge in charges:
        number = charge.order_number
        if not number:
            continue
        if number not in results:
            try:
                results[number] = to_order_view(amazon_orders.get_order(number))
            except Exception as error:
                logger.warning("Could not load order %s: %s", number, error)
                results[number] = str(error) or type(error).__name__
        result = results[number]
        if isinstance(result, str):
            charge.lookup_error = result
        else:
            charge.order = result


def fetch_uncharged_orders(session, charges: list[Charge], start: date, end: date) -> tuple[list[OrderView], list[str]]:
    """Find orders placed in range with no charge, from both the regular and the digital history."""
    amazon_orders = AmazonOrders(session)
    charged = {c.order_number for c in charges if c.order_number}
    found: dict[str, OrderView] = {}
    warnings: list[str] = []
    for year in years_in_range(start, end):
        for order_filter, label in ((None, "regular"), (DIGITAL_ORDERS_FILTER, "digital")):
            try:
                orders = amazon_orders.get_orders(year=year, order_filter=order_filter)
                views = [to_order_view(o) for o in orders]
            except Exception as error:
                logger.warning("Could not check %s orders for %s: %s", label, year, error)
                warnings.append(f"Could not check {label} orders for {year}, so some orders may be missing here: {error}")
                continue
            for view in views:
                in_range = view.placed_date is None or start <= view.placed_date <= end
                if in_range and view.order_number not in charged:
                    found[view.order_number] = view
    ordered = sorted(found.values(), key=lambda o: (o.placed_date is None, o.placed_date or date.min))
    return ordered, warnings
