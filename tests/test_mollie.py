"""Tests voor de Mollie-integratie (zonder echte API-calls).

De voorbeeldtransacties volgen de structuur zoals live geverifieerd tegen
de Mollie Balances API (baltr_-ids, getekende resultAmount, createdAt met
tijdzone).
"""

import tempfile
import unittest
from pathlib import Path

from bunq_moneybird.config import load_config
from bunq_moneybird.mollie_client import MollieClient, transaction_date
from bunq_moneybird.state import SyncState
from bunq_moneybird.sync import filter_new_mutations, mollie_transaction_to_mutations


def _tx(tx_id, tx_type, value, created="2026-09-15T00:57:05+00:00", context=None,
        initial=None, deductions=None):
    tx = {
        "resource": "balance-transaction",
        "id": tx_id,
        "type": tx_type,
        "resultAmount": {"currency": "EUR", "value": value},
        "createdAt": created,
        "context": context or {},
    }
    if initial is not None:
        tx["initialAmount"] = {"currency": "EUR", "value": initial}
    if deductions is not None:
        tx["deductions"] = {"currency": "EUR", "value": deductions}
    return tx


class MutationMappingTests(unittest.TestCase):
    def test_payment_with_fees_is_split_into_gross_and_fee(self):
        # Zoals live geverifieerd: resultAmount 72.06 = initialAmount 72.37
        # + deductions -0.31. Bruto matcht de factuur; samen kloppend saldo.
        tx = _tx("baltr_abc", "payment", "72.06", initial="72.37", deductions="-0.31",
                 context={"paymentId": "tr_xyz"})
        info = {"description": "Factuur 2026-1424", "consumer_name": "Exclusief Vastgoedbeheer",
                "consumer_iban": "NL08RABO0331931001"}
        mutations = mollie_transaction_to_mutations(tx, info)
        self.assertEqual(
            mutations,
            [
                {
                    "date": "2026-09-15",
                    "amount": "72.37",
                    "message": "Mollie betaling - Factuur 2026-1424 (tr_xyz)",
                    "code": "mollie-baltr_abc",
                    "contra_account_name": "Exclusief Vastgoedbeheer",
                    "contra_account_number": "NL08RABO0331931001",
                },
                {
                    "date": "2026-09-15",
                    "amount": "-0.31",
                    "message": "Mollie transactiekosten (tr_xyz)",
                    "code": "mollie-baltr_abc-fee",
                    "contra_account_name": "Mollie",
                    "contra_account_number": "",
                },
            ],
        )

    def test_payment_without_fees_uses_result_amount(self):
        tx = _tx("baltr_abc", "payment", "72.06", context={"paymentId": "tr_xyz"})
        mutations = mollie_transaction_to_mutations(tx)
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0]["amount"], "72.06")
        self.assertEqual(mutations[0]["code"], "mollie-baltr_abc")
        self.assertEqual(mutations[0]["contra_account_name"], "Mollie")

    def test_invoice_compensation_and_fee_prepayment_labels(self):
        compensation = mollie_transaction_to_mutations(
            _tx("baltr_i", "invoice-compensation", "0.73",
                context={"invoiceId": "inv_xyz"})
        )[0]
        self.assertEqual(compensation["message"], "Mollie factuurverrekening (inv_xyz)")
        withheld = mollie_transaction_to_mutations(
            _tx("baltr_w", "fee-prepayment", "-61.23")
        )[0]
        self.assertEqual(withheld["message"], "Mollie ingehouden kosten (baltr_w)")
        self.assertEqual(withheld["amount"], "-61.23")

    def test_chargeback_and_transfer_labels(self):
        chargeback = mollie_transaction_to_mutations(
            _tx("baltr_c", "chargeback", "-42.50", context={"paymentId": "tr_1"})
        )[0]
        self.assertEqual(chargeback["message"], "Mollie chargeback (tr_1)")
        self.assertEqual(chargeback["amount"], "-42.50")
        transfer = mollie_transaction_to_mutations(
            _tx("baltr_t", "outgoing-transfer", "-7556.36")
        )[0]
        self.assertEqual(
            transfer["message"], "Mollie uitbetaling naar bankrekening (baltr_t)"
        )

    def test_unknown_type_falls_back_to_raw_label(self):
        mutation = mollie_transaction_to_mutations(
            _tx("baltr_x", "some-new-type", "1.00")
        )[0]
        self.assertEqual(mutation["message"], "Mollie some-new-type (baltr_x)")

    def test_transaction_date_uses_timezone_aware_created_at(self):
        self.assertEqual(
            transaction_date(_tx("x", "payment", "1.00",
                                 created="2026-09-23T23:09:28+00:00")).isoformat(),
            "2026-09-23",
        )


