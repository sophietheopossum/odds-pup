"""Base class for every domain error raised by :mod:`odds_pup.core`."""

from __future__ import annotations


class CoreError(ValueError):
    """Bad input to the domain layer. The UI catches this one type and shows the message."""
