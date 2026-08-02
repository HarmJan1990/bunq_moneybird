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
from .state import SyncState
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
    except ConfigError as exc:
        print(f"Configuratiefout: {exc}", file=sys.stderr)
        return 2
    except (BunqApiError, MoneybirdApiError) as exc:
        print(f"Fout: {exc}", file=sys.stderr)
        return 1
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
