"""Export Amazon.com charges for a date range, each tied to the order and items it paid for."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from collections.abc import Collection, Mapping
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
    card: str | None = None


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid YYYY-MM-DD date")


def _last_four(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not the last four digits of a card")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_iso_date, required=True, help="first charge date, YYYY-MM-DD")
    parser.add_argument("--end", type=_iso_date, required=True, help="last charge date, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, help="output path (default: under reports/)")
    parser.add_argument("--json", action="store_true", help="write JSON instead of Markdown")
    parser.add_argument("--card", type=_last_four, help="only include charges on the card with these last four digits")
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end must not be before --start")
    return args


def default_out(start: date, end: date, as_json: bool, card: str | None = None) -> Path:
    extension = "json" if as_json else "md"
    card_suffix = f"_card-{card}" if card else ""
    return Path("reports") / f"amazon-charges_{start}_{end}{card_suffix}.{extension}"


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
                         f"Charges in this period, on any card, total {_money(charge.charges_total)}.")
        else:
            lines.append("This is the only charge found for this order, and it differs from the order total.")
        lines.append("These items can't be attributed to this specific charge.")
    elif charge.status == REFUND and charge.order is None:
        detail = charge.lookup_error or "Amazon did not link this refund to an order"
        lines.append(f"Order details not available: {escape_md(detail)}")
    for other in charge.related:
        if other.is_refund:
            lines.append(f"Refund of {_money(other.amount)} on {other.date} for this order.")
        else:
            lines.append(f"Refunds a charge of {_money(other.amount)} on {other.date}.")
    return lines


ENTRY_SEPARATOR = "---"


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
    order_link = safe_url(charge.order_link) or safe_url(charge.order.link if charge.order else None)
    if order_link:
        parts.append(f"[View order]({order_link})")
    if charge.order:
        parts.extend(_items_block(charge.order))
    parts.append(ENTRY_SEPARATOR)
    return "\n\n".join(parts)


def _uncharged_block(order: OrderView) -> str:
    parts = [f"### {_order_summary(order)}"]
    order_link = safe_url(order.link)
    if order_link:
        parts.append(f"[View order]({order_link})")
    parts.extend(_items_block(order))
    parts.append(ENTRY_SEPARATOR)
    return "\n\n".join(parts)


def status_counts(charges: list[Charge]) -> str:
    return " · ".join(f"{s}: {sum(c.status == s for c in charges)}" for s in (MATCHED, SPLIT, REFUND, UNRESOLVED))


def render_markdown(report: Report) -> str:
    charges = sorted(report.charges, key=lambda c: (c.date, c.order_number or ""))
    unresolved = [c for c in charges if c.status == UNRESOLVED]
    resolved = [c for c in charges if c.status != UNRESOLVED]

    title = f"# Amazon charges, {report.start} to {report.end}"
    parts = [
        f"{title}, card ending {report.card}" if report.card else title,
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


class LoginFailed(Exception):
    """Amazon needs a sign-in that couldn't be completed, so fetching can't continue."""


def _has_credentials(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("AMAZON_USERNAME") and environ.get("AMAZON_PASSWORD"))


def credentials_problem(session, environ: Mapping[str, str]) -> str | None:
    """Explain what's missing when there's neither a saved session nor credentials to log in with."""
    if session.auth_cookies_stored() or _has_credentials(environ):
        return None
    return "No saved Amazon session found. Set AMAZON_USERNAME and AMAZON_PASSWORD to log in."


def _login_again(session) -> None:
    if not _has_credentials(os.environ):
        raise LoginFailed("The saved Amazon session has expired. Set AMAZON_USERNAME and AMAZON_PASSWORD to log in.")
    try:
        # login() starts fresh after Amazon has redirected a request to sign-in.
        session.login()
    except Exception as error:
        raise LoginFailed(f"Could not log in to Amazon: {error}") from error


def _retry_after_login(session, fetch):
    """Run fetch, signing in again and retrying once if Amazon rejects the session."""
    try:
        return fetch()
    except AmazonOrdersAuthRedirectError:
        _login_again(session)
    try:
        return fetch()
    except AmazonOrdersAuthRedirectError as error:
        raise LoginFailed(f"Could not log in to Amazon: it still asks to sign in. {error}") from error


class _WarningCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def fetch_charges(session, start: date, end: date, today: date) -> tuple[list[Charge], int, set[str]]:
    """Return the charges in range, how many rows the library skipped, and every order number seen in the feed."""
    days = lookback_days(start, today)
    counter = _WarningCounter()
    library_logger = logging.getLogger("amazonorders.transactions")
    library_logger.addHandler(counter)

    def fetch():
        counter.count = 0
        return AmazonTransactions(session).get_transactions(days=days)

    try:
        transactions = _retry_after_login(session, fetch)
    finally:
        library_logger.removeHandler(counter)
    charges = [to_charge(t) for t in transactions if start <= t.completed_date <= end]
    return charges, counter.count, {t.order_number for t in transactions if t.order_number}


def no_card_number_warnings(charges: list[Charge]) -> list[str]:
    missing = sum(not c.last_4 for c in charges)
    if not missing:
        return []
    subject, verb = ("1 charge", "was") if missing == 1 else (f"{missing} charges", "were")
    return [f"{subject} had no card number and {verb} left out, but could belong to this card."]


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
                results[number] = to_order_view(
                    _retry_after_login(session, lambda: amazon_orders.get_order(number)))
            except LoginFailed:
                raise
            except Exception as error:
                logger.warning("Could not load order %s: %s", number, error)
                results[number] = str(error) or type(error).__name__
        result = results[number]
        if isinstance(result, str):
            charge.lookup_error = result
        else:
            charge.order = result


def _by_placed_date(orders) -> list[OrderView]:
    return sorted(orders, key=lambda o: (o.placed_date is None, o.placed_date or date.min))


def fetch_uncharged_orders(session, charges: list[Charge], start: date, end: date,
                           charged_elsewhere: Collection[str] = ()) -> tuple[list[OrderView], list[str]]:
    """Find orders placed in range with no charge, from both the regular and the digital history.

    charged_elsewhere holds order numbers charged outside the range, which aren't missing a charge.
    """
    amazon_orders = AmazonOrders(session)
    charged = {c.order_number for c in charges if c.order_number} | set(charged_elsewhere)
    found: dict[str, OrderView] = {}
    warnings: list[str] = []
    for year in years_in_range(start, end):
        for order_filter, label in ((None, "regular"), (DIGITAL_ORDERS_FILTER, "digital")):
            try:
                orders = _retry_after_login(
                    session, lambda: amazon_orders.get_orders(year=year, order_filter=order_filter))
                views = [to_order_view(o) for o in orders]
            except LoginFailed as error:
                # Stop here: more sign-in attempts risk flagging the account.
                logger.warning("Stopped checking orders: %s", error)
                warnings.append(f"Stopped checking orders from {label} {year} onward: {error}")
                return _by_placed_date(found.values()), warnings
            except Exception as error:
                logger.warning("Could not check %s orders for %s: %s", label, year, error)
                warnings.append(f"Could not check {label} orders for {year}, so some orders may be missing here: {error}")
                continue
            for view in views:
                in_range = view.placed_date is None or start <= view.placed_date <= end
                if in_range and view.order_number not in charged:
                    found[view.order_number] = view
    return _by_placed_date(found.values()), warnings


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    args = parse_args(argv)
    today = today or date.today()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        session = AmazonSession(config=build_config())
        problem = credentials_problem(session, os.environ)
        if problem:
            return _fail(problem)
        session.login()
    except Exception as error:
        return _fail(f"Could not log in to Amazon: {error}")

    try:
        charges, skipped_rows, seen_order_numbers = fetch_charges(session, args.start, args.end, today)
        selected = [c for c in charges if c.last_4 == args.card] if args.card else charges
        attach_orders(session, selected)
    except LoginFailed as error:
        return _fail(f"{error} No report was written.")
    except Exception as error:
        return _fail(f"Could not read Amazon's transactions, so no report was written: {error}")

    # Classify every charge, so an order split across cards keeps its numbering in a one-card report.
    classify(charges)
    if args.card:
        # Orders with no charge have no card, so they can't be part of a one-card report.
        uncharged: list[OrderView] = []
        warnings = no_card_number_warnings(charges)
    else:
        uncharged, warnings = fetch_uncharged_orders(session, charges, args.start, args.end, seen_order_numbers)
    if skipped_rows:
        rows = "1 row" if skipped_rows == 1 else f"{skipped_rows} rows"
        warnings.insert(0, f"{rows} of Amazon's transactions could not be read, so charges may be missing.")

    report = Report(args.start, args.end, datetime.now(), selected, uncharged, warnings, args.card)
    content = render_json(report) if args.json else render_markdown(report)
    out = args.out or default_out(args.start, args.end, args.json, args.card)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
    except OSError as error:
        return _fail(f"Could not write {out}: {error}")

    print(f"{status_counts(selected)}. Report written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
