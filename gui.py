#!/usr/bin/env python3
"""Desktop ledger entry point. Implementation lives in salestracker.ui.gui."""

from salestracker.ui.gui import (
    ProductWizard,
    SalesApp,
    SettingsDialog,
    main,
    messagebox,
)

__all__ = ["ProductWizard", "SalesApp", "SettingsDialog", "main", "messagebox"]

if __name__ == "__main__":
    raise SystemExit(main())
