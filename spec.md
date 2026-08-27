# spec.md — Sales Tracker App

## Problem
A solo seller needs to **establish what they sell**, **log who bought how
many**, and **tick people off as they receive their order** — including
partial hand-offs (Jim ordered 10, has received 5). Orders must not vanish
from a stray delete; they stay on the list until the operator removes them
deliberately in Settings. This
app is a local order list with fulfillment tracking. It is **not** a
payment processor, inventory system, tax filer, CRM, or double-entry ledger.

## Intended Users
- **Solo seller / small-business operator** running the app on their own
  machine (desktop GUI or terminal)
- **Not** a public multi-tenant SaaS, not an accountant's books, not a
  shared cloud service

## Required Behavior
- **Establish a product first**, through a short interactive series of
  inputs (name; how it is counted; price per unit, 0 allowed; optional SKU;
  optional notes; review/confirm). Logging an order is blocked until at
  least one product exists. Duplicate product names (case-insensitive) are
  rejected. More than one product is allowed; the operator then chooses
  which product an order is for.
- **Log an order:** purchaser name + quantity bought (must be > 0) + how it
  is paid (`cash`, `venmo`, or `other`; default `cash`). The order is
  appended to a persistent list. Received starts at 0. The payment method
  can be changed later.
- **Fulfillment:** on a selected order, the operator enters how many have
  been handed out so far (example: Jim needs 10, received-so-far box = 5).
  Received must be ≥ 0 and ≤ ordered. Marking the name received (received
  ≥ ordered) does **not** remove the row. Filter the list: all /
  outstanding / received. Search by purchaser or product name.
- **Orders never disappear from the main list.** There is no per-order
  delete control on the order list in the GUI or CLI. Removing a row is a
  Settings-level action only (see below).
- **Settings:** the only place records can be removed.
  - Delete one order
  - Delete one product — refused while any order still references it, and
    the refusal names how many orders are attached
  - Reset all orders (keeps products)
  - Reset everything (products and orders)
  All four require an explicit confirmation the operator cannot hit by
  accident (GUI: type `RESET` to unlock; CLI: `--yes`, plus `--orders` or
  `--all` for the resets). Deleting is permanent; there is no undo.
- **Money page:** shows expected money split four ways — cash collected,
  cash still to collect, other collected, other still to collect — plus the
  totals. "Collected" means received quantity x unit price; the full order
  value is ordered quantity x unit price.
- **Cash sanity check:** the operator counts their own bills ($1, $2, $5,
  $10, $20, $50, $100) and enters the number of each. The app totals the
  count and compares it against *cash collected only*, reporting balanced,
  over, or short. Venmo and other payments are excluded because they never
  reach the drawer. Counts are a live calculation and are not stored; the
  ledger figure and the drawer count must be arrived at independently or
  the comparison proves nothing.
- **Export:** one CSV containing every order as a row plus a totals block.
- **Empty states** when there are no products or no orders. Invalid input
  fails closed: no partial row is written.
- **Money:** optional. Stored as `decimal.Decimal` quantized to 0.01,
  persisted as TEXT. Display as USD (`$1,240.00`) until this spec says
  otherwise. Quantity allows fractions (quantized to 0.001) so units such
  as lb still work.
- Line total is computed (`quantity_ordered × unit_price`), not stored.
- An order is **outstanding** when received < ordered, **received** when
  received ≥ ordered.

## User Experience
- Two entry points share one `SalesTracker` and one SQLite file:
  - Desktop: `python3 gui.py`
  - Script: `python3 sales_tracker.py` (interactive menu) or subcommands
    (`product`, `order`, `receive`, `list`, `summary`, `money`, `export`,
    `pay`, `delete`, `reset`, …)
- GUI: product wizard on first run if the catalog is empty; order ticket
  (purchaser + quantity); list with received/ordered; a separate
  received-so-far box for the selected row; Settings in the Ledger menu
  and header. Settings holds the order and product delete pickers behind a
  typed `RESET` unlock.
- CLI interactive session asks one question at a time for product setup,
  logging, and received-so-far updates.
- Example: establish Honey (jar, $12.50) → log Jim bought 10 → enter 5 in
  received so far → row still shows Jim 5 / 10 outstanding → later 10 / 10
  received, still on the list → Settings is the only place it can be removed.
- No web server, no `/health`, no browser UI.

## Architecture
- Language/runtime: **Python 3.14**. Agents use 3.14 locally. Do not
  downgrade.
- Stack: **Python standard library only** at runtime (sqlite3, tkinter,
  argparse, unittest). No FastAPI, no npm, no frontend build. New runtime
  dependencies need justification, a pin, and a spec update.
