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
python3 sales_tracker.py delete order 1 --yes
python3 sales_tracker.py delete product 1 --yes
python3 sales_tracker.py reset --orders --yes
```

Every order records how it was paid — `cash`, `venmo`, or `other`. Only cash
counts toward the drawer.

**Money** (button in the header, or `money` on the CLI) shows what you should
have: cash collected, cash still to collect, and the same for non-cash. You
count your own bills and type in how many of each you hold; the app totals
them and tells you whether you are balanced, over, or short against *cash
collected only*. Bill counts are never saved — the point is that the two
figures are reached independently.

**Export CSV** writes every order as a row, followed by a totals block.

Rows are never removed from the main list. Deleting one order or one product
happens in Settings (GUI) or the Settings menu / `delete` command (CLI). A
product that still has orders on the list cannot be deleted until those
orders are.

Packaged GUI (no Python install needed): download the Windows `.exe` or
Linux ELF from the
[Releases](https://github.com/j0nsh1n/sales-tracker/releases) page.
`sales.db` is created next to the binary. A tag matching `v*` (for example
`v0.1.0`) builds both targets and attaches them to that release.

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
