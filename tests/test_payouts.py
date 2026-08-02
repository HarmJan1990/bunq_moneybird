"""Tests voor het inlezen en valideren van uitbetalingsexports."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bunq_moneybird.payouts import (
    PayoutFileError,
    parse_payout_file,
    validate_iban,
)
from bunq_moneybird.state import PayoutState

SAMPLE_CSV = """\
Referentie;Naam begunstigde;IBAN;Bedrag (EUR);Omschrijving
WKB-7K2M-9Q4P;J. Jansen;NL91ABNA0417164300;42.50;Uitbetaling cadeaukaart WKB-7K2M-9Q4P
WKB-A1B2-C3D4;M. de Vries;NL39RABO0300065264;15.00;Uitbetaling cadeaukaart WKB-A1B2-C3D4
"""

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03">
  <CstmrCdtTrfInitn>
    <GrpHdr>
      <MsgId>WKB-20260802181422-AB12CD</MsgId>
      <CreDtTm>2026-08-02T18:14:22</CreDtTm>
      <NbOfTxs>2</NbOfTxs>
      <CtrlSum>57.50</CtrlSum>
      <InitgPty><Nm>WijKopenBonnen B.V.</Nm></InitgPty>
    </GrpHdr>
    <PmtInf>
      <PmtInfId>WKB-20260802181422-AB12CD-1</PmtInfId>
      <PmtMtd>TRF</PmtMtd>
      <NbOfTxs>2</NbOfTxs>
      <CtrlSum>57.50</CtrlSum>
      <ReqdExctnDt>2026-08-02</ReqdExctnDt>
      <Dbtr><Nm>WijKopenBonnen B.V.</Nm></Dbtr>
      <DbtrAcct><Id><IBAN>NL00BANK0123456789</IBAN></Id></DbtrAcct>
      <CdtTrfTxInf>
        <PmtId><EndToEndId>WKB-7K2M-9Q4P</EndToEndId></PmtId>
        <Amt><InstdAmt Ccy="EUR">42.50</InstdAmt></Amt>
        <Cdtr><Nm>J. Jansen</Nm></Cdtr>
        <CdtrAcct><Id><IBAN>NL91ABNA0417164300</IBAN></Id></CdtrAcct>
        <RmtInf><Ustrd>Uitbetaling cadeaukaart WKB-7K2M-9Q4P</Ustrd></RmtInf>
      </CdtTrfTxInf>
      <CdtTrfTxInf>
        <PmtId><EndToEndId>WKB-A1B2-C3D4</EndToEndId></PmtId>
        <Amt><InstdAmt Ccy="EUR">15.00</InstdAmt></Amt>
        <Cdtr><Nm>M. de Vries</Nm></Cdtr>
        <CdtrAcct><Id><IBAN>NL39RABO0300065264</IBAN></Id></CdtrAcct>
        <RmtInf><Ustrd>Uitbetaling cadeaukaart WKB-A1B2-C3D4</Ustrd></RmtInf>
      </CdtTrfTxInf>
    </PmtInf>
  </CstmrCdtTrfInitn>
</Document>
"""


def _write(tmp: str, name: str, content: str) -> Path:
    path = Path(tmp) / name
    path.write_text(content, encoding="utf-8")
    return path


class IbanTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(validate_iban("NL91ABNA0417164300"))
        self.assertTrue(validate_iban("nl91 abna 0417 1643 00"))
        self.assertFalse(validate_iban("NL92ABNA0417164300"))  # fout controlegetal
        self.assertFalse(validate_iban("NL91"))
        self.assertFalse(validate_iban(""))


class CsvTests(unittest.TestCase):
    def test_parse_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = parse_payout_file(_write(tmp, "export.csv", SAMPLE_CSV))
        self.assertEqual(len(batch.payouts), 2)
        self.assertEqual(batch.total, Decimal("57.50"))
        self.assertIsNone(batch.debtor_iban)
        first = batch.payouts[0]
        self.assertEqual(first.reference, "WKB-7K2M-9Q4P")
        self.assertEqual(first.iban, "NL91ABNA0417164300")
        self.assertEqual(first.amount, Decimal("42.50"))
        self.assertEqual(first.description, "Uitbetaling cadeaukaart WKB-7K2M-9Q4P")

    def test_wrong_header_rejected(self):
        broken = SAMPLE_CSV.replace("Referentie", "Ref")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.csv", broken))

    def test_invalid_iban_rejected(self):
        broken = SAMPLE_CSV.replace("NL91ABNA0417164300", "NL92ABNA0417164300")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.csv", broken))

    def test_duplicate_reference_rejected(self):
        broken = SAMPLE_CSV.replace("WKB-A1B2-C3D4", "WKB-7K2M-9Q4P")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.csv", broken))


class XmlTests(unittest.TestCase):
    def test_parse_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = parse_payout_file(_write(tmp, "export.xml", SAMPLE_XML))
        self.assertEqual(len(batch.payouts), 2)
        self.assertEqual(batch.total, Decimal("57.50"))
        self.assertEqual(batch.debtor_iban, "NL00BANK0123456789")
        self.assertEqual(batch.payouts[1].name, "M. de Vries")

    def test_ctrlsum_mismatch_rejected(self):
        broken = SAMPLE_XML.replace("<CtrlSum>57.50</CtrlSum>", "<CtrlSum>99.99</CtrlSum>")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.xml", broken))

    def test_nboftxs_mismatch_rejected(self):
        broken = SAMPLE_XML.replace("<NbOfTxs>2</NbOfTxs>", "<NbOfTxs>3</NbOfTxs>")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.xml", broken))

    def test_non_eur_rejected(self):
        broken = SAMPLE_XML.replace('Ccy="EUR">42.50', 'Ccy="USD">42.50')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PayoutFileError):
                parse_payout_file(_write(tmp, "export.xml", broken))


class PayoutStateTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payouts.json"
            state = PayoutState(path)
            self.assertFalse(state.is_submitted("WKB-7K2M-9Q4P"))
            state.mark_submitted("WKB-7K2M-9Q4P", draft_id=42, source="export.csv")
            state.save()
            reloaded = PayoutState(path)
            self.assertTrue(reloaded.is_submitted("WKB-7K2M-9Q4P"))


if __name__ == "__main__":
    unittest.main()
