#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for auction24.cz."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'auction24.cz'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
