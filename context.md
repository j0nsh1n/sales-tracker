# context.md — Sales Tracker App

## Current State
- Folder: `/home/jonathans/Sales Tracker App`.
- App is a **SQLite ledger** with two entry points: interactive CLI
  (`sales_tracker.py`) and Tkinter UI (`gui.py`). Implementation lives in
  the `salestracker` package; root files are shims.
- Tests: `python3 -m unittest test_sales_tracker.py` — 52 tests, green on
  Windows and Linux. Packaged build: `python3 tools/smoke_test.py`.
  Lint / types: **not configured**.
- Frozen GUI: built by CI; `v*` tags attach Windows exe and Linux ELF to
  a GitHub Release. Binaries are not tracked in git.
- Git: `j0nsh1n/sales-tracker` (private). Working branch
  `fix/windows-frozen-launch`. Nothing pushed this session.

## Repo Landmarks
| Path | Role |
|------|------|
| `salestracker/` | models, store, cli, `ui/gui.py` |
| `sales_tracker.py` | Thin CLI shim |
| `gui.py` | Thin GUI shim |
| `test_sales_tracker.py` | unittest for library, CLI, interactive session, GUI |
| `SalesTracker.spec` | PyInstaller spec for frozen GUI builds |
| `release/` | gitignored (local binaries and `sales.db`; not tracked) |
| `tools/smoke_test.py` | Launches a frozen build, requires a real window |
| `requirements-build.txt` | Build-only pin: pyinstaller==6.22.2 |
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
- PyInstaller is build-only, not a runtime dependency. The pin is 6.22.2
  because 6.21.0 collects no Tcl/Tk data against Python 3.14 (Tcl/Tk 9
  keeps its library in a zip reached through zipfs, and the probe uses a
  real-filesystem check). It reports that as a warning and still exits 0,
  so the build looked fine while the exe was dead.
- `SalesTrackerTests` registers `tmp.cleanup` with `addCleanup` in setUp
  rather than calling it in tearDown, so it runs after any connection a test
  opened. Windows cannot unlink an open database file; Linux can, which is
  why this only ever failed locally.
- The smoke test passes no stdio redirection on Windows. Handing the child
  DEVNULL would give it a valid handle, so sys.stdout would not be None
  and the very bug it exists to catch would not reproduce.
- A onefile build re-execs itself, so the Tk window belongs to a child
  process. Window checks walk the process tree, not just the launched pid.
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
- **Branch:** fix/windows-frozen-launch
- **Done:** Packaged Windows exe launches again. Bumped PyInstaller to
  6.22.2 and stopped reading `sys.stdout` at import time in
  `salestracker/cli.py`; either fault alone kept the exe dead. Added
  `tools/smoke_test.py`, two guards in `FrozenLaunchGuardTests`, and a
  smoke step to both CI build jobs. Nothing pushed.
  Also fixed the Windows-only tearDown error in `SalesTrackerTests` and
  updated the spec.md pin (human approved).
- **Verified:** smoke test passes on the fixed exe and fails on a build
  with either fault reintroduced; the guards fail on the old cli.py.
  Suite is 52 tests, green on Windows. The Linux and Wine paths of the
  smoke test are written but unrun here (Windows machine).
- **Open:** Linux and Wine smoke paths still need a real run. Coins not
  handled. Extra payment methods (zelle/card) need a spec line.
  `docs/GROK-TASKS.md` is a spent handoff. History still holds 30 MB of
  old binaries.
- **Next:** Confirm the Linux ELF smoke step passes on the first CI run.
