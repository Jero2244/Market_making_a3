"""Packaging entry point. The executable exposes only the read-only desktop app."""
from market_making.desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
