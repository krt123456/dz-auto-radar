#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for automotive-auctions.nl."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'automotive-auctions.nl'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
