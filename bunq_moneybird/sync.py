"""De eigenlijke synchronisatie: bunq-betalingen -> Moneybird-afschriften."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .bunq_client import BunqClient, _payment_date
from .config import Company, Config
from .moneybird_client import MoneybirdClient
from .state import SyncState

logger = logging.getLogger(__name__)

# Moneybird accepteert grote afschriften, maar we houden ze behapbaar.
MUTATIONS_PER_STATEMENT = 100


def payment_to_mutation(payment: dict) -> dict:
    counterparty = payment.get("counterparty_alias") or {}
    message = (payment.get("description") or "").strip()
    if not message:
        message = counterparty.get("display_name") or "bunq-transactie"
    return {
        "date": _payment_date(payment).isoformat(),
        "amount": (payment.get("amount") or {}).get("value", "0"),
        "message": message[:255],
        "contra_account_name": (counterparty.get("display_name") or "")[:255],
        "contra_account_number": counterparty.get("iban") or "",
    }


def _amount_key(value: str | None) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def filter_new_payments(
    payments: list[dict], existing_mutations: list[dict]
) -> tuple[list[dict], int]:
    """Laat betalingen weg die al als mutatie in Moneybird staan.

    Matcht op (datum, bedrag) met aantallen: staat een bedrag op een dag al
    n keer in Moneybird, dan worden er maximaal n bunq-betalingen met
    diezelfde datum en hetzelfde bedrag overgeslagen. Zo blijven legitieme
    dubbele betalingen behouden en worden gaten die een eerdere koppeling
    liet vallen alsnog geïmporteerd.
    """
    existing = Counter(
        (m.get("date"), _amount_key(m.get("amount"))) for m in existing_mutations
    )
    new_payments: list[dict] = []
    skipped = 0
    for payment in payments:
        key = (
            _payment_date(payment).isoformat(),
            _amount_key((payment.get("amount") or {}).get("value")),
        )
        if existing[key] > 0:
            existing[key] -= 1
            skipped += 1
        else:
            new_payments.append(payment)
    return new_payments, skipped


def sync_company(
    config: Config,
    company: Company,
    moneybird: MoneybirdClient,
    state: SyncState,
    dry_run: bool = False,
) -> int:
    """Synchroniseert alle gekoppelde rekeningen van één bedrijf.

    Geeft het aantal overgezette transacties terug.
    """
    if not company.accounts:
        logger.warning("[%s] Geen rekeningen geconfigureerd; overgeslagen.", company.name)
        return 0

    bunq = BunqClient(
        api_url=config.bunq_api_url,
        api_key=company.bunq_api_key,
        context_file=company.bunq_context_file,
        wildcard_ip=company.bunq_wildcard_ip,
    )

    total = 0
    for mapping in company.accounts:
        account = bunq.find_account_by_iban(mapping.iban)
        last_id = state.last_payment_id(company.name, mapping.iban)
        since = None
        if last_id is None:
            since = mapping.sync_from or (
                date.today() - timedelta(days=config.initial_sync_days)
            )
            logger.info(
                "[%s] %s: eerste sync, transacties vanaf %s worden opgehaald%s.",
                company.name, mapping.iban, since.isoformat(),
                " (sync_from uit config)" if mapping.sync_from else "",
            )

        payments = bunq.fetch_payments(account["id"], after_payment_id=last_id, since=since)
        if not payments:
            logger.info("[%s] %s: geen nieuwe transacties.", company.name, mapping.iban)
            continue

        # Vergelijk met wat er al in Moneybird staat, zodat overlap met een
        # eerdere koppeling (of een verloren statebestand) nooit tot dubbele
        # mutaties leidt.
        existing = moneybird.list_financial_mutations(
            administration_id=company.moneybird_administration_id,
            financial_account_id=mapping.moneybird_financial_account_id,
            start=_payment_date(payments[0]).strftime("%Y%m%d"),
            end=_payment_date(payments[-1]).strftime("%Y%m%d"),
        )
        new_payments, skipped = filter_new_payments(payments, existing)
        if skipped:
            logger.info(
                "[%s] %s: %d transactie(s) overgeslagen die al in Moneybird staan.",
                company.name, mapping.iban, skipped,
            )
        if not new_payments:
            logger.info(
                "[%s] %s: alles staat al in Moneybird; niets te doen.",
                company.name, mapping.iban,
            )
            if not dry_run:
                state.update(company.name, mapping.iban, payments[-1]["id"])
                state.save()
            continue

        logger.info(
            "[%s] %s: %d nieuwe transactie(s) (%s t/m %s).",
            company.name, mapping.iban, len(new_payments),
            _payment_date(new_payments[0]).isoformat(),
            _payment_date(new_payments[-1]).isoformat(),
        )

        for chunk_start in range(0, len(new_payments), MUTATIONS_PER_STATEMENT):
            chunk = new_payments[chunk_start : chunk_start + MUTATIONS_PER_STATEMENT]
            reference = (
                f"bunq {mapping.iban} "
                f"#{chunk[0]['id']}-{chunk[-1]['id']}"
            )
            mutations = [payment_to_mutation(p) for p in chunk]

            if dry_run:
                logger.info("  [dry-run] Zou afschrift '%s' aanmaken met %d mutatie(s):",
                            reference, len(mutations))
                for m in mutations:
                    logger.info("    %s  %10s  %s", m["date"], m["amount"], m["message"][:60])
                continue

            moneybird.create_financial_statement(
                administration_id=company.moneybird_administration_id,
                financial_account_id=mapping.moneybird_financial_account_id,
                reference=reference,
                mutations=mutations,
            )
            state.update(company.name, mapping.iban, chunk[-1]["id"])
            state.save()
            logger.info("  Afschrift '%s' aangemaakt (%d mutaties).", reference, len(mutations))

        if not dry_run:
            # Ook overgeslagen (al bestaande) betalingen aan het einde tellen
            # mee als verwerkt.
            state.update(company.name, mapping.iban, payments[-1]["id"])
            state.save()
        total += len(new_payments)

    return total
