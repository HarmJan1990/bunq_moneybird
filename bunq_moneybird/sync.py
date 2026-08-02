"""De eigenlijke synchronisatie: bunq-betalingen -> Moneybird-afschriften."""

from __future__ import annotations

import logging
from datetime import date, timedelta

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

        logger.info(
            "[%s] %s: %d nieuwe transactie(s) (%s t/m %s).",
            company.name, mapping.iban, len(payments),
            _payment_date(payments[0]).isoformat(),
            _payment_date(payments[-1]).isoformat(),
        )

        for chunk_start in range(0, len(payments), MUTATIONS_PER_STATEMENT):
            chunk = payments[chunk_start : chunk_start + MUTATIONS_PER_STATEMENT]
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

        total += len(payments)

    return total
