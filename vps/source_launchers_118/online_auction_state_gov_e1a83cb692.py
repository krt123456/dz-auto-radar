#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for online-auction.state.gov."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'online-auction.state.gov'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
