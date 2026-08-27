#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for rbauction.eu."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'rbauction.eu'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
