"""Allow `python -m intentradar`."""

from __future__ import annotations

from intentradar.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
