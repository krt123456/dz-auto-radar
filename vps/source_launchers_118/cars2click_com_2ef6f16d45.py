#!/usr/bin/env python3
"""Generated fail-closed acquisition launcher for cars2click.com."""
from auction_source_adapter_runtime import main_for_source


CANONICAL_IDENTITY = 'cars2click.com'


if __name__ == "__main__":
    raise SystemExit(main_for_source(CANONICAL_IDENTITY))
