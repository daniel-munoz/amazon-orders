# amazon-orders

Personal CLI for exporting Amazon.com order history for a date range, built on the
unofficial [`amazon-orders`](https://github.com/alexdlaird/amazon-orders) library.

For each Amazon charge or refund in a date range, it shows the order and items behind it, so the
charge can be identified on a card statement.

**Status:** not yet tried against a real Amazon account.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` sets public PyPI as the package index, so installs for this
project don't depend on any machine-wide pip configuration.

## Usage

```sh
export AMAZON_USERNAME=...
export AMAZON_PASSWORD=...
export AMAZON_OTP_SECRET_KEY=...   # optional, for app-based two-step verification

.venv/bin/python amazon_orders_export.py --start 2025-01-01 --end 2025-06-30 [--card 1234] [--out PATH] [--json]
```

The report goes to `reports/` by default. Dates are compared with the charge date.

`--card` limits the report to purchases and refunds on the card with those last four digits, so it
can be checked against that card's statement. Charges Amazon lists without a card number are left
out and counted in a warning at the top. The "Orders with no charge found" section is omitted.

The first login may ask for a CAPTCHA or a one-time code at the terminal, so the account holder may
need to be there with their phone. Later runs reuse the saved session in
`~/.config/amazonorders/cookies.json`. If this machine also uses another Amazon login, set
`AMAZON_COOKIE_JAR_PATH` to a separate file so the wrong account's session is never reused.

If login fails with "JavaScript-based sign-in challenge", or the saved session has expired, sign in
on Amazon's own page instead. It opens a browser window for the account holder, and no password is
needed in the environment:

```sh
.venv/bin/amazon-orders login --browser
```

Then run the report as usual. It reuses the session that command saved.

Item images only appear in a viewer that renders Markdown and loads remote images. A plain text
editor shows the raw text.

## Data and credentials

Reports contain personal purchase history. `reports/`, CSV files, `orders*.json` and `.env` files are
ignored by git, but a custom `--out` path is your responsibility. Credentials come only from
environment variables and must never be committed.

## Tests

```sh
.venv/bin/python -m pytest
```
