#!/usr/bin/env python3
"""Desktop ledger entry point. Implementation lives in salestracker.ui.gui."""

from salestracker.ui.gui import (
    MoneyPanel,
    OrderEditor,
    ProductEditor,
    ProductWizard,
    SalesApp,
    SettingsDialog,
    main,
    messagebox,
)

__all__ = [
    "MoneyPanel",
    "OrderEditor",
    "ProductEditor",
    "ProductWizard",
    "SalesApp",
    "SettingsDialog",
    "main",
    "messagebox",
]

if __name__ == "__main__":
    raise SystemExit(main())
