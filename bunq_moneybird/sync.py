"""De eigenlijke synchronisatie: bunq-betalingen -> Moneybird-afschriften."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .bunq_client import BunqClient, _payment_date
from .config import Company, Config
from .mollie_client import MollieClient, transaction_date
from .moneybird_client import MoneybirdClient
from .state import SyncState

logger = logging.getLogger(__name__)

# Moneybird accepteert grote afschriften, maar we houden ze behapbaar.
MUTATIONS_PER_STATEMENT = 100

# Codes met deze voorvoegsels zijn door deze tool gezet en identificeren
# exact één brontransactie.
KNOWN_CODE_PREFIXES = ("bunq-", "mollie-")


def _payment_code(payment: dict) -> str:
    """Unieke code per bunq-betaling; maakt de dedup exact."""
    return f"bunq-{payment['id']}"


def payment_to_mutation(payment: dict) -> dict:
    counterparty = payment.get("counterparty_alias") or {}
    message = (payment.get("description") or "").strip()
    if not message:
        message = counterparty.get("display_name") or "bunq-transactie"
    return {
        "date": _payment_date(payment).isoformat(),
        "amount": (payment.get("amount") or {}).get("value", "0"),
        "message": message[:255],
        "code": _payment_code(payment),
        "contra_account_name": (counterparty.get("display_name") or "")[:255],
        "contra_account_number": counterparty.get("iban") or "",
    }


def _amount_key(value: str | None) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def _normalize_iban(value: str | None) -> str:
    return (value or "").replace(" ", "").upper()


def filter_new_mutations(
    candidates: list[dict], existing_mutations: list[dict]
) -> tuple[list[dict], int]:
    """Laat kandidaat-mutaties weg die al in Moneybird staan.

    Mutaties die door deze tool zijn aangemaakt dragen een code
    ('bunq-<id>' of 'mollie-<id>') en matchen daarop exact. Alleen voor
    bestaande mutaties zonder zo'n code (bijv. van de oude bunq-koppeling
    of oude MT940-uploads) geldt nog een heuristiek met aantallen: mét
    tegenrekening op (datum, bedrag, tegenrekening-IBAN), zonder
    tegenrekening op (datum, bedrag).
    """
    known_codes: set[str] = set()
    with_contra: Counter = Counter()
    without_contra: Counter = Counter()
    for mutation in existing_mutations:
        code = (mutation.get("code") or "").strip()
        if code.startswith(KNOWN_CODE_PREFIXES):
            # Exact herleidbaar naar één brontransactie; doet niet mee aan
            # de heuristiek.
            known_codes.add(code)
            continue
        key = (mutation.get("date"), _amount_key(mutation.get("amount")))
        contra = _normalize_iban(mutation.get("contra_account_number"))
        if contra:
            with_contra[key + (contra,)] += 1
        else:
            without_contra[key] += 1

    new_mutations: list[dict] = []
    skipped = 0
    for mutation in candidates:
        code = (mutation.get("code") or "").strip()
        if code and code in known_codes:
            skipped += 1
            continue
        key = (mutation["date"], _amount_key(mutation.get("amount")))
        contra = _normalize_iban(mutation.get("contra_account_number"))
        if contra and with_contra[key + (contra,)] > 0:
            with_contra[key + (contra,)] -= 1
            skipped += 1
        elif without_contra[key] > 0:
            without_contra[key] -= 1
            skipped += 1
        else:
            new_mutations.append(mutation)
    return new_mutations, skipped


def filter_new_payments(
    payments: list[dict], existing_mutations: list[dict]
) -> tuple[list[dict], int]:
    """Als filter_new_mutations, maar voor bunq-betalingen (geeft de
    betalingen zelf terug)."""
    pairs = [(payment, payment_to_mutation(payment)) for payment in payments]
    kept, skipped = filter_new_mutations([m for _, m in pairs], existing_mutations)
    kept_ids = {id(m) for m in kept}
    return [p for p, m in pairs if id(m) in kept_ids], skipped


def payment_matches(payment: dict, terms: list[str]) -> bool:
    """True als een zoekterm in de omschrijving voorkomt of gelijk is aan het
    bunq payment-id."""
    description = (payment.get("description") or "").lower()
    return any(
        term.lower() in description or term == str(payment.get("id"))
        for term in terms
    )


def sync_company(
    config: Config,
    company: Company,
    moneybird: MoneybirdClient,
    state: SyncState,
    dry_run: bool = False,
    rescan_days: int | None = None,
) -> int:
    """Synchroniseert alle gekoppelde rekeningen van één bedrijf.

    Geeft het aantal overgezette transacties terug.
    """
    if not company.accounts:
        if company.mollie is None:
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
        if rescan_days:
            # Negeer het onthouden punt en loop de hele periode opnieuw
            # langs; de vergelijking met bestaande Moneybird-mutaties houdt
            # alles tegen wat er al staat.
            last_id = None
            since = date.today() - timedelta(days=rescan_days)
            logger.info(
                "[%s] %s: rescan, transacties vanaf %s worden opnieuw vergeleken.",
                company.name, mapping.iban, since.isoformat(),
            )
        elif last_id is None:
            stored_since = state.first_sync_since(company.name, mapping.iban)
            if stored_since:
                # Eerdere sync vond nog geen transacties; ga verder vanaf
                # hetzelfde startpunt.
                since = date.fromisoformat(stored_since)
            else:
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
            if last_id is None and since is not None and not dry_run and not rescan_days:
                state.remember_empty_first_sync(
                    company.name, mapping.iban, since.isoformat()
                )
                state.save()
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


_MOLLIE_TYPE_LABELS = {
    "payment": "betaling",
    "capture": "betaling",
    "refund": "terugbetaling",
    "returned-refund": "teruggekomen terugbetaling",
    "chargeback": "chargeback",
    "chargeback-reversal": "teruggedraaide chargeback",
    "failed-payment": "mislukte betaling",
    "unauthorized-direct-debit": "gestorneerde incasso",
    "outgoing-transfer": "uitbetaling naar bankrekening",
    "canceled-outgoing-transfer": "geannuleerde uitbetaling",
    "returned-transfer": "teruggekomen uitbetaling",
    "incoming-transfer": "ontvangen overboeking",
    "balance-correction": "saldocorrectie",
    "invoice-compensation": "factuurverrekening",
    "fee-prepayment": "ingehouden kosten",
    "application-fee": "platformkosten",
    "rolling-reserve-hold": "aangehouden reserve",
    "rolling-reserve-release": "vrijgegeven reserve",
}


def mollie_transaction_to_mutations(tx: dict, payment_info: dict | None = None) -> list[dict]:
    """Mollie balance transaction -> Moneybird-mutatie(s).

    Een transactie mét transactiekosten (deductions) wordt gesplitst in een
    bruto-mutatie en een aparte kostenmutatie, zodat het bruto-bedrag in
    Moneybird op de bijbehorende factuur matcht — zoals in de MT940-export.
    Samen tellen ze op tot resultAmount, dus het saldo blijft kloppend.
    """
    payment_info = payment_info or {}
    tx_type = tx.get("type") or "transactie"
    label = _MOLLIE_TYPE_LABELS.get(tx_type, tx_type)
    context = tx.get("context") or {}
    reference = (
        context.get("paymentId")
        or context.get("invoiceId")
        or context.get("transferId")
        or context.get("settlementId")
        or tx["id"]
    )
    message = f"Mollie {label}"
    if payment_info.get("description"):
        message += f" - {payment_info['description']}"
    message += f" ({reference})"
    date_iso = transaction_date(tx).isoformat()
    base = {
        "date": date_iso,
        "message": message[:255],
        "contra_account_name": (payment_info.get("consumer_name") or "Mollie")[:255],
        "contra_account_number": payment_info.get("consumer_iban") or "",
    }

    deductions = _amount_key((tx.get("deductions") or {}).get("value"))
    if deductions != 0:
        gross = (tx.get("initialAmount") or {}).get("value", "0")
        return [
            {**base, "amount": gross, "code": f"mollie-{tx['id']}"},
            {
                "date": date_iso,
                "amount": (tx.get("deductions") or {}).get("value", "0"),
                "message": f"Mollie transactiekosten ({reference})"[:255],
                "code": f"mollie-{tx['id']}-fee",
                "contra_account_name": "Mollie",
                "contra_account_number": "",
            },
        ]
    return [
        {**base, "amount": (tx.get("resultAmount") or {}).get("value", "0"),
         "code": f"mollie-{tx['id']}"},
    ]


def sync_company_mollie(
    config: Config,
    company: Company,
    moneybird: MoneybirdClient,
    state: SyncState,
    dry_run: bool = False,
    rescan_days: int | None = None,
) -> int:
    """Synchroniseert de Mollie-balanstransacties van één bedrijf naar het
    Mollie-financial-account in Moneybird. Geeft het aantal overgezette
    transacties terug."""
    settings = company.mollie
    assert settings is not None
    client = MollieClient(settings.token)
    balance_id = settings.balance_id or client.get_primary_balance()["id"]
    state_key = f"mollie:{balance_id}"

    last = state.mollie_last(company.name, balance_id)
    after_id = None
    since = None
    if rescan_days:
        since = date.today() - timedelta(days=rescan_days)
        logger.info(
            "[%s] mollie %s: rescan, transacties vanaf %s worden opnieuw vergeleken.",
            company.name, balance_id, since.isoformat(),
        )
    elif last:
        after_id = last["last_transaction_id"]
        # Vangnet voor het geval het bekende id niet meer langskomt.
        since = date.fromisoformat(last["last_created_at"][:10]) - timedelta(days=3)
    else:
        stored_since = state.first_sync_since(company.name, state_key)
        if stored_since:
            since = date.fromisoformat(stored_since)
        else:
            since = settings.sync_from or (
                date.today() - timedelta(days=config.initial_sync_days)
            )
            logger.info(
                "[%s] mollie %s: eerste sync, transacties vanaf %s worden opgehaald%s.",
                company.name, balance_id, since.isoformat(),
                " (sync_from uit config)" if settings.sync_from else "",
            )

    transactions = client.fetch_balance_transactions(balance_id, after_id, since)
    transactions = [
        tx for tx in transactions
        if _amount_key((tx.get("resultAmount") or {}).get("value")) != 0
    ]
    if not transactions:
        logger.info("[%s] mollie %s: geen nieuwe transacties.", company.name, balance_id)
        if last is None and not dry_run and not rescan_days:
            state.remember_empty_first_sync(company.name, state_key, since.isoformat())
            state.save()
        return 0

    def payment_info(tx: dict) -> dict | None:
        payment_id = (tx.get("context") or {}).get("paymentId")
        if payment_id and tx.get("type") in ("payment", "capture", "refund",
                                             "chargeback", "chargeback-reversal"):
            return client.get_payment_info(payment_id)
        return None

    candidates = [
        mutation
        for tx in transactions
        for mutation in mollie_transaction_to_mutations(tx, payment_info(tx))
    ]
    existing = moneybird.list_financial_mutations(
        administration_id=company.moneybird_administration_id,
        financial_account_id=settings.moneybird_financial_account_id,
        start=candidates[0]["date"].replace("-", ""),
        end=candidates[-1]["date"].replace("-", ""),
    )
    new_mutations, skipped = filter_new_mutations(candidates, existing)
    if skipped:
        logger.info(
            "[%s] mollie %s: %d transactie(s) overgeslagen die al in Moneybird staan.",
            company.name, balance_id, skipped,
        )
    if not new_mutations:
        logger.info(
            "[%s] mollie %s: alles staat al in Moneybird; niets te doen.",
            company.name, balance_id,
        )
        if not dry_run:
            state.update_mollie(
                company.name, balance_id,
                transactions[-1]["id"], transactions[-1]["createdAt"],
            )
            state.save()
        return 0

    logger.info(
        "[%s] mollie %s: %d nieuwe mutatie(s) uit %d transactie(s) (%s t/m %s).",
        company.name, balance_id, len(new_mutations), len(transactions),
        new_mutations[0]["date"], new_mutations[-1]["date"],
    )

    for chunk_start in range(0, len(new_mutations), MUTATIONS_PER_STATEMENT):
        chunk = new_mutations[chunk_start : chunk_start + MUTATIONS_PER_STATEMENT]
        # code 'mollie-baltr_x' -> 'baltr_x' voor een leesbare referentie
        reference = f"mollie {chunk[0]['code'][7:]} t/m {chunk[-1]['code'][7:]}"

        if dry_run:
            logger.info("  [dry-run] Zou afschrift '%s' aanmaken met %d mutatie(s):",
                        reference, len(chunk))
            for m in chunk:
                logger.info("    %s  %10s  %s", m["date"], m["amount"], m["message"][:60])
            continue

        moneybird.create_financial_statement(
            administration_id=company.moneybird_administration_id,
            financial_account_id=settings.moneybird_financial_account_id,
            reference=reference,
            mutations=chunk,
        )
        logger.info("  Afschrift '%s' aangemaakt (%d mutaties).", reference, len(chunk))

    if not dry_run:
        state.update_mollie(
            company.name, balance_id,
            transactions[-1]["id"], transactions[-1]["createdAt"],
        )
        state.save()
    return len(new_mutations)
