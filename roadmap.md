# roadmap.md — Sales Tracker App

Note: "Complete when" conditions are verified locally (tests pass, feature
works) and via PR review. One phase may span several small PRs.

## Phase 1 — Governance files
- Tasks:
  - Track `agents.md`, `spec.md`, `roadmap.md`, `context.md`, `CHANGELOG.md`
  - Rewrite product files for a sales log (not LitSieve)
  - Add a short README and `.gitignore`
- Complete when: the five governance files exist in this folder and describe
  Sales Tracker, not literature search
- Status: [x] 2026-08-26

## Phase 2 — Desktop ledger
- Tasks:
  - Python 3.14, stdlib only (`sqlite3`, `tkinter`, `argparse`, `unittest`)
  - Product wizard (interactive inputs) before any order can be logged
  - Log purchaser + quantity onto a persistent list
  - Received-so-far box (e.g. Jim 5 of 10); fully received rows stay
  - Settings reset (confirmed) as the only way orders disappear
  - CLI + GUI share `SalesTracker` / `sales.db`
  - `python3 -m unittest test_sales_tracker.py` exits 0
- Complete when: the acceptance criteria in spec.md that are marked [x]
  hold, including no per-order delete
- Status: [x] 2026-08-26

## Phase 3 — Later app work
- Tasks: (unscheduled — human said app work resumes later)
- Complete when: a later approved slice lands
- Status: [ ]

## Backlog (unscheduled)
- Realign leftover FastAPI-era ideas only if the human asks for a web UI
- Multi-user accounts / login
- CRM pipeline (lead → quote → won / lost)
- Inventory / stock
- Tax / invoicing
- Payment processors (Stripe, etc.)
- Multi-currency
- CSV export / import
- Separate customer records beyond purchaser name
