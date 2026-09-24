# Sales Tracker App

Establish a product, log who bought how many, and check people off as they
receive their order. Rows stay on the list until you reset them in Settings.

Two entry points share one SQLite file (`sales.db` next to the scripts):

```bash
# Desktop
python3 gui.py

# Interactive script (asks one question at a time)
python3 sales_tracker.py

# Same thing with flags
python3 sales_tracker.py product --name Honey --unit jar --price 12.50
python3 sales_tracker.py order --buyer Jim --qty 10
python3 sales_tracker.py receive 1 --got 5
python3 sales_tracker.py list
python3 sales_tracker.py order --buyer Ann --qty 4 --method venmo
python3 sales_tracker.py money
python3 sales_tracker.py money --count --n20 3 --n10 1 --n5 1
python3 sales_tracker.py export --out sales.csv
python3 sales_tracker.py pay 2 cash
python3 sales_tracker.py edit order 1 --buyer Jimmy --qty 12
python3 sales_tracker.py edit product 1 --price 14 --yes
python3 sales_tracker.py delete order 1 --yes
python3 sales_tracker.py delete product 1 --yes
python3 sales_tracker.py reset --orders --yes
```

Every order records how it was paid — `cash`, `venmo`, or `other`. Only cash
counts toward the drawer.

The desktop window has a sidebar with five pages. **Counter** is where you
work at the stall: everyone still waiting is a card, oldest first. Press
**Log a sale** to record who bought what; press **Hand over** on a card and
use − / + / All (or type the figure and press Enter) as they collect. Orders
that are fully handed over fold away under Collected. **Details** is the
full grid with every column: click a heading to sort, filter by status or
product, and log a sale from one line (`Jim Carter, 10, Honey, venmo`, then
Enter; product and paid-by are optional). On the grid, Enter or a
double-click types the received figure in place, + / − hand over one, `a`
marks all received, `e` edits, `n` jumps to the log line and `/` to search.
**Buyers** groups every order by the person who placed it, with what they
still owe. **Products** shows a card per product with what it has sold.

**Money** (sidebar, or `money` on the CLI) shows what you should
have: cash collected, cash still to collect, and the same for non-cash. You
count your own bills and type in how many of each you hold; the app totals
them and tells you whether you are balanced, over, or short against *cash
collected only*. Bill counts are never saved — the point is that the two
figures are reached independently.

**Appearance** (Settings, or the toggle at the bottom of the sidebar)
switches between Light and Dark. The default,
System, follows your desktop and changes with it while the app is open —
switch Windows to dark and the ledger follows without a restart. The choice
is remembered in `sales.db` and is not cleared by a reset.

**Export CSV** writes every order as a row, followed by a totals block.
Payment methods read as `Cash` and `Venmo` on screen but stay lowercase in
the CSV and on the CLI, which is what those commands take as input.

**Fix a mistake** with Edit on a Counter card or under the Details grid
(Ctrl+E), Edit on a product's card, the Ledger menu, menu item 8 in the
interactive script, or
`edit` on the CLI. Only what you change is changed. An order's quantity
cannot go below what has already been handed out. Price belongs to the
product, so changing it reprices every order for that product, including
money you have already collected — the app asks before doing that, and the
CLI needs `--yes`.

Rows are never removed from the main list. Deleting one order or one product
happens in Settings (GUI) or the Settings menu / `delete` command (CLI). A
product that still has orders on the list cannot be deleted until those
orders are.

Packaged GUI (no Python install needed): download the Windows `.exe` or
Linux ELF from the
[Releases](https://github.com/j0nsh1n/sales-tracker/releases) page.
`sales.db` is created next to the binary. A tag matching `v*` (for example
`v0.1.0`) builds both targets and attaches them to that release.

**Updating** a packaged build: the app looks for a newer release once a day
and says so in the status bar. Settings → Updates has **Check now** and
**Install and restart**; the version you had is kept and **Restore previous
version** brings it back. From the command line: `python3 sales_tracker.py
update` to check, `update --install --yes` to install. Releases come from
GitHub by default (`github:j0nsh1n/sales-tracker`). A private repository needs
a token (paste it in Settings, or set `SALES_TRACKER_UPDATE_TOKEN`); the
token stays in your `sales.db`. If GitHub is out of reach, point the source
at any web address or a folder that holds `update.json` and the binaries;
`python3 -m salestracker.update write-manifest --version v0.2.0 dist/…`
writes that manifest. Every download is checked against the manifest's size
and SHA-256 before it is installed.

Rebuild:

```bash
python3 -m pip install -r requirements-build.txt
python3 -m PyInstaller --noconfirm SalesTracker.spec
# Linux output: dist/SalesTracker
# Windows output: dist/SalesTracker.exe
```

A packaged build can start and still be unusable — no window, or an error
dialog — while the unit suite stays green and PyInstaller exits 0. After
building, launch it and check that a window really opens:

```bash
python3 tools/smoke_test.py
```

It starts the binary detached with no console, the way a double-click does,
and waits for a visible window titled `Sales Tracker`. Exit 0 means the app
came up, 1 means it did not, 2 means the check itself could not run. CI runs
this on both targets. On Linux it needs `xdotool` to see windows, and a
display (`xvfb-run -a` works headless).

To check the Windows `.exe` from a Linux machine:

```bash
python3 tools/smoke_test.py --binary dist/SalesTracker.exe --wine
```

This is a sales log with fulfillment tracking — not a CRM, not accounting
software, and not a payment processor.

## Docs

- `spec.md` — product contract (desktop ledger + CLI)
- `context.md` — current state
- `roadmap.md` — phased plan
- `agents.md` — agent coding rules