class MollieDedupTests(unittest.TestCase):
    def test_code_match_is_exact(self):
        candidates = (
            mollie_transaction_to_mutations(_tx("baltr_1", "payment", "10.00"))
            + mollie_transaction_to_mutations(_tx("baltr_2", "payment", "10.00"))
        )
        existing = [{"date": "2026-09-15", "amount": "10.00", "code": "mollie-baltr_1"}]
        new, skipped = filter_new_mutations(candidates, existing)
        self.assertEqual(skipped, 1)
        self.assertEqual([m["code"] for m in new], ["mollie-baltr_2"])

    def test_old_mt940_mutations_match_on_date_and_amount(self):
        # Mutaties uit een oude MT940-upload hebben geen code; die matchen
        # heuristisch zodat de overgangsmaand niet dubbel wordt geïmporteerd.
        candidates = (
            mollie_transaction_to_mutations(_tx("baltr_1", "payment", "72.06"))
            + mollie_transaction_to_mutations(_tx("baltr_2", "payment", "65.54"))
        )
        existing = [{"date": "2026-09-15", "amount": "72.06", "code": ""}]
        new, skipped = filter_new_mutations(candidates, existing)
        self.assertEqual(skipped, 1)
        self.assertEqual([m["code"] for m in new], ["mollie-baltr_2"])


class PaginationTests(unittest.TestCase):
    def test_stops_at_known_id_across_pages(self):
        pages = {
            "/balances/bal_1/transactions?limit=250": {
                "_embedded": {"balance_transactions": [
                    _tx("baltr_d", "payment", "4.00", created="2026-09-20T10:00:00+00:00"),
                    _tx("baltr_c", "payment", "3.00", created="2026-09-19T10:00:00+00:00"),
                ]},
                "_links": {"next": {"href": "https://api.mollie.com/v2/next-page"}},
            },
            "https://api.mollie.com/v2/next-page": {
                "_embedded": {"balance_transactions": [
                    _tx("baltr_b", "payment", "2.00", created="2026-09-18T10:00:00+00:00"),
                    _tx("baltr_a", "payment", "1.00", created="2026-09-17T10:00:00+00:00"),
                ]},
                "_links": {"next": None},
            },
        }
        client = MollieClient.__new__(MollieClient)
        client._request = lambda path_or_url: pages[path_or_url]
        result = client.fetch_balance_transactions("bal_1", after_transaction_id="baltr_b")
        self.assertEqual([tx["id"] for tx in result], ["baltr_c", "baltr_d"])

    def test_since_date_limits_first_sync(self):
        from datetime import date

        pages = {
            "/balances/bal_1/transactions?limit=250": {
                "_embedded": {"balance_transactions": [
                    _tx("baltr_b", "payment", "2.00", created="2026-09-18T10:00:00+00:00"),
                    _tx("baltr_a", "payment", "1.00", created="2026-08-01T10:00:00+00:00"),
                ]},
                "_links": {"next": None},
            },
        }
        client = MollieClient.__new__(MollieClient)
        client._request = lambda path_or_url: pages[path_or_url]
        result = client.fetch_balance_transactions("bal_1", since=date(2026, 9, 1))
        self.assertEqual([tx["id"] for tx in result], ["baltr_b"])


class ConfigTests(unittest.TestCase):
    def test_mollie_block_parsed(self):
        raw = """
companies:
  - name: shop
    bunq:
      api_key_env: BUNQ_KEY_SHOP
    moneybird:
      token_env: MB_TOKEN_SHOP
      administration_id: "111"
    mollie:
      token_env: MOLLIE_TOKEN_SHOP
      moneybird_financial_account_id: 555
      sync_from: "2026-09-01"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(raw)
            config = load_config(path)
        mollie = config.company("shop").mollie
        self.assertIsNotNone(mollie)
        self.assertEqual(mollie.token_env, "MOLLIE_TOKEN_SHOP")
        self.assertEqual(mollie.moneybird_financial_account_id, "555")
        self.assertIsNone(mollie.balance_id)
        self.assertEqual(mollie.sync_from.isoformat(), "2026-09-01")

    def test_mollie_only_company_needs_no_bunq_block(self):
        raw = """
companies:
  - name: webshop
    moneybird:
      token_env: MB_TOKEN_WEBSHOP
      administration_id: "222"
    mollie:
      token_env: MOLLIE_TOKEN_WEBSHOP
      moneybird_financial_account_id: 777
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(raw)
            config = load_config(path)
        company = config.company("webshop")
        self.assertEqual(company.accounts, [])
        self.assertIsNotNone(company.mollie)
        from bunq_moneybird.config import ConfigError

        with self.assertRaises(ConfigError):
            _ = company.bunq_api_key

    def test_bunq_accounts_still_require_api_key_env(self):
        raw = """
companies:
  - name: kapot
    moneybird:
      token_env: MB_TOKEN
      administration_id: "222"
    accounts:
      - iban: NL91ABNA0417164300
        moneybird_financial_account_id: 1
"""
        from bunq_moneybird.config import ConfigError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(raw)
            with self.assertRaises(ConfigError):
                load_config(path)


class MollieStateTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = SyncState(path)
            self.assertIsNone(state.mollie_last("shop", "bal_1"))
            state.update_mollie("shop", "bal_1", "baltr_x", "2026-09-23T23:09:28+00:00")
            state.save()
            reloaded = SyncState(path)
            last = reloaded.mollie_last("shop", "bal_1")
            self.assertEqual(last["last_transaction_id"], "baltr_x")
            self.assertEqual(last["last_created_at"], "2026-09-23T23:09:28+00:00")
            # De bunq-state van hetzelfde bedrijf blijft er los van.
            self.assertIsNone(reloaded.last_payment_id("shop", "NL00"))


if __name__ == "__main__":
    unittest.main()
