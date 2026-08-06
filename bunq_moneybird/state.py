"""Sync-staat: laatst verwerkte bunq payment-id per bedrijf/IBAN.

Hiermee worden transacties nooit dubbel naar Moneybird gestuurd, ook niet
als de sync vaker draait.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class PayoutState:
    """Referenties van al ingediende uitbetalingen, zodat dezelfde export
    nooit twee keer wordt uitbetaald."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"submitted": {}}
        if path.exists():
            self._data = json.loads(path.read_text())
            self._data.setdefault("submitted", {})

    def is_submitted(self, reference: str) -> bool:
        return reference in self._data["submitted"]

    def mark_submitted(self, reference: str, draft_id: int, source: str) -> None:
        self._data["submitted"][reference] = {
            "draft_payment_id": draft_id,
            "source": source,
            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))


class SyncState:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"accounts": {}}
        if path.exists():
            self._data = json.loads(path.read_text())
            self._data.setdefault("accounts", {})

    @staticmethod
    def _key(company: str, iban: str) -> str:
        return f"{company}/{iban}"

    def last_payment_id(self, company: str, iban: str) -> int | None:
        entry = self._data["accounts"].get(self._key(company, iban))
        return entry.get("last_payment_id") if entry else None

    def first_sync_since(self, company: str, iban: str) -> str | None:
        """De startdatum (ISO) van een eerdere sync die nog geen transacties
        vond; None als er nog nooit gesynct is of er al een payment-id is."""
        entry = self._data["accounts"].get(self._key(company, iban))
        return entry.get("since") if entry else None

    def update(self, company: str, iban: str, last_payment_id: int) -> None:
        self._data["accounts"][self._key(company, iban)] = {
            "last_payment_id": last_payment_id,
            "last_synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def remember_empty_first_sync(self, company: str, iban: str, since: str) -> None:
        """Eerste sync zonder transacties: onthoud de startdatum, zodat de
        volgende run geen 'eerste sync' meer is."""
        self._data["accounts"][self._key(company, iban)] = {
            "since": since,
            "last_synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))
