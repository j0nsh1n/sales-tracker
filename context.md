# context.md — Sales Tracker App

## Current State
- Folder: `/home/jonathans/Sales Tracker App`.
- App is a **SQLite ledger** with two entry points: interactive CLI
  (`sales_tracker.py`) and Tkinter UI (`gui.py`). Implementation lives in
  the `salestracker` package; root files are shims.
- Tests: `python3 -m unittest test_sales_tracker.py` — 149 tests, green on
  Linux (cloud container, 3.14 via `uv python install 3.14`, GUI under
  `xvfb-run`). Packaged build: `python3 tools/smoke_test.py`.
  Lint / types: **not configured**.
- Frozen GUI: built by CI; `v*` tags attach Windows exe and Linux ELF to
  a GitHub Release. Binaries are not tracked in git.
- Git: `j0nsh1n/sales-tracker` (private). Branch is cut for v0.2.0.

## Repo Landmarks
| Path | Role |
|------|------|
| `salestracker/` | models, store, cli, `update.py` (self-update protocol), `_version.py`, `ui/gui.py` |
| `sales_tracker.py` | Thin CLI shim |
| `gui.py` | Thin GUI shim |
| `test_sales_tracker.py` | unittest for library, CLI, interactive session, GUI |
| `SalesTracker.spec` | PyInstaller spec for frozen GUI builds |
| `release/` | gitignored (local binaries and `sales.db`; not tracked) |
| `salestracker/ui/theme.py` | Light/dark palettes and OS theme detection |
| `tools/smoke_test.py` | Launches a frozen build, requires a real window |
| `docs/explorations/` | Four clickable HTML design directions (A–D) on one shared fixture; not product code |
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
- **Setting:** key, value — operator preferences (currently `theme`), not
  ledger data and not cleared by either reset
- Line total is computed: ordered × unit_price
- Fulfilled when received >= ordered; the row stays
- Editing: `edit_order()` / `edit_product()` change only the fields passed.
  Received is not editable there, and ordered cannot go below received.
- Deleting happens only in Settings: `delete_order()`, `delete_product()`
  (refused while orders reference the product), `reset_orders()`,
  `reset_all()`. The main list still has no delete control.

## Non-Obvious Decisions
- Money is `decimal.Decimal` stored as TEXT, not integer cents.
- Quantity allows fractions (0.001) so lb / kg still work.
- Schema version is `PRAGMA user_version` (current 3). v2 added
  `orders.payment_method`, defaulting existing rows to cash. v3 added a
  `settings` key/value table for operator preferences; `reset_all` leaves
  it alone because the spec's "reset everything" means products and
  orders. Legacy `sales` import is migration 0 → 1 (received starts at 0).
  Newer-than-code databases raise TrackerError.
- GUI auto-opens the product wizard when the catalog is empty. The
  "Establish a product" button belongs to the welcome card and disappears
  with it, so the sidebar carries its own New product button.
- GUI layout (2026-09-24 redesign): sidebar + five pages (`show_page`).
  Counter (design direction A) is the working page: cards rebuilt by
  `_render_counter` on every refresh, one card's stepper open at a time
  (`_open_card`). Details (direction B) holds the grid, the command line
  (`_parse_cmd` / `run_cmd`) and an in-place received editor placed over
  the cell. `selected_order_id` is the one source of "the order being
  worked on" for both pages; `_on_select` ignores selection changes made
  during `refresh`. Log a sale is a dialog whose fields are the app's own
  variables, so `log_order` works with or without it open. Money is a
  page (`MoneyPanel`), not a dialog. Plain Tk widgets register
  their palette names with `SalesApp.paint()` and `_repaint` reapplies
  them after the generic canvas pass, which would otherwise leave page
  canvases in the dialog colour. Entry hints are `Placeholder` overlays,
  so the variables never hold hint text. A Treeview cuts off columns it
  cannot fit instead of shrinking them, so `_fit_columns` shares the width
  on every resize. The Buyers grid keeps its own headings; the Details
  grid uses `HEADINGS`.
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
- gui.py's colour names are rebound by `apply_palette`, not constants.
  Read them at call time; a value captured in a default argument keeps
  the old theme after a switch. ttk styles repaint themselves when
  `_style` re-runs, but plain Tk widgets (canvases, the hairline rules)
  and the `done` row tag hold their own colour and are repainted by hand.
