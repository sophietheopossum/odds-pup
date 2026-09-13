"""Console entry point for ``odds-pup``: hands over to the Qt application."""

from __future__ import annotations

import sys

from odds_pup import __version__


def main(argv: list[str] | None = None) -> int:
    """Launch the desktop application; ``--version`` answers without importing Qt."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--version"]:
        print(f"odds-pup {__version__}")
        return 0
    from odds_pup.ui.app import run  # noqa: PLC0415 - keep Qt out of the --version path

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
