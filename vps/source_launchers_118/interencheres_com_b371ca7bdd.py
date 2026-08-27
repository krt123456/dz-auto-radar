#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for interencheres.com."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'interencheres.com'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
