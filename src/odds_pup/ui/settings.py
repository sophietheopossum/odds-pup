"""Per-user UI preferences (last exchange, geometry). Nothing here is ledger data."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

ORGANISATION = "odds-pup"
APPLICATION = "odds-pup"


class UiSettings:
    def __init__(self, settings: QSettings | None = None) -> None:
        self._s = settings or QSettings(ORGANISATION, APPLICATION)

    @property
    def last_exchange(self) -> str:
        return str(self._s.value("newbet/exchange", "", type=str))

    @last_exchange.setter
    def last_exchange(self, name: str) -> None:
        self._s.setValue("newbet/exchange", name)

    @property
    def last_commission_bp(self) -> int | None:
        raw = self._s.value("newbet/commission_bp", None)
        if raw is None or raw == "":
            return None
        try:
            return int(str(raw))
        except ValueError:
            return None

    @last_commission_bp.setter
    def last_commission_bp(self, bp: int) -> None:
        self._s.setValue("newbet/commission_bp", int(bp))

    @property
    def last_bookmaker(self) -> str:
        return str(self._s.value("newbet/bookmaker", "", type=str))

    @last_bookmaker.setter
    def last_bookmaker(self, name: str) -> None:
        self._s.setValue("newbet/bookmaker", name)

    def geometry(self) -> QByteArray:
        value = self._s.value("window/geometry")
        return value if isinstance(value, QByteArray) else QByteArray()

    def save_geometry(self, data: QByteArray) -> None:
        self._s.setValue("window/geometry", data)

    def window_state(self) -> QByteArray:
        value = self._s.value("window/state")
        return value if isinstance(value, QByteArray) else QByteArray()

    def save_window_state(self, data: QByteArray) -> None:
        self._s.setValue("window/state", data)

    def sync(self) -> None:
        self._s.sync()
