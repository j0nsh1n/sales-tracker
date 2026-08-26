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
  When frozen, `sales.db` is stored next to the executable.

### Changed
- Ledger is now products + orders with fulfillment, not free-text sale lines.
- Individual order delete was removed from the CLI and GUI.
