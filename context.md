# context.md — Sales Tracker App

## Current State
- Folder: `/home/jonathans/Sales Tracker App`.
- App is a **SQLite ledger** with two entry points: interactive CLI
  (`sales_tracker.py`) and Tkinter UI (`gui.py`). Shared class: `SalesTracker`.
- Tests: `python3 -m unittest test_sales_tracker.py` — 25 passed.
  Lint / types: **not configured**.
- Frozen GUI: `release/SalesTracker.exe` (Windows PE32+) and
  `release/SalesTracker-linux-x86_64` (Linux ELF). `sales.db` lives next
  to the binary.
- Git: `j0nsh1n/sales-tracker` (private). PR #1 is `feat/windows-exe`.

## Repo Landmarks
| Path | Role |
|------|------|
| `sales_tracker.py` | Products, orders, fulfillment, interactive CLI, reset |
| `gui.py` | Product wizard, order list, received box, Settings |
| `test_sales_tracker.py` | unittest for library, CLI, interactive session, GUI |
| `SalesTracker.spec` | PyInstaller spec for frozen GUI builds |
| `release/` | `SalesTracker.exe`, `SalesTracker-linux-x86_64` |
| `requirements-build.txt` | Build-only pin: pyinstaller==6.21.0 |
| `.github/workflows/build-windows-exe.yml` | Windows exe CI |
| `agents.md` | Global coding rules |
| `spec.md` | Product contract (desktop ledger + CLI) |
| `roadmap.md` | Phased plan |
| `CHANGELOG.md` | User-visible history |
| `README.md` | How to run |

## Domain Model
SQLite file `sales.db` next to the script or exe (gitignored).

```
Product 1---* Order
```

- **Product:** id, name, unit, unit_price (Decimal TEXT), sku, notes, created_at
- **Order:** id, product_id, purchaser, quantity_ordered, quantity_received
  (default 0), created_at, updated_at
- Line total is computed: ordered × unit_price
- Fulfilled when received >= ordered; the row stays
- No per-order delete. `reset_orders()` / `reset_all()` only, from Settings

## Non-Obvious Decisions
- Money is `decimal.Decimal` stored as TEXT, not integer cents.
- Quantity allows fractions (0.001) so lb / kg still work.
- Legacy `sales` table, if present, is migrated into products + orders
  (received starts at 0).
- GUI auto-opens the product wizard when the catalog is empty.
- Settings reset requires typing RESET so it cannot be a stray click.
- PyInstaller is build-only, not a runtime dependency.
- Linux frozen binary was built natively here; Windows exe was Wine + CI.

## Session Handoff
- **Date:** 2026-08-26
- **Branch:** feat/windows-exe
- **Done:** Linux onefile GUI at `release/SalesTracker-linux-x86_64`
  (smoke-tested: process stayed up, created `--db` file).
- **Next:** Human reviews
  https://github.com/j0nsh1n/sales-tracker/pull/1
