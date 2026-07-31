"""Minimale Moneybird API-client (alleen wat de sync nodig heeft)."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

API_URL = "https://moneybird.com/api/v2"


class MoneybirdApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Moneybird API-fout {status_code}: {message}")
        self.status_code = status_code


class MoneybirdClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "bunq-moneybird-sync/0.1",
            }
        )

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        response = self.session.request(method, API_URL + path, json=payload, timeout=30)
        if response.status_code == 429:
            raise MoneybirdApiError(429, "Rate limit bereikt; probeer het later opnieuw.")
        if response.status_code >= 400:
            try:
                message = str(response.json())[:500]
            except ValueError:
                message = response.text[:500]
            raise MoneybirdApiError(response.status_code, message)
        if not response.text:
            return {}
        return response.json()

    def list_administrations(self) -> list[dict]:
        return self._request("GET", "/administrations.json")

    def list_financial_accounts(self, administration_id: str) -> list[dict]:
        return self._request("GET", f"/{administration_id}/financial_accounts.json")

    def create_financial_statement(
        self,
        administration_id: str,
        financial_account_id: str,
        reference: str,
        mutations: list[dict],
        official_balance: str | None = None,
    ) -> dict:
        """Maak een bankafschrift met mutaties aan.

        Elke mutatie: {"date": "YYYY-MM-DD", "amount": "-12.34", "message": "...",
                       "contra_account_name": "...", "contra_account_number": "..."}
        """
        payload = {
            "financial_statement": {
                "financial_account_id": financial_account_id,
                "reference": reference,
                "financial_mutations_attributes": {
                    str(i): mutation for i, mutation in enumerate(mutations)
                },
            }
        }
        if official_balance is not None:
            payload["financial_statement"]["official_balance"] = official_balance
        return self._request("POST", f"/{administration_id}/financial_statements.json", payload)
