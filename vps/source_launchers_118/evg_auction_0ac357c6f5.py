#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for evg.auction."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'evg.auction'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
