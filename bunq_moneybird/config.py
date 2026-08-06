"""Configuratie laden en valideren."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

PRODUCTION_API_URL = "https://api.bunq.com"
SANDBOX_API_URL = "https://public-api.sandbox.bunq.com"


class ConfigError(Exception):
    pass


@dataclass
class AccountMapping:
    iban: str
    moneybird_financial_account_id: str
    # Eerste sync begint op deze datum in plaats van initial_sync_days terug.
    # Handig als een oude koppeling al tot een bepaalde datum heeft geïmporteerd.
    sync_from: date | None = None


@dataclass
class Company:
    name: str
    bunq_api_key_env: str
    bunq_context_file: Path
    moneybird_administration_id: str
    moneybird_token_env: str
    accounts: list[AccountMapping]
    bunq_wildcard_ip: bool = False

    @property
    def bunq_api_key(self) -> str:
        key = os.environ.get(self.bunq_api_key_env, "").strip()
        if not key:
            raise ConfigError(
                f"Omgevingsvariabele {self.bunq_api_key_env} (bunq API key voor "
                f"'{self.name}') is niet gezet."
            )
        return key

    @property
    def moneybird_token(self) -> str:
        token = os.environ.get(self.moneybird_token_env, "").strip()
        if not token:
            raise ConfigError(
                f"Omgevingsvariabele {self.moneybird_token_env} (Moneybird-token voor "
                f"'{self.name}') is niet gezet."
            )
        return token


@dataclass
class Config:
    companies: list[Company]
    bunq_api_url: str = PRODUCTION_API_URL
    initial_sync_days: int = 30
    state_file: Path = field(default_factory=lambda: Path(".state/sync-state.json"))

    def company(self, name: str) -> Company:
        for company in self.companies:
            if company.name == name:
                return company
        known = ", ".join(c.name for c in self.companies) or "(geen)"
        raise ConfigError(f"Onbekend bedrijf '{name}'. Beschikbaar: {known}")


def load_dotenv(path: Path) -> None:
    """Laad KEY=VALUE-regels uit een .env-bestand in de omgeving.

    Al gezette omgevingsvariabelen winnen van het bestand. Ondersteunt
    commentaarregels (#), een optioneel 'export '-voorvoegsel en waarden
    tussen enkele of dubbele aanhalingstekens.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(
            f"Configuratiebestand {path} niet gevonden. "
            "Kopieer config.example.yaml naar config.yaml en vul het in."
        )
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    base_dir = path.resolve().parent
    defaults = raw.get("defaults") or {}
    moneybird = raw.get("moneybird") or {}

    companies: list[Company] = []
    for entry in raw.get("companies") or []:
        name = entry.get("name")
        if not name:
            raise ConfigError("Elk bedrijf in 'companies' heeft een 'name' nodig.")
        bunq = entry.get("bunq") or {}
        api_key_env = bunq.get("api_key_env")
        if not api_key_env:
            raise ConfigError(f"Bedrijf '{name}': bunq.api_key_env ontbreekt.")
        context_file = bunq.get("context_file") or f".bunq/{name}-context.json"

        company_moneybird = entry.get("moneybird") or {}
        administration_id = company_moneybird.get("administration_id") or entry.get(
            "moneybird_administration_id"
        )
        if not administration_id:
            raise ConfigError(f"Bedrijf '{name}': moneybird.administration_id ontbreekt.")
        # Moneybird geeft tokens per administratie uit, dus elk bedrijf heeft
        # zijn eigen token nodig. Een top-level moneybird.token_env blijft
        # werken als fallback voor wie één token voor alles heeft.
        token_env = company_moneybird.get("token_env") or moneybird.get("token_env")
        if not token_env:
            raise ConfigError(f"Bedrijf '{name}': moneybird.token_env ontbreekt.")

        accounts = []
        for acc in entry.get("accounts") or []:
            iban = (acc.get("iban") or "").replace(" ", "").upper()
            fa_id = acc.get("moneybird_financial_account_id")
            if not iban or not fa_id:
                raise ConfigError(
                    f"Bedrijf '{name}': elk account heeft 'iban' en "
                    "'moneybird_financial_account_id' nodig."
                )
            sync_from = acc.get("sync_from")
            if sync_from is not None and not isinstance(sync_from, date):
                try:
                    sync_from = date.fromisoformat(str(sync_from))
                except ValueError:
                    raise ConfigError(
                        f"Bedrijf '{name}', rekening {iban}: sync_from moet een "
                        f"datum zijn (JJJJ-MM-DD), niet '{sync_from}'."
                    )
            accounts.append(
                AccountMapping(
                    iban=iban,
                    moneybird_financial_account_id=str(fa_id),
                    sync_from=sync_from,
                )
            )

        companies.append(
            Company(
                name=name,
                bunq_api_key_env=api_key_env,
                bunq_context_file=(base_dir / context_file),
                moneybird_administration_id=str(administration_id),
                moneybird_token_env=token_env,
                accounts=accounts,
                bunq_wildcard_ip=bool(bunq.get("wildcard_ip", False)),
            )
        )

    if not companies:
        raise ConfigError("Geen bedrijven geconfigureerd onder 'companies'.")

    api_url = defaults.get("bunq_api_url") or PRODUCTION_API_URL
    if defaults.get("sandbox"):
        api_url = SANDBOX_API_URL

    return Config(
        companies=companies,
        bunq_api_url=api_url,
        initial_sync_days=int(defaults.get("initial_sync_days", 30)),
        state_file=base_dir / (defaults.get("state_file") or ".state/sync-state.json"),
    )
