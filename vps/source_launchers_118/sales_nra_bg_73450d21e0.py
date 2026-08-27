#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for sales.nra.bg."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'sales.nra.bg'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