- A readonly ttk Combobox draws from its state map, not `configure`, so
  dark mode needs `style.map` and the dropdown listbox needs
  `option_add` — ttk cannot reach that listbox. The listbox is created by
  Tcl on first open, so it is invisible to Python's widget registry: any
  code that looks for it must go through `winfo`/Tcl calls, and
  `_repaint` destroys built popdowns so the next open picks up the new
  palette (Tk 8.6 and 9 both rebuild it on demand).
- Wheel handling is ours, not Tk's, on every scrollable surface:
  `bind_wheel_scroll` on each list and one handler on each dialog
  toplevel for its panel. Deltas divide by 40.0; Tk 9 takes the fraction,
  Tk 8.6 raises "expected integer" so the leftover is carried in Python.
  Tk 8.6's own bindings round sub-notch deltas to zero, which is why a
  touchpad moved nothing on Linux even in the order list. The list
  bindings return "break" so Tk's class binding cannot also fire.
- Price is read from the product at query time; orders store none. Editing
  a price therefore reprices every order for it, collected money included,
  which moves the Money page's cash-collected figure. Both UIs confirm first
  via `price_change_warning()`; the store itself does not refuse.
- Updates: `update.json` beside each release is the protocol; sources are
  `github:owner/repo`, a URL, or a folder (`resolve_source`). The GitHub
  default reads the `releases/latest/download/` redirect, not the API, so
  unauthenticated checks are not rate-limited; a private repo needs a
  token and goes through the API, where asset downloads redirect to a
  storage host that rejects the token (`_NoTokenAcrossHosts` strips it).
  Install is a rename of the running binary to `.old` plus a rename of
  the download, which Windows and Linux both allow; the Windows path is
  reasoned about, not run here. CI attaches `update.json` in a
  `release-manifest` job and refuses a `v*` tag that differs from
  `_version.py`. Releases can also be cut from the Actions tab: Run
  workflow with `release_tag` creates the tag at that commit (a cloud
  session's git credential cannot push tags, which is why this exists).
- Payment methods are capitalised for display only. The ledger, the CSV,
  and the CLI's accepted input all stay lowercase.
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
- **Date:** 2026-09-24
- **Branch:** `claude/loving-sagan-cstbui` (from `main` at v0.1.5)
- **Done:** (1) edit orders and products. (2) Sidebar redesign. (3) Four
  UI directions prototyped in `docs/explorations/`; the human chose A
  (Counter) as the main page with B (Register) as a Details page, now
  implemented. (4) Self-update protocol (`salestracker/update.py`):
  manifest, three source kinds, ETag cache, daily quiet check, verified
  download, rename-swap install with restore; Settings → Updates, CLI
  `update`, CI manifest job and tag/version check. spec.md gained an
  Updates bullet and exceptions to "no external services" and "no network
  auth", at the human's request this session.
- **Verified:** 149 tests green under xvfb on 3.14 (uv); Counter and
  Details checked by screenshot in light and dark at 1260x800 and at the
  minimum 1180x700, including the card and cell error states. Linux
  onefile rebuilt and `tools/smoke_test.py` passed.
- **Open:** the updater's Windows rename path and the real GitHub path
  are untested here (no Windows, and no release carries `update.json`
  yet; the first tagged release after this merge will). Downloads are
  read into memory, fine at ~17 MB. Price is not snapshotted per order.
  Buyers page kept although neither chosen direction had it. Windows exe
  not rebuilt locally; CI builds it. Coins, zelle/card, history binaries
  as before.
- **Next:** `_version.py` is 0.2.0 and CHANGELOG is cut. Merge the PR,
  then tag `v0.2.0` on main: CI checks the tag against the version and
  attaches the binaries and `update.json` to the release.
