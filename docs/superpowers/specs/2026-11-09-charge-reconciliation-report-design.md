# Amazon charge reconciliation report: design

**Status:** draft for review. Library facts come from reading `amazon-orders` 5.3.0 and
`amazon-auth` 0.2.3 source. Nothing has been run against a real Amazon account yet.

Items marked **(builder's call)** were chosen while writing this and were not discussed with the
project owner. Check those first.

## Why this exists

The owner's wife reconciles her accounts and runs into Amazon charges she can't identify. She buys
many Kindle and Audible books, which often cost the same or nearly the same, so the amount alone
doesn't say which purchase a charge was. Amazon orders also sometimes split across more than one
charge.

The tool answers one question for each Amazon charge in a date range: **what did this pay for?** It
shows enough detail to recognise the purchase: a title, and an image where one exists.

The project began as a line-item CSV export. This spec replaces that framing: the unit of the report
is the charge, with its items attached.

## Goals

- For a date range on her Amazon.com login, list every charge and refund, with amounts and card
  digits that can be compared directly with her statement.
- Tie each charge to its order and items, for both regular and digital (Kindle) purchases.
- Show item title and image, so similar-priced books can be told apart at a glance.
- Make split orders obvious, and connect the charges that belong to the same order.
- Say plainly when a charge can't be explained.

## Non-goals (v1)

- Importing or matching against her bank or card statement. She does that comparison by eye.
- Guessing a match between an unexplained charge and an order by amount or date.
- Audible coverage beyond what Amazon's own transactions and orders pages expose.
- Sites other than amazon.com, currencies other than USD.
- CSV output **(builder's call:** the owner moved away from CSV because of images; `--json` stays for
  machine use).
- Hosting, a GUI, multi-user support, scheduled runs.

## Anti-goals: failure even if it technically works

- A **confident wrong match**. A charge not firmly tied to items stays visibly unresolved.
- A **silently incomplete report**. Anything known to be missing is stated at the top of the report.
  The owner's original instruction was to log a warning and continue rather than crash, so a report is
  withheld only when no list of charges could be obtained at all.
- Credentials written to disk, or purchase history committed to git.
- Locking or flagging her Amazon account through repeated failed logins.

## Usage

```
python amazon_orders_export.py --start 2025-01-01 --end 2025-06-30 [--out PATH] [--json]
```

- `--start` and `--end` are inclusive `YYYY-MM-DD` dates, compared with the charge date. Reject
  malformed dates and `end < start`.
- Default output is Markdown at `reports/amazon-charges_<start>_<end>.md` **(builder's call)**.
- `--json` writes the same structure as JSON, with default extension `.json`.
- A one-line summary goes to stderr at the end, with counts per status and the output path.

## Constraints

- One script (`amazon_orders_export.py`), `requirements.txt`, and a short README. A single test file
  is also fine **(builder's call)**.
- Keep `amazon-orders==5.3.0` pinned. Selectors and API change between releases.
- Credentials come only from `AMAZON_USERNAME`, `AMAZON_PASSWORD`, and optionally
  `AMAZON_OTP_SECRET_KEY`. The library reads all three directly from the environment. The script must
  never call `config.save()` or log these values.
- Reuse the library's session cookies (`~/.config/amazonorders/cookies.json`, a private file), so she
  doesn't log in every run. If the machine also uses another Amazon login, set
  `AMAZON_COOKIE_JAR_PATH` so the wrong account's session is never reused. The README must say so.
- Set `max_auth_attempts` low (3) instead of the library default of 10 **(builder's call)**.
- `.gitignore` does not currently cover Markdown. Add `reports/` before the first real run. A custom
  `--out` path is the user's responsibility, and the README must say so.
- This is an unofficial scraper running against her real account. Keep request volume modest.

## How it works

Use the library names as written here. `get_order_history` from the original prompt does not exist in
5.3.0; the method is `get_orders`.

1. **Log in.** `AmazonSession(config=...).login()`. The session is reused if still valid. CAPTCHA and
   SMS or app codes are prompted at the terminal when they can't be solved automatically, so the
   first login may need her present with her phone.
2. **Fetch charges.** `AmazonTransactions(session).get_transactions(days=N)`, with
   `N = (today - start).days + 1`, because the window counts back from today. Then keep only
   `start <= completed_date <= end`. Each `Transaction` has `completed_date`, `grand_total` (negative
   for a charge, positive for a refund), `is_refund`, `payment_method`, `payment_method_last_4`,
   `order_number`, `order_details_link`, and `seller`.
3. **Fetch each order once.** For every distinct `order_number`, call
   `AmazonOrders(session).get_order(order_number)`. This returns full details whenever the order was
   placed, including orders placed before the date range and charged inside it. Use `order.items`
   (`title`, `quantity`, `price`, `asin`, `link`, `image_link`), `order.shipments` (`items`,
   `delivery_status`), `order.grand_total`, and `order.order_placed_date`.
4. **Coverage check.** For each calendar year the range touches, call
   `get_orders(year=Y)` and `get_orders(year=Y, order_filter="digital-orders")`. The default history
   excludes digital purchases, so both calls are required. Keep orders whose `order_placed_date` is in
   range and whose number isn't on any charge. List them as "Orders with no charge found", which is
   also where a gift-card-paid order or a charge the feed didn't link will surface. `full_details` is
   not needed here. Title and image from the history page are enough.
5. **Classify and render** (below).

Set `warn_on_missing_required_field=True` in the config, so a missing field becomes a warning and not
an exception.

## Charge statuses

Each charge gets exactly one status. Compare money rounded to cents.

| Status | Condition |
|---|---|
| **Refund** | `is_refund` is true. Checked first. Show the linked order's items when available. |
| **Unresolved** | No order number, order lookup failed, the order has no items, or the order total is missing. Show the reason. If items loaded, show them labelled "amount not verified". |
| **Split** | Order details loaded with items and a total, and either another non-refund charge in the report shares its order number or the amount differs from the order total. |
| **Matched** | Order details loaded with items and a total, it is the only non-refund charge for that order, and the amount equals the order total. |

Refunds are not counted as charges **(builder's call)**. They don't make a purchase Split, and they
are left out of "Charge 1 of 2" numbering and the "Charges in this report total" figure. A purchase
and a refund on the same order each note the other's date and amount.

A missing order total is Unresolved **(builder's call)**, because the amount can't be checked and a
charged-then-cancelled order deserves her attention.

**Known limit on split orders:** Amazon's data links a charge to an order, not to individual items. A
split charge therefore shows all of that order's items, grouped by shipment, and states that the items
can't be attributed to this specific charge. Do not infer an attribution from shipments or prices.

**Known limit on bundled charges:** for one of the two page formats Amazon serves, the library keeps
only the first order linked to a charge. If several purchases were billed in one charge, the others
would show up under "Orders with no charge found", and the charge amount won't equal the order total,
so it is reported as Split rather than Matched.

## Report layout (builder's call)

Top: date range, generation time, counts per status, and any completeness warnings. Then Unresolved
charges, since those need her attention. Then all others, oldest first, followed by "Orders with no
charge found". Use headings and lists, not tables, so images and long titles render cleanly.

```markdown
## 2025-03-14 · $12.99 · Visa ****1234 · Split

Order 111-1234567-1234567, placed 2025-03-12, order total $27.98
Charge 1 of 2 for this order. Charges in this report total $27.98.
These items can't be attributed to this specific charge.
[View order](https://www.amazon.com/...)

**Shipment: Delivered March 15**

- ![Item title](https://m.media-amazon.com/...)
  **Item title**, qty 1, $12.99, [product page](https://www.amazon.com/dp/...)
```

- Show amounts positive, as on a statement, with a "Refund" label where it applies.
- Link images to Amazon's URLs; don't download them. If `image_link` is empty, show the title alone.
- Escape Markdown characters in titles and other scraped text.
- A cancelled order or missing field shows "not available" and doesn't prevent the rest from rendering.

## Failure handling

| Failure | Behaviour |
|---|---|
| Login, CAPTCHA, or 2FA failure (`AmazonOrdersAuthError`, `AmazonOrdersWafSolverError`) | Clear message, non-zero exit, no report. |
| `get_transactions` raises, including mid-paging | Non-zero exit, no report, because it returns no list of charges. Catch broad exceptions here: with warnings enabled, the library's `is_refund` comparison raises `TypeError` on a missing total. |
| Library skips an unreadable transaction row and only logs a warning | Capture warnings from the `amazonorders.transactions` logger and count them. Write the report with a warning at the top: N rows couldn't be read, so charges may be missing. A skipped row may be undated, so always count it, even if it might fall outside the range. |
| One `get_order` fails (`AmazonOrdersNotFoundError`, `AmazonOrdersUnavailableError`, parsing errors) | Log a warning, mark that charge Unresolved with the reason, continue. |
| Coverage check fails | Log a warning, write the report, and state at the top which part couldn't be checked. |
| Individual field missing | Render "not available" and continue. |

Warnings go to stderr. They must not include credentials.

## Rollout

1. Run for the last 30 days and show the output to the owner before attempting the full range.
2. Use that run to answer the questions below, then update this spec.

### Must be confirmed on her real account

- Do Kindle purchases appear in the transactions feed with a `D01-…` order number?
- Does `get_order` return items, `image_link`, and `grand_total` for digital orders? If the total is
  often missing, many Kindle charges would be Unresolved and the status rule needs revisiting.
- Do Audible purchases appear anywhere, and how are they labelled?
- Do bundled digital charges occur, and how do they look in the report?
- If she's in an Amazon Household, do other members' orders show up in the coverage check?
  (`family_member_id="self"` restricts it.)

## Open question

**Who runs it and how the report gets read isn't settled.** v1 assumes someone comfortable with a
terminal runs it. A Markdown file only shows images in a viewer that renders Markdown and loads remote
images. A plain text editor shows raw text. Settle this after the 30-day run.

## Left to the builder

Internal structure of the script, retry policy for transient `get_order` failures, progress output,
and JSON field names.
