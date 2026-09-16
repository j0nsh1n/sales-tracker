# Handoff prompt — infrastructure backlog for Sales Tracker App

Paste everything below the line into Grok. It is self-contained.

**Status 2026-08-28: all three tasks below are complete** (binaries are out
of git with CI attaching both targets to Releases; `PRAGMA user_version`
migrations are in at schema 3; the `salestracker` package restructure has
landed). Kept for reference — refresh this snapshot before handing off
anything new.

The design work (GUI layout, interaction, visual language) is deliberately
excluded — that is being handled separately. Do not touch `gui.py`,
`design_preview.py`, or `docs/design/`.

---

You are working on **Sales Tracker App**, a local-first desktop sales ledger.
Repo: `git@github.com:j0nsh1n/sales-tracker.git` (private). Working branch at
handoff: `main`. Language: **Python 3.14**, standard library
only at runtime. No web server, no frontend, no third-party runtime deps.

## Read these first, in this order

1. `agents.md` — global coding rules. **Binding.** Read it fully before you
   write anything. Key points you must obey:
   - Minimal diffs. No drive-by refactors, renames, or reformatting.
   - Never push to a remote or open a PR unless the human explicitly says so
     in the conversation. A remote is configured; this is not permission.
   - Never edit `spec.md` without explicit human approval. If your work shows
     the spec is wrong, flag it as "spec.md drift" and propose the edit.
   - Never edit `agents.md`.
   - `context.md` / `roadmap.md` / `CHANGELOG.md` hold state and history only.
     Never write rules or workflow instructions into them.
   - Branch per task: `type/short-description`.
   - Update `context.md` (Current State + Session Handoff) at the end of every
     task, and `CHANGELOG.md` for every user-visible change.
2. `spec.md` — the product contract. It was updated by the human this session.
3. `context.md` — current state, landmarks, domain model, decisions.

## Current state (verified, do not re-derive)

- Tests: `python3 -m unittest test_sales_tracker.py` — **84 pass**, 0 fail.
  GUI tests skip automatically when no display is available.
- Lint / type checking: **not configured**. Per `spec.md`, treat `ruff` and
  `pyright` as report-only. **Do not install them on your own initiative.**
- CI: `.github/workflows/ci.yml` runs the suite on ubuntu under `xvfb-run`,
  then builds both executables; a `v*` tag attaches them to a GitHub
  Release.
- Layout: the `salestracker` package holds the implementation (models,
  store, cli, `ui/gui.py`, `ui/theme.py`); `sales_tracker.py` and `gui.py`
  at the root are thin shims. Sizes: `store.py` 608, `cli.py` 611,
  `ui/gui.py` 1629, tests 1355.
- `PRAGMA user_version` is **3**: v1 products + orders, v2
  `orders.payment_method`, v3 the `settings` table. Newer-than-code files
  are refused.

A prior session already fixed these; do not redo them: non-finite/oversized
number validation, atomic legacy-`sales` migration, LIKE-wildcard escaping in
search, all-digit product-name lookup, and a connection leak that held the
write lock when `__init__` failed.

## Your tasks

### Task 1 — Get the shipped binaries out of git history (highest value)

**Problem, measured:** `.git` is 31 MB. Two blobs account for 30.2 MB of it:

| Blob | Size |
|------|------|
| `release/SalesTracker-linux-x86_64` | 17.4 MB |
| `release/SalesTracker.exe` | 12.8 MB |

All tracked source is under 200 KB. Every clone pays 31 MB forever, and every
rebuild adds another multi-MB blob that can never be garbage collected.

**Do this:**
1. `git rm --cached` both binaries; add `release/` to `.gitignore` (keep
   `release/sales.db` ignored as it already is — see the warning below).
2. Change `.github/workflows/ci.yml` so that pushing a tag matching `v*`
   builds both targets and attaches them to a **GitHub Release**:
   - Windows exe on `windows-latest` (job already exists).
   - Linux ELF on `ubuntu-latest` — this job does **not** exist yet and must
     be added. The current Linux binary was hand-built on the maintainer's
     machine and is unreproducible; that is the gap you are closing.
   - Use `softprops/action-gh-release` or `gh release upload`. Pin the action
     to a major version.
3. Update `README.md` to point at the Releases page instead of `release/`.
4. **`spec.md` drift — do not edit it yourself.** `spec.md` currently names
   `release/SalesTracker.exe` and `release/SalesTracker-linux-x86_64` as
   "Shipped binaries" under Architecture. Your change makes that false.
   Propose the replacement wording in your summary and let the human apply it.

**Do NOT rewrite git history** (`filter-repo`, `filter-branch`, force-push).
The 30 MB stays in history until the human explicitly asks for a rewrite;
there is an open PR (#1) against this branch. Removing the files going forward
is the whole scope. Say clearly in your summary that history is unchanged and
that a rewrite plus a coordinated force-push is the only way to reclaim the
30 MB, if they want it later.

**Warning:** `release/sales.db` is an untracked, gitignored SQLite file that
contains a real product row ("Milkshakes"). It is the maintainer's data. Do
not delete, move, or commit it.

### Task 2 — Real schema versioning

`_init_schema()` in `sales_tracker.py` relies on `CREATE TABLE IF NOT EXISTS`
plus ad-hoc sniffing of `sqlite_master` to detect a legacy `sales` table.
There is no version marker, so any future schema change has no safe path.

Introduce `PRAGMA user_version`-based migrations:
- Define the current schema as version 1 and stamp existing databases.
- A small ordered registry of migration steps, each run in one transaction.
- The legacy-`sales` import becomes migration step 0 → 1, preserving its
  current behaviour exactly (received starts at 0, `sales` is dropped, an
  interrupted run must still be safely retryable — there is a regression test
  named `test_interrupted_legacy_migration_can_retry`; it must keep passing).
- Refuse to open a database whose `user_version` is *newer* than the code
  understands, with a clear error rather than silent corruption.

Add tests for: fresh database, upgrade from a legacy database, re-open at
current version being a no-op, and the newer-than-code refusal.

### Task 3 — Package restructure

Both modules mix four concerns. `sales_tracker.py` holds the domain model,
SQLite persistence, CLI rendering helpers, and the interactive session loop.

Restructure to a package, preserving behaviour and public entry points:

```
salestracker/
  __init__.py
  models.py      # Product, Order, Summary, TrackerError, parse/format helpers
  store.py       # SalesTracker: connection, schema, migrations, queries
  cli.py         # argparse wiring, InteractiveSession, print helpers
  ui/            # gui.py moves here UNCHANGED - see constraint below
```

Constraints:
- `python3 sales_tracker.py ...` and `python3 gui.py` must keep working
  exactly as documented in `README.md`. Thin shims at the repo root are fine.
- `SalesTracker.spec` (PyInstaller) references `gui.py` and hiddenimport
  `sales_tracker`; update it and confirm the frozen build still starts.
- **Do not modify the contents of `gui.py` beyond import statements.** It is
  being redesigned in parallel and any other change will conflict.
- Keep `test_sales_tracker.py` green throughout. Update imports only.

Do this task **last**, and in its own commit, so the diff stays reviewable.

## Definition of done

- `python3 -m unittest test_sales_tracker.py` exits 0.
- New behaviour has tests. Every claim you make is backed by pasted command
  output — do not assert something passes without showing it.
- Work is committed on a task branch with `type: summary` messages.
- `context.md` (Current State + Session Handoff) and `CHANGELOG.md` updated.
- Nothing pushed to any remote. No PR opened.
- Your summary lists: what changed, why, how you verified it, any `spec.md`
  drift flags, and anything you deliberately left alone.
