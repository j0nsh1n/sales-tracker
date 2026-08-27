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
python3 sales_tracker.py delete order 1 --yes
python3 sales_tracker.py delete product 1 --yes
python3 sales_tracker.py reset --orders --yes
```

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

This is a sales log with fulfillment tracking — not a CRM, not accounting
software, and not a payment processor.

## Docs

- `spec.md` — product contract (desktop ledger + CLI)
- `context.md` — current state
- `roadmap.md` — phased plan
- `agents.md` — agent coding rules
