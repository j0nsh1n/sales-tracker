# context.md — Sales Tracker App

## Current State
- Folder: `/home/jonathans/Sales Tracker App`.
- App is a **SQLite ledger** with two entry points: interactive CLI
  (`sales_tracker.py`) and Tkinter UI (`gui.py`). Implementation lives in
  the `salestracker` package; root files are shims.
- Tests: `python3 -m unittest test_sales_tracker.py` — 50 passed.
  Lint / types: **not configured**.
- Frozen GUI: built by CI; `v*` tags attach Windows exe and Linux ELF to
  a GitHub Release. Binaries are not tracked in git.
- Git: `j0nsh1n/sales-tracker` (private). Working branch
  `refactor/package-restructure`. Nothing pushed this session.

## Repo Landmarks
| Path | Role |
|------|------|
| `salestracker/` | models, store, cli, `ui/gui.py` |
| `sales_tracker.py` | Thin CLI shim |
| `gui.py` | Thin GUI shim |
| `test_sales_tracker.py` | unittest for library, CLI, interactive session, GUI |
| `SalesTracker.spec` | PyInstaller spec for frozen GUI builds |
| `release/` | gitignored (local binaries and `sales.db`; not tracked) |
| `requirements-build.txt` | Build-only pin: pyinstaller==6.21.0 |
| `.github/workflows/ci.yml` | Tests, then Windows + Linux package; Releases on `v*` |
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
- Deleting happens only in Settings: `delete_order()`, `delete_product()`
  (refused while orders reference the product), `reset_orders()`,
  `reset_all()`. The main list still has no delete control.

## Non-Obvious Decisions
- Money is `decimal.Decimal` stored as TEXT, not integer cents.
- Quantity allows fractions (0.001) so lb / kg still work.
- Schema version is `PRAGMA user_version` (current 2; v2 added
  `orders.payment_method`, defaulting existing rows to cash). Legacy `sales`
  import is migration 0 → 1 (received starts at 0). Newer-than-code
  databases raise TrackerError.
- GUI auto-opens the product wizard when the catalog is empty.
- Settings reset requires typing RESET so it cannot be a stray click.
- PyInstaller is build-only, not a runtime dependency.
- Linux frozen binary was built natively here; Windows exe was Wine + CI.
- `_migrate_legacy_sales` inserts products inline rather than via
  `add_product`, so the whole migration commits once and a interrupted run
  can be retried instead of bricking the file.
- `SalesTracker.__init__` closes its connection if schema init raises;
  otherwise a failed open holds the write lock for the rest of the process.
- Search escapes `%` and `_` and uses `LIKE ... ESCAPE`, so those are
  matched literally.
- `find_product` resolves by name before id, keeping all-digit product
  names (for example `2024`) reachable.
- GUI tests are skipped when no display is available, so CI stays green on
  headless runners.

## Session Handoff
- **Date:** 2026-08-26
- **Branch:** refactor/package-restructure
- **Done:** Claude's uncommitted Money / payment / CSV work committed
  locally. Linux onefile rebuilt at `release/SalesTracker-linux-x86_64`.
  Nothing pushed.
- **Verified:** `python3 -m unittest test_sales_tracker.py` — 50 passed.
- **Open:** `docs/GROK-TASKS.md` is a spent handoff. Coins not handled.
  Extra payment methods (zelle/card) need a spec line. History still holds
  30 MB of old binaries.
- **Next:** Human reviews Money page and export; say if this should be
  pushed or opened as a PR.
