"""Inlezen en valideren van SEPA-uitbetalingsexports (WijKopenBonnen).

Ondersteunt de CSV-export (puntkomma-gescheiden) en de pain.001.001.03
XML-export. Beide leveren dezelfde lijst uitbetalingen op; de XML bevat
daarnaast batchgegevens (debiteur-IBAN, controlesom) die tegen de inhoud
worden gevalideerd.
"""

from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

PAIN_NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"

CSV_HEADERS = ["Referentie", "Naam begunstigde", "IBAN", "Bedrag (EUR)", "Omschrijving"]


class PayoutFileError(Exception):
    pass


@dataclass
class Payout:
    reference: str
    name: str
    iban: str
    amount: Decimal
    description: str


@dataclass
class PayoutBatch:
    payouts: list[Payout]
    debtor_iban: str | None = None  # alleen aanwezig in pain.001
    source: str = ""

    @property
    def total(self) -> Decimal:
        return sum((p.amount for p in self.payouts), Decimal("0"))


def normalize_iban(iban: str) -> str:
    return (iban or "").replace(" ", "").upper()


def validate_iban(iban: str) -> bool:
    """ISO 13616 mod-97-controle."""
    iban = normalize_iban(iban)
    if len(iban) < 15 or len(iban) > 34 or not iban[:2].isalpha() or not iban.isalnum():
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def _parse_amount(raw: str, context: str) -> Decimal:
    try:
        amount = Decimal(str(raw).strip())
    except InvalidOperation:
        raise PayoutFileError(f"{context}: ongeldig bedrag '{raw}'.")
    if amount <= 0:
        raise PayoutFileError(f"{context}: bedrag moet positief zijn, kreeg {amount}.")
    if -amount.as_tuple().exponent > 2:
        raise PayoutFileError(f"{context}: bedrag {amount} heeft meer dan 2 decimalen.")
    return amount.quantize(Decimal("0.01"))


def _validate_payout(payout: Payout) -> None:
    context = f"Uitbetaling {payout.reference or '(zonder referentie)'}"
    if not payout.reference:
        raise PayoutFileError("Uitbetaling zonder referentie gevonden.")
    if not payout.name:
        raise PayoutFileError(f"{context}: naam begunstigde ontbreekt.")
    if not validate_iban(payout.iban):
        raise PayoutFileError(f"{context}: IBAN '{payout.iban}' is ongeldig.")


def parse_csv(path: Path) -> PayoutBatch:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=";", quotechar='"')
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise PayoutFileError(f"{path.name}: leeg bestand.")
    header = [cell.strip() for cell in rows[0]]
    if header != CSV_HEADERS:
        raise PayoutFileError(
            f"{path.name}: onverwachte kolommen {header}; verwacht {CSV_HEADERS}."
        )
    payouts = []
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) != len(CSV_HEADERS):
            raise PayoutFileError(
                f"{path.name} regel {line_no}: {len(row)} kolommen, "
                f"verwacht {len(CSV_HEADERS)}."
            )
        reference, name, iban, amount_raw, description = (cell.strip() for cell in row)
        payout = Payout(
            reference=reference,
            name=name,
            iban=normalize_iban(iban),
            amount=_parse_amount(amount_raw, f"{path.name} regel {line_no}"),
            description=description or f"Uitbetaling cadeaukaart {reference}",
        )
        _validate_payout(payout)
        payouts.append(payout)
    if not payouts:
        raise PayoutFileError(f"{path.name}: geen uitbetalingen gevonden.")
    _check_duplicate_references(payouts, path.name)
    return PayoutBatch(payouts=payouts, source=path.name)


def parse_pain001(path: Path) -> PayoutBatch:
    ns = {"p": PAIN_NS}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise PayoutFileError(f"{path.name}: geen geldige XML ({exc}).")
    if root.tag != f"{{{PAIN_NS}}}Document":
        raise PayoutFileError(
            f"{path.name}: geen pain.001.001.03-document (root is {root.tag})."
        )
    grp_hdr = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", ns)
    pmt_infs = root.findall("p:CstmrCdtTrfInitn/p:PmtInf", ns)
    if grp_hdr is None or not pmt_infs:
        raise PayoutFileError(f"{path.name}: GrpHdr of PmtInf ontbreekt.")

    def text(parent, xpath):
        el = parent.find(xpath, ns)
        return el.text.strip() if el is not None and el.text else ""

    payouts = []
    debtor_iban = None
    for pmt_inf in pmt_infs:
        debtor_iban = normalize_iban(text(pmt_inf, "p:DbtrAcct/p:Id/p:IBAN")) or debtor_iban
        for tx in pmt_inf.findall("p:CdtTrfTxInf", ns):
            reference = text(tx, "p:PmtId/p:EndToEndId")
            amount_el = tx.find("p:Amt/p:InstdAmt", ns)
            if amount_el is None:
                raise PayoutFileError(f"{path.name}: InstdAmt ontbreekt bij {reference}.")
            currency = amount_el.get("Ccy", "EUR")
            if currency != "EUR":
                raise PayoutFileError(
                    f"{path.name}: {reference} heeft valuta {currency}; alleen EUR wordt "
                    "ondersteund."
                )
            payout = Payout(
                reference=reference,
                name=text(tx, "p:Cdtr/p:Nm"),
                iban=normalize_iban(text(tx, "p:CdtrAcct/p:Id/p:IBAN")),
                amount=_parse_amount(amount_el.text, f"{path.name} {reference}"),
                description=text(tx, "p:RmtInf/p:Ustrd")
                or f"Uitbetaling cadeaukaart {reference}",
            )
            _validate_payout(payout)
            payouts.append(payout)

    if not payouts:
        raise PayoutFileError(f"{path.name}: geen transacties gevonden.")
    _check_duplicate_references(payouts, path.name)

    batch = PayoutBatch(payouts=payouts, debtor_iban=debtor_iban, source=path.name)

    # Controlesom en aantal uit de GrpHdr moeten kloppen met de inhoud.
    declared_count = text(grp_hdr, "p:NbOfTxs")
    if declared_count and int(declared_count) != len(payouts):
        raise PayoutFileError(
            f"{path.name}: NbOfTxs is {declared_count}, maar het bestand bevat "
            f"{len(payouts)} transacties."
        )
    declared_sum = text(grp_hdr, "p:CtrlSum")
    if declared_sum and Decimal(declared_sum) != batch.total:
        raise PayoutFileError(
            f"{path.name}: CtrlSum is {declared_sum}, maar de transacties tellen op "
            f"tot {batch.total}."
        )
    return batch


def _check_duplicate_references(payouts: list[Payout], filename: str) -> None:
    seen: set[str] = set()
    for payout in payouts:
        if payout.reference in seen:
            raise PayoutFileError(
                f"{filename}: referentie {payout.reference} komt meerdere keren voor."
            )
        seen.add(payout.reference)


def parse_payout_file(path: Path) -> PayoutBatch:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".xml":
        return parse_pain001(path)
    raise PayoutFileError(f"Onbekend bestandstype '{suffix}'; verwacht .csv of .xml.")
