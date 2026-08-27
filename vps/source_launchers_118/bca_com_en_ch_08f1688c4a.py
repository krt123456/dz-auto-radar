#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for bca.com/en_CH."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'bca.com/en_CH'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
