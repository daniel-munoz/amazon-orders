# amazon-orders

Personal CLI for exporting Amazon.com order history for a date range, built on the
unofficial [`amazon-orders`](https://github.com/alexdlaird/amazon-orders) library.

**Status:** project setup only. The export script doesn't exist yet.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` sets public PyPI as the package index, so installs for this
project don't depend on any machine-wide pip configuration.

## Data and credentials

Exported CSV and JSON files contain personal purchase history and are ignored by
git, as are `.env` files. Credentials will come from environment variables and
must never be committed.
