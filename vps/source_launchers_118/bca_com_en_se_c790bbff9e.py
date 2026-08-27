#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for bca.com/en/se."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'bca.com/en/se'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
