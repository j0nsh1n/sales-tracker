#!/usr/bin/env python3
"""Sales Tracker CLI entry point. Implementation lives in the salestracker package."""

from salestracker import *  # noqa: F403
from salestracker import (  # noqa: F401
    DEFAULT_DB,
    SCHEMA_VERSION,
    UNITS,
    InteractiveSession,
    Order,
    Product,
    SalesTracker,
    Summary,
    TrackerError,
    application_dir,
    build_parser,
    collect_product_answers,
    format_money,
    format_qty,
    main,
    parse_money,
    parse_qty,
    parse_quantity,
)

if __name__ == "__main__":
    raise SystemExit(main())
