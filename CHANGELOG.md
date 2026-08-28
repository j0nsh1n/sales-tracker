# Changelog

All notable user-visible changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Project governance set: `agents.md`, `spec.md`, `roadmap.md`, `context.md`,
  `CHANGELOG.md`, short `README.md`, and `.gitignore`.
- Product setup wizard (GUI and interactive CLI) before orders can be logged.
- Order list with purchaser name, quantity ordered, and a received-so-far
  box (for example Jim 5 of 10). Fully received rows stay on the list.
- Settings reset (type RESET) as the only way to clear orders.
- Windows GUI package: `release/SalesTracker.exe` (PyInstaller onefile).
- Linux GUI package: PyInstaller onefile ELF. When frozen, `sales.db` is
  stored next to the binary.
- Settings can now delete a single order or a single product. Both are gated
  behind the existing "type RESET" unlock. A product that still has orders
  on the list is refused, naming how many, until those orders are removed.
- CLI equivalents: `delete order <id> --yes`, `delete product <id> --yes`,
  plus `o)` and `p)` entries in the interactive Settings menu.

- Orders record how they are paid: `cash`, `venmo`, or `other`. Existing
  ledgers upgrade with every order set to cash. A "Paid by" picker sits in
  the entry bar and a column shows it on the list. CLI: `order --method`,
  and `pay <id> <method>` to change one later.
- Money page (header button, `Ledger > Money…`, or `money` on the CLI):
  expected money split into cash collected, cash still to collect, and the
  same for venmo/other, with totals.
- Cash sanity check on the Money page: enter how many $1, $2, $5, $10, $20,
  $50 and $100 bills you are holding and it totals them live, then reports
  whether you are balanced, over, or short against cash collected. Venmo and
  other payments are excluded because they never reach the drawer. Counts
  are never saved. CLI: `money --count`.
- Export to CSV: every order as a row followed by a totals block. Header
  button, `Ledger > Export CSV…`, or `export --out sales.csv`.

### Changed
- Desktop window redesigned around the order list. Order entry moved from a
  permanent left-hand column into a single top bar, so the list is the
  primary surface. Pressing Enter in any entry field logs the order.
- Received figures are now edited inline on the row, by double-clicking it or
  pressing Enter, instead of in a separate panel below the list.
- A rejected received figure keeps the editor open with the reason shown
  beside the box on the same row, and writes nothing until it is valid.
- Logging an order or recording a hand-off now shows a brief confirmation in
  the footer instead of succeeding silently.
- Each row carries a progress bar next to its received / ordered figures.
- New visual language: white page, one accent, monospace reserved for
  figures. The mint-paper and brass register styling is gone. All fifteen
  text/background pairs meet WCAG AA (4.5:1).
- Empty states now explain the specific situation (no products yet, no
  orders yet, or nothing matching the current filter).
- Packaged binaries are no longer stored in git. Push a `v*` tag to attach
  `SalesTracker.exe` and `SalesTracker-linux-x86_64` to a GitHub Release.
- Ledgers now carry a schema version (`PRAGMA user_version` = 2). Older
  files upgrade on open; a file from a newer app is refused. Schema 2
  records payment method on each order.
- Library layout is now the `salestracker` package. `python3 sales_tracker.py`
  and `python3 gui.py` still work.
- Ledger is now products + orders with fulfillment, not free-text sale lines.
- Individual order delete was removed from the main list in the CLI and GUI;
  deleting is possible only from Settings.
- Settings no longer closes itself after a reset, so the result is visible.
- CI now runs the test suite on every push and pull request, and the Windows
  exe build only runs once the tests pass.

### Fixed
- Entering a price or quantity of `nan`, `Infinity`, or an oversized value
  such as `1e999` crashed instead of reporting a validation error. In the
  packaged GUI the crash was silent and the button simply did nothing.
- A legacy `sales` migration interrupted partway (crash or power loss) left
  the file permanently unopenable, because the retry failed on a duplicate
  product. The migration is now a single transaction and can be retried.
- Searching for `%` or `_` matched every row instead of the literal
  character.
- A product whose name is all digits (for example `2024`) could not be found
  by name.
- A tracker whose startup failed kept the database write-locked for the rest
  of the process.

## [0.1.4] - 2026-08-28

### Fixed
- Switching theme while the Money page was open left its separator rules in
  the old colours until the dialog was reopened. Dialogs now register their
  hairline rules, and a switch repaints them with everything else.
- A "Paid by" dropdown that had been opened at least once kept the old
  palette after a theme switch. The stale dropdown is discarded on a switch
  and rebuilt from the new colours the next time it opens.
- Following the desktop theme ran its check every four seconds on every
  platform. Windows reads the registry and keeps four seconds; Linux and
  macOS spawn a subprocess per check and now poll every fifteen.

## [0.1.3] - 2026-08-27

### Fixed
- Scrolling a dialog with a touchpad did nothing; only dragging the
  scrollbar worked. One notch of a mouse wheel reports 120 and the
  handler divided by that and rounded down to a whole number, so the
  smaller amounts a precision touchpad sends were all rounded away to
  zero. The order list had the same problem on Linux, where the Tk
  version does that rounding itself. Both now keep the leftover and
  spend it once it adds up, so a touchpad scrolls everywhere.

## [0.1.2] - 2026-08-27

### Added
- Dark mode. Appearance in Settings offers System, Light, and Dark.
  System is the default: it matches your desktop at launch and keeps
  following it while the app is open, so switching Windows to dark
  repaints the ledger without a restart. The choice is stored in
  `sales.db` and is left alone by "reset everything", which is about
  products and orders.

### Fixed
- The Money page cut off its verdict line and Close button and had no way
  to scroll to them. Settings scrolled the panel and the picker list at
  once when the wheel was over a picker, and never scrolled at all over
  anything built after the dialog first opened. The main window clipped
  its totals footer.
- Dialogs opened in the corner of the screen instead of over the main
  window.
- "cash" and "venmo" were shown in lowercase in the Paid by column and
  the picker. They are capitalised on screen now; the stored value, the
  CSV column, and what the CLI accepts are unchanged.

## [0.1.1] - 2026-08-26

### Added
- `tools/smoke_test.py`: launches a frozen build the way a double-click
  does and requires a real visible window, so a packaged build that starts
  but shows nothing is caught. Runs on Windows and Linux, and can drive the
  Windows `.exe` through Wine with `--wine`. CI runs it on both targets
  after the build step.

### Fixed
- The packaged Windows `.exe` did not launch. Double-clicking it showed an
  "Unhandled exception in script" dialog and no window, from two separate
  faults: PyInstaller 6.21.0 bundled no Tcl/Tk data for Python 3.14's
  Tcl/Tk 9 (whose library ships inside a zip, mounted through Tcl's own
  zipfs), and the CLI module read `sys.stdout` at import time, which is
  `None` in a windowed build started without a console. Either fault
  alone was enough to kill the app before it drew anything.
- The Linux CI build never published its binary. `tools/smoke_test.py` was
  given a relative path by CI and starts the app with the working directory
  set to the binary's own folder, so on Linux the path resolved twice and
  the harness failed before it could launch anything. The Windows job was
  unaffected because Windows resolves the program path differently. With
  the Linux job fixed, a tagged release now carries both a `.exe` and a
  Linux binary. The same job then failed a second time during cleanup: it
  tried to shut down a Wine server on every Linux run, including native
  ones, and the runner has no Wine installed. Cleanup now only does that
  when the run actually went through Wine.
