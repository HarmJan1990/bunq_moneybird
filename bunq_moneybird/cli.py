"""Command-line interface.

    bunq-moneybird sync [--company NAAM] [--dry-run]
    bunq-moneybird list-bunq --company NAAM
    bunq-moneybird list-moneybird
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .bunq_client import BunqApiError, BunqClient
from .config import ConfigError, load_config
from .moneybird_client import MoneybirdApiError, MoneybirdClient
from .payouts import PayoutFileError, parse_payout_file
from .state import PayoutState, SyncState
from .sync import sync_company


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bunq-moneybird",
        description="Synchroniseer bunq-transacties naar Moneybird als bankafschriften.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"),
        help="Pad naar config.yaml (standaard: ./config.yaml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_sync = subparsers.add_parser("sync", help="Nieuwe transacties naar Moneybird sturen")
    p_sync.add_argument("--company", help="Alleen dit bedrijf synchroniseren")
    p_sync.add_argument(
        "--dry-run", action="store_true",
        help="Laat zien wat er zou gebeuren zonder iets naar Moneybird te sturen",
    )

    p_bunq = subparsers.add_parser(
        "list-bunq", help="Toon bunq-rekeningen (IBAN's) van een bedrijf"
    )
    p_bunq.add_argument("--company", required=True)

    subparsers.add_parser(
        "list-moneybird",
        help="Toon Moneybird-administraties en hun financial accounts (voor de mapping)",
    )

    p_pay = subparsers.add_parser(
        "pay",
        help="Zet uitbetalingen uit een CSV- of pain.001-export klaar als "
        "concept-betaling in bunq (goedkeuren doe je in de bunq-app)",
    )
    p_pay.add_argument("file", type=Path, help="Pad naar het .csv- of .xml-exportbestand")
    p_pay.add_argument("--company", required=True, help="Bedrijf uit config.yaml")
    p_pay.add_argument(
        "--iban",
        help="IBAN van de bunq-rekening waarvan betaald wordt (verplicht bij CSV; "
        "bij XML wordt standaard de debiteur-IBAN uit het bestand gebruikt)",
    )
    p_pay.add_argument(
        "--dry-run", action="store_true",
        help="Alleen inlezen, valideren en tonen; niets naar bunq sturen",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    # Ruis van urllib3 dempen
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        config = load_config(args.config)
        if args.command == "sync":
            return _cmd_sync(config, args)
        if args.command == "list-bunq":
            return _cmd_list_bunq(config, args)
        if args.command == "list-moneybird":
            return _cmd_list_moneybird(config)
        if args.command == "pay":
            return _cmd_pay(config, args)
    except (ConfigError, PayoutFileError) as exc:
        print(f"Configuratiefout: {exc}", file=sys.stderr)
        return 2
    except (BunqApiError, MoneybirdApiError) as exc:
        print(f"Fout: {exc}", file=sys.stderr)
        return 1
    return 0


# Aantal uitbetalingen per draft payment; grote exports worden gesplitst.
PAYOUTS_PER_DRAFT = 100


def _cmd_pay(config, args) -> int:
    company = config.company(args.company)
    batch = parse_payout_file(args.file)

    debit_iban = (args.iban or "").replace(" ", "").upper() or batch.debtor_iban
    if not debit_iban:
        print(
            "Fout: geef met --iban aan van welke bunq-rekening betaald moet worden "
            "(de CSV-export bevat geen debiteur-IBAN).",
            file=sys.stderr,
        )
        return 2
    if batch.debtor_iban and debit_iban != batch.debtor_iban:
        print(
            f"Fout: --iban {debit_iban} wijkt af van de debiteur-IBAN in het bestand "
            f"({batch.debtor_iban}). Kies bewust met --iban of laat hem weg.",
            file=sys.stderr,
        )
        return 2

    payout_state = PayoutState(config.state_file.parent / "payouts.json")
    already = [p for p in batch.payouts if payout_state.is_submitted(p.reference)]
    todo = [p for p in batch.payouts if not payout_state.is_submitted(p.reference)]

    print(f"Bestand: {batch.source} — {len(batch.payouts)} uitbetaling(en), "
          f"totaal € {batch.total}")
    if already:
        print(f"Al eerder ingediend (overgeslagen): {len(already)}")
        for payout in already:
            print(f"  - {payout.reference}  € {payout.amount}  {payout.name}")
    if not todo:
        print("Niets te doen: alle referenties zijn al eerder ingediend.")
        return 0

    total_todo = sum(p.amount for p in todo)
    print(f"\nKlaar te zetten vanaf {debit_iban}: {len(todo)} uitbetaling(en), "
          f"totaal € {total_todo}")
    for payout in todo:
        print(f"  {payout.reference}  {payout.iban:<22} {payout.amount:>10}  {payout.name}")

    if args.dry_run:
        print("\n[dry-run] Er is niets naar bunq gestuurd.")
        return 0

    bunq = BunqClient(
        api_url=config.bunq_api_url,
        api_key=company.bunq_api_key,
        context_file=company.bunq_context_file,
        wildcard_ip=company.bunq_wildcard_ip,
    )
    account = bunq.find_account_by_iban(debit_iban)

    for chunk_start in range(0, len(todo), PAYOUTS_PER_DRAFT):
        chunk = todo[chunk_start : chunk_start + PAYOUTS_PER_DRAFT]
        entries = [
            {
                "amount": {"value": str(payout.amount), "currency": "EUR"},
                "counterparty_alias": {
                    "type": "IBAN",
                    "value": payout.iban,
                    "name": payout.name,
                },
                "description": payout.description[:140],
            }
            for payout in chunk
        ]
        draft_id = bunq.create_draft_payment(account["id"], entries)
        for payout in chunk:
            payout_state.mark_submitted(payout.reference, draft_id, batch.source)
        payout_state.save()
        print(f"\nConcept-betaling {draft_id} aangemaakt met {len(chunk)} uitbetaling(en).")

    print(
        "\nOpen de bunq-app om de concept-betaling(en) goed te keuren — "
        "pas daarna wordt er daadwerkelijk uitbetaald."
    )
    return 0


def _cmd_sync(config, args) -> int:
    companies = [config.company(args.company)] if args.company else config.companies
    state = SyncState(config.state_file)

    total = 0
    failures = 0
    for company in companies:
        try:
            moneybird = MoneybirdClient(company.moneybird_token)
            total += sync_company(config, company, moneybird, state, dry_run=args.dry_run)
        except (BunqApiError, MoneybirdApiError, ConfigError) as exc:
            failures += 1
            print(f"[{company.name}] Fout: {exc}", file=sys.stderr)

    label = "zouden worden overgezet (dry-run)" if args.dry_run else "overgezet"
    print(f"\nKlaar: {total} transactie(s) {label}.")
    return 1 if failures else 0


def _cmd_list_bunq(config, args) -> int:
    company = config.company(args.company)
    client = BunqClient(
        api_url=config.bunq_api_url,
        api_key=company.bunq_api_key,
        context_file=company.bunq_context_file,
        wildcard_ip=company.bunq_wildcard_ip,
    )
    print(f"bunq-rekeningen voor '{company.name}':\n")
    for account in client.list_monetary_accounts():
        print(
            f"  {account['iban'] or '(geen IBAN)':<22} "
            f"{account['description'] or '':<30} "
            f"{account['balance'] or '':>12} {account['currency'] or ''}  "
            f"[{account['status']}]"
        )
    return 0


def _cmd_list_moneybird(config) -> int:
    # Moneybird-tokens zijn per administratie; we tonen per bedrijf wat
    # zijn token kan zien.
    exit_code = 0
    for company in config.companies:
        print(f"\n=== Bedrijf: {company.name} ===")
        try:
            client = MoneybirdClient(company.moneybird_token)
            for admin in client.list_administrations():
                print(f"Administratie: {admin['name']}  (id: {admin['id']})")
                accounts = client.list_financial_accounts(str(admin["id"]))
                if not accounts:
                    print("  (geen financial accounts)")
                for account in accounts:
                    print(
                        f"  id: {account['id']:<20} {account.get('identifier') or '':<22} "
                        f"{account.get('name') or ''}"
                    )
        except (ConfigError, MoneybirdApiError) as exc:
            print(f"  Overgeslagen: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
