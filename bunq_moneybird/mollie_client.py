"""Minimale Mollie API-client (Balances API).

De balance transactions van Mollie zijn de tegenhanger van het
MT940-exportbestand: betalingen, refunds, chargebacks en uitbetalingen
naar de bankrekening, elk met het netto-effect op het Mollie-saldo.

Let op: de Balances API vereist een *organization access token*
(begint met 'access_...', aan te maken in het Mollie-dashboard onder
Developers → Organization access tokens, met de scopes balances.read en
payments.read). Een gewone live_-API key geeft hier een 403.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.mollie.com/v2"


class MollieApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Mollie API-fout {status_code}: {message}")
        self.status_code = status_code


def transaction_date(tx: dict) -> date:
    # createdAt: "2026-09-20T12:06:28+00:00"
    return datetime.fromisoformat(tx["createdAt"]).date()


class MollieClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": "bunq-moneybird-sync/0.1",
            }
        )
        self._description_cache: dict[str, str | None] = {}

    def _request(self, path_or_url: str) -> dict:
        url = path_or_url if path_or_url.startswith("http") else API_URL + path_or_url
        response = self.session.get(url, timeout=30)
        if response.status_code == 403:
            raise MollieApiError(
                403,
                "Geen toegang. De Balances API vereist een organization access "
                "token (access_...) met scope balances.read; een gewone API key "
                "is niet voldoende.",
            )
        if response.status_code >= 400:
            try:
                body = response.json()
                message = f"{body.get('title', '')}: {body.get('detail', '')}"
            except ValueError:
                message = response.text[:500]
            raise MollieApiError(response.status_code, message)
        return response.json()

    def list_balances(self) -> list[dict]:
        data = self._request("/balances?limit=250")
        return (data.get("_embedded") or {}).get("balances", [])

    def get_primary_balance(self) -> dict:
        return self._request("/balances/primary")

    def fetch_balance_transactions(
        self,
        balance_id: str,
        after_transaction_id: str | None = None,
        since: date | None = None,
    ) -> list[dict]:
        """Balance transactions nieuwer dan `after_transaction_id` (of vanaf
        `since`), oudste eerst.

        Mollie geeft nieuwste-eerst met cursor-paginering; we volgen de
        next-link tot we het bekende punt of de sinds-datum bereiken.
        """
        url: str | None = f"/balances/{balance_id}/transactions?limit=250"
        collected: list[dict] = []
        while url:
            data = self._request(url)
            transactions = (data.get("_embedded") or {}).get("balance_transactions", [])
            if not transactions:
                break
            reached_end = False
            for tx in transactions:
                if after_transaction_id is not None and tx["id"] == after_transaction_id:
                    reached_end = True
                    break
                if since is not None and transaction_date(tx) < since:
                    reached_end = True
                    break
                collected.append(tx)
            if reached_end:
                break
            next_link = ((data.get("_links") or {}).get("next") or {}).get("href")
            url = next_link
        collected.reverse()
        return collected

    def get_payment_info(self, payment_id: str) -> dict:
        """Betaalgegevens voor leesbare mutaties: omschrijving en, waar de
        betaalmethode dat geeft (iDEAL, incasso), naam en IBAN van de betaler.

        Fouten zijn hier nooit fataal: dan valt de sync terug op een
        generieke omschrijving.
        """
        if payment_id in self._description_cache:
            return self._description_cache[payment_id]
        info: dict = {}
        try:
            payment = self._request(f"/payments/{payment_id}")
            details = payment.get("details") or {}
            consumer_account = (details.get("consumerAccount") or "").replace(" ", "")
            info = {
                "description": (payment.get("description") or "").strip() or None,
                "consumer_name": (
                    details.get("consumerName") or details.get("cardHolder") or ""
                ).strip() or None,
                "consumer_iban": consumer_account
                if consumer_account[:2].isalpha()
                else None,
            }
        except (MollieApiError, requests.RequestException) as exc:
            logger.debug("Betaalgegevens van %s niet op te halen: %s", payment_id, exc)
        self._description_cache[payment_id] = info
        return info
