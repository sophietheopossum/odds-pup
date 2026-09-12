"""Console entry point for ``odds-pup``.

The Qt user interface is not implemented yet; this stub exists so that ``uv run odds-pup`` works
and reports the version. It will hand over to :mod:`odds_pup.ui` once that package lands.
"""

from __future__ import annotations

import argparse
import sys

from odds_pup import __version__


def main(argv: list[str] | None = None) -> int:
    """Parse command line arguments and launch the application."""
    parser = argparse.ArgumentParser(prog="odds-pup", description="Matched betting ledger.")
    parser.add_argument("--version", action="version", version=f"odds-pup {__version__}")
    parser.add_argument(
        "--data-dir",
        help="Directory holding the ledger database and backups "
        "(default: platform user data dir, or $ODDS_PUP_DATA_DIR).",
    )
    parser.parse_args(argv)
    sys.stderr.write(
        "odds-pup: the desktop UI is not implemented yet. See docs/SPEC.md for the plan.\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
