"""Minimale bunq API-client.

Implementeert de officiële bunq-flow zonder externe SDK:

1. POST /installation      — registreer onze RSA-publieke sleutel, krijg installation-token
2. POST /device-server     — registreer dit apparaat met de API key
3. POST /session-server    — open een sessie, krijg sessietoken + user id

Alle requests na de installation worden ondertekend met onze private sleutel
(SHA256/PKCS#1 v1.5 over de request-body, base64 in de
X-Bunq-Client-Signature header).

De context (private sleutel, tokens, user id) wordt opgeslagen in een lokaal
JSON-bestand zodat installatie en apparaatregistratie maar één keer gebeuren.
Verlopen sessies worden automatisch vernieuwd.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

USER_AGENT = "bunq-moneybird-sync/0.1"


class BunqApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"bunq API-fout {status_code}: {message}")
        self.status_code = status_code


def _api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


class BunqClient:
    def __init__(self, api_url: str, api_key: str, context_file: Path, wildcard_ip: bool = False):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.context_file = context_file
        self.wildcard_ip = wildcard_ip
        self.session = requests.Session()

        self._private_key: rsa.RSAPrivateKey | None = None
        self._installation_token: str | None = None
        self._session_token: str | None = None
        self._user_id: int | None = None

        self._load_context()

    # ------------------------------------------------------------------ context

    def _load_context(self) -> None:
        if not self.context_file.exists():
            return
        try:
            data = json.loads(self.context_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Contextbestand %s onleesbaar; wordt opnieuw aangemaakt.", self.context_file)
            return
        if data.get("api_key_fingerprint") != _api_key_fingerprint(self.api_key):
            logger.info("API key gewijzigd; context %s wordt opnieuw opgebouwd.", self.context_file)
            return
        self._private_key = serialization.load_pem_private_key(
            data["private_key_pem"].encode(), password=None
        )
        self._installation_token = data["installation_token"]
        self._session_token = data.get("session_token")
        self._user_id = data.get("user_id")

    def _save_context(self) -> None:
        assert self._private_key is not None and self._installation_token is not None
        pem = self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        data = {
            "api_key_fingerprint": _api_key_fingerprint(self.api_key),
            "private_key_pem": pem,
            "installation_token": self._installation_token,
            "session_token": self._session_token,
            "user_id": self._user_id,
        }
        self.context_file.parent.mkdir(parents=True, exist_ok=True)
        self.context_file.write_text(json.dumps(data, indent=2))
        self.context_file.chmod(0o600)

    # ------------------------------------------------------------------ signing

    def _sign(self, body: bytes) -> str:
        assert self._private_key is not None
        signature = self._private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def _headers(self, token: str | None, body: bytes, signed: bool) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
            "X-Bunq-Client-Request-Id": str(uuid.uuid4()),
            "X-Bunq-Geolocation": "0 0 0 0 000",
            "X-Bunq-Language": "nl_NL",
            "X-Bunq-Region": "nl_NL",
        }
        if token:
            headers["X-Bunq-Client-Authentication"] = token
        if signed:
            headers["X-Bunq-Client-Signature"] = self._sign(body)
        return headers

    # ------------------------------------------------------------------ http

    def _raw_request(
        self,
        method: str,
        path: str,
        token: str | None,
        payload: dict | None = None,
        signed: bool = True,
    ) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        url = self.api_url + path
        response = self.session.request(
            method,
            url,
            data=body if payload is not None else None,
            headers=self._headers(token, body, signed),
            timeout=30,
        )
        if response.status_code >= 400:
            try:
                errors = response.json().get("Error", [])
                message = "; ".join(e.get("error_description", "") for e in errors)
            except (ValueError, AttributeError):
                message = response.text[:500]
            raise BunqApiError(response.status_code, message)
        return response.json()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Sessie-gebonden request; vernieuwt de sessie automatisch bij verlopen token."""
        self._ensure_session()
        try:
            return self._raw_request(method, path, self._session_token, payload)
        except BunqApiError as exc:
            if exc.status_code == 401:
                logger.info("bunq-sessie verlopen; nieuwe sessie wordt geopend.")
                self._create_session()
                return self._raw_request(method, path, self._session_token, payload)
            raise

    # ------------------------------------------------------------------ auth-flow

    def _ensure_session(self) -> None:
        if self._private_key is None or self._installation_token is None:
            self._install()
            self._register_device()
            self._create_session()
        elif self._session_token is None or self._user_id is None:
            self._create_session()

    def _install(self) -> None:
        logger.info("Nieuwe bunq-installatie wordt aangemaakt.")
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = (
            self._private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        data = self._raw_request(
            "POST", "/v1/installation", token=None,
            payload={"client_public_key": public_pem}, signed=False,
        )
        self._installation_token = _find(data, "Token")["token"]

    def _register_device(self) -> None:
        payload: dict = {
            "description": "bunq-moneybird-sync",
            "secret": self.api_key,
        }
        if self.wildcard_ip:
            payload["permitted_ips"] = ["*"]
        try:
            self._raw_request("POST", "/v1/device-server", self._installation_token, payload)
        except BunqApiError as exc:
            # Al geregistreerd voor deze API key is geen probleem.
            if "already" not in str(exc).lower():
                raise

    def _create_session(self) -> None:
        data = self._raw_request(
            "POST", "/v1/session-server", self._installation_token,
            payload={"secret": self.api_key},
        )
        self._session_token = _find(data, "Token")["token"]
        for user_key in ("UserPerson", "UserCompany", "UserApiKey", "UserPaymentServiceProvider"):
            user = _find(data, user_key, required=False)
            if user:
                self._user_id = user["id"]
                break
        if self._user_id is None:
            raise BunqApiError(500, "Geen gebruikers-id gevonden in session-server-antwoord.")
        self._save_context()

    # ------------------------------------------------------------------ publieke API

    def list_monetary_accounts(self) -> list[dict]:
        """Alle actieve rekeningen als [{'id', 'iban', 'description', 'balance', 'currency'}]."""
        self._ensure_session()
        data = self._request("GET", f"/v1/user/{self._user_id}/monetary-account?count=200")
        accounts = []
        for item in data.get("Response", []):
            for account in item.values():
                iban = next(
                    (a.get("value") for a in account.get("alias", []) if a.get("type") == "IBAN"),
                    None,
                )
                accounts.append(
                    {
                        "id": account["id"],
                        "iban": iban,
                        "description": account.get("description"),
                        "status": account.get("status"),
                        "balance": (account.get("balance") or {}).get("value"),
                        "currency": (account.get("balance") or {}).get("currency"),
                    }
                )
        return accounts

    def find_account_by_iban(self, iban: str) -> dict:
        wanted = iban.replace(" ", "").upper()
        for account in self.list_monetary_accounts():
            if (account["iban"] or "").replace(" ", "").upper() == wanted:
                return account
        raise BunqApiError(404, f"Geen bunq-rekening gevonden met IBAN {iban}.")

    def create_draft_payment(self, monetary_account_id: int, entries: list[dict]) -> int:
        """Zet een concept-betaling (batch) klaar die in de bunq-app moet worden
        goedgekeurd voordat er iets wordt overgemaakt.

        Elke entry: {"amount": {"value": "42.50", "currency": "EUR"},
                     "counterparty_alias": {"type": "IBAN", "value": "NL..", "name": "..."},
                     "description": "..."}
        """
        self._ensure_session()
        data = self._request(
            "POST",
            f"/v1/user/{self._user_id}/monetary-account/{monetary_account_id}/draft-payment",
            payload={"number_of_required_accepts": 1, "entries": entries},
        )
        return _find(data, "Id")["id"]

    def fetch_payments(
        self,
        monetary_account_id: int,
        after_payment_id: int | None = None,
        since: date | None = None,
    ) -> list[dict]:
        """Betalingen nieuwer dan `after_payment_id` (of vanaf `since`), oudste eerst.

        bunq geeft betalingen nieuwste-eerst terug; we volgen de older_url-paginering
        tot we het al-gesynchroniseerde punt (of de sinds-datum) bereiken.
        """
        self._ensure_session()
        path = f"/v1/user/{self._user_id}/monetary-account/{monetary_account_id}/payment?count=200"
        collected: list[dict] = []
        while path:
            data = self._request("GET", path)
            items = [item["Payment"] for item in data.get("Response", []) if "Payment" in item]
            if not items:
                break
            reached_end = False
            for payment in items:
                if after_payment_id is not None and payment["id"] <= after_payment_id:
                    reached_end = True
                    break
                if since is not None and _payment_date(payment) < since:
                    reached_end = True
                    break
                collected.append(payment)
            if reached_end:
                break
            path = (data.get("Pagination") or {}).get("older_url")
        collected.reverse()
        return collected


def _payment_date(payment: dict) -> date:
    # bunq: "2026-07-30 14:03:12.123456"
    return datetime.strptime(payment["created"][:10], "%Y-%m-%d").date()


def _find(data: dict, key: str, required: bool = True) -> dict | None:
    for item in data.get("Response", []):
        if key in item:
            return item[key]
    if required:
        raise BunqApiError(500, f"'{key}' niet gevonden in bunq-antwoord.")
    return None
