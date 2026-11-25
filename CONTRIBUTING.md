# Contributing to Open Home Inventory

Pull requests are welcome, whether you are writing code by hand or working with a coding agent. Pick an issue tagged `good first issue` or `help wanted` to start, or open a short issue first if you want to propose something bigger.

## Local setup and tests

Everything in the test suite runs offline with zero GPU, camera, or live email credentials.

```bash
pip install -r requirements.txt
pip install flake8
pytest tests/ -v
flake8 src tests --count --select=E9,F63,F7,F82 --show-source --statistics
python examples/generate_sample_data.py
```

## What makes a PR easy to merge

- Keep each PR focused on one issue and touch only the files needed for it.
- Add unit tests with synthetic fixture data for any new parser, exporter, or bug fix.
- Never commit real receipts, personal emails, serial numbers, private IPs, or `.db` files. `tests/test_hygiene.py` and `tests/test_pipeline_upgrades.py` will catch most of these automatically.
- If you are adding a merchant receipt parser, follow `BaseReceiptParser` in `src/receipts/parsers/base.py`, register it in `src/receipts/parser_registry.py`, and add a synthetic test case in `tests/test_receipt_parsers.py`.
- In your PR description, list what changed and paste the test command output you ran.

## Using AI coding agents

AI-assisted PRs are totally fine here as long as the diff stays small, stays within the issue scope, and passes the full test suite cleanly. Avoid drive-by rewrites of unrelated modules.
