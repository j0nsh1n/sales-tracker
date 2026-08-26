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
python3 sales_tracker.py reset --orders --yes
```

Windows: GitHub Actions builds `SalesTracker.exe` (one-file GUI). Download
the **SalesTracker-windows** artifact from the workflow run. Double-click
the exe; `sales.db` is created next to it. Rebuild locally on Windows with:

```bat
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm SalesTracker.spec
```

This is a sales log with fulfillment tracking — not a CRM, not accounting
software, and not a payment processor.

## Docs

- `spec.md` — product contract (desktop ledger + CLI)
- `context.md` — current state
- `roadmap.md` — phased plan
- `agents.md` — agent coding rules
