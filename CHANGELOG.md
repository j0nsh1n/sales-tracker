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

### Changed
- Packaged binaries are no longer stored in git. Push a `v*` tag to attach
  `SalesTracker.exe` and `SalesTracker-linux-x86_64` to a GitHub Release.
- Ledgers now carry a schema version (`PRAGMA user_version` = 1). Older
  files upgrade on open; a file from a newer app is refused.
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