- Storage: SQLite file `sales.db` next to the scripts, or next to the
  frozen binary (gitignored). Path override: `--db`.
- Packaged GUI: PyInstaller onefile via `SalesTracker.spec`. PyInstaller
  is **build-only**, pinned in `requirements-build.txt` as
  `pyinstaller==6.22.2`. Do not drop below 6.22: 6.21.0 collects no Tcl/Tk
  data against Python 3.14 and produces a Windows exe that cannot start.
  Shipped binaries:
  - `release/SalesTracker.exe` — Windows PE32+
  - `release/SalesTracker-linux-x86_64` — Linux ELF x86-64
  GitHub Actions (`.github/workflows/ci.yml`) runs the test suite on every
  push and pull request and builds the Windows exe only after tests pass;
  `dist/` stays gitignored.
- Major components:
  - `sales_tracker.py` — `SalesTracker`, product/order schema, interactive
    CLI and flag CLI
  - `gui.py` — Tkinter ledger (product wizard, list, received box, Settings)
  - `test_sales_tracker.py` — unittest (library, CLI, interactive session,
    GUI smoke)
  - `SalesTracker.spec` / `requirements-build.txt` — frozen GUI packaging
- Data model:

  ```
  Product 1---* Order
  ```

  - **Product:** id, name, unit, unit_price, sku, notes, created_at
  - **Order:** id, product_id, purchaser, quantity_ordered,
    quantity_received, created_at, updated_at, payment_method
- If a legacy `sales` table is present, migrate it into products + orders
  (received starts at 0) and drop `sales`.
- External APIs/services: **none**. No Stripe, Shopify, email, or LLM.

## Security & Privacy
- No secrets in source. No accounts, no network auth.
- Purchaser names and product data stay on the machine. Do not log extra
  copies of purchaser names.
- This app never stores card numbers, bank accounts, or payment tokens.
- Reset is destructive and local-only; require the confirmation described
  above.
- Dependencies: prefer stdlib. Dependabot is **not required** unless the
  human enables it.

## Validation & Tooling
- Tests: `python3 -m unittest test_sales_tracker.py` — must pass. GUI tests
  are skipped when no display is available, so the suite is green headless.
- Packaged builds: `python3 tools/smoke_test.py` — must pass. A frozen build
  can start and still show no window while PyInstaller exits 0, so this
  launches the binary detached with no console and requires a real visible
  window. CI runs it after both the Windows and Linux build steps.
- Lint / types: **not configured**. Treat `ruff` / `pyright` as report-only
  until `pyproject.toml` / `pyrightconfig.json` exist. Do not install that
  tooling on your own initiative.
- Do not add a runtime `requirements.txt` until a third-party runtime
  package is actually approved. `requirements-build.txt` is packaging-only.

## Project workflow (spec overrides / alignment with agents.md)
- Push / open PRs only when the human explicitly asks in the current
  conversation.
- Prefer task branches when committing; default branch should stay green.
- Update `context.md` (state + handoff) and `CHANGELOG.md` (user-visible
  history) per `agents.md`.
- Do not put policy rules in `context.md`, `roadmap.md`, or `CHANGELOG.md`.

## Acceptance Criteria
- [x] Operator cannot log an order until a product has been established
- [x] Product setup is a series of interactive inputs (name, unit, price,
      optional SKU, optional notes, confirm)
- [x] Operator can log purchaser name + quantity; the row appears on the list
- [x] Operator can set received-so-far (e.g. 5 of 10); the row stays
- [x] A fully received name stays on the list until Settings reset
- [x] Received > ordered or negative received is rejected; no partial write
- [x] There is no per-order delete control on the main order list
- [x] Settings can delete one order, and can delete one product only once no
      orders reference it
- [x] Every Settings removal is gated behind an explicit typed confirmation
- [x] Settings is the only place any record can be removed
- [x] CLI and GUI share one SQLite ledger
- [x] Every order records how it is paid; existing ledgers default to cash
- [x] Expected money is split into cash and non-cash, collected and not
- [x] A bill count is compared against cash collected only, and reports
      balanced / over / short
- [x] Bill counts are never persisted
- [x] Export writes one CSV of order rows plus a totals block
- [x] `python3 -m unittest test_sales_tracker.py` exits 0
- [ ] CHANGELOG.md updated for later user-visible releases

## Out of scope (until a later approved phase)
- Web UI, FastAPI, `/health`, `run_dev.sh`
- Separate customer records (contact, notes) beyond purchaser name on an order
- Refund / cancel status, inventory, tax, invoicing
- Multi-user login, multi-currency, payment processors
- Integer-cents storage (Decimal TEXT is the contract)
