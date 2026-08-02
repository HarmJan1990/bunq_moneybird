"""Tests voor de kernlogica (zonder echte API-calls).

Draaien met: python -m unittest discover tests
"""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bunq_moneybird.bunq_client import BunqClient
from bunq_moneybird.config import ConfigError, load_config
from bunq_moneybird.state import SyncState
from bunq_moneybird.sync import filter_new_payments, payment_to_mutation

EXAMPLE_CONFIG = """
defaults:
  initial_sync_days: 14
companies:
  - name: testbedrijf
    bunq:
      api_key_env: BUNQ_KEY_TEST
    moneybird:
      token_env: MB_TOKEN_TEST
      administration_id: "111"
    accounts:
      - iban: "nl00 bunq 0000 0000 00"
        moneybird_financial_account_id: 222
        sync_from: "2026-07-15"
"""

LEGACY_CONFIG = """
moneybird:
  token_env: MB_TOKEN_GLOBAL
companies:
  - name: oud
    bunq:
      api_key_env: BUNQ_KEY_OUD
    moneybird_administration_id: "333"
"""


class ConfigTests(unittest.TestCase):
    def test_load_and_normalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(EXAMPLE_CONFIG)
            config = load_config(path)
        self.assertEqual(config.initial_sync_days, 14)
        company = config.company("testbedrijf")
        self.assertEqual(company.accounts[0].iban, "NL00BUNQ0000000000")
        self.assertEqual(company.accounts[0].moneybird_financial_account_id, "222")
        self.assertEqual(company.moneybird_token_env, "MB_TOKEN_TEST")
        self.assertEqual(company.moneybird_administration_id, "111")
        self.assertEqual(company.accounts[0].sync_from, datetime.date(2026, 7, 15))
        with self.assertRaises(ConfigError):
            config.company("bestaat-niet")

    def test_legacy_shape_with_global_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(LEGACY_CONFIG)
            config = load_config(path)
        company = config.company("oud")
        self.assertEqual(company.moneybird_token_env, "MB_TOKEN_GLOBAL")
        self.assertEqual(company.moneybird_administration_id, "333")

    def test_missing_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(EXAMPLE_CONFIG)
            config = load_config(path)
        os.environ.pop("BUNQ_KEY_TEST", None)
        with self.assertRaises(ConfigError):
            _ = config.company("testbedrijf").bunq_api_key


class StateTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = SyncState(path)
            self.assertIsNone(state.last_payment_id("a", "NL00"))
            state.update("a", "NL00", 42)
            state.save()
            reloaded = SyncState(path)
            self.assertEqual(reloaded.last_payment_id("a", "NL00"), 42)


class MutationTests(unittest.TestCase):
    def test_payment_to_mutation(self):
        payment = {
            "id": 7,
            "created": "2026-07-30 14:03:12.123456",
            "amount": {"value": "-12.34", "currency": "EUR"},
            "description": "Factuur 2026-001",
            "counterparty_alias": {"iban": "NL99RABO0123456789", "display_name": "Klant BV"},
        }
        mutation = payment_to_mutation(payment)
        self.assertEqual(
            mutation,
            {
                "date": "2026-07-30",
                "amount": "-12.34",
                "message": "Factuur 2026-001",
                "contra_account_name": "Klant BV",
                "contra_account_number": "NL99RABO0123456789",
            },
        )

    def test_empty_description_falls_back_to_counterparty(self):
        payment = {
            "id": 8,
            "created": "2026-07-30 00:00:00.000000",
            "amount": {"value": "5.00"},
            "description": "  ",
            "counterparty_alias": {"display_name": "Iemand"},
        }
        self.assertEqual(payment_to_mutation(payment)["message"], "Iemand")


def _bunq_payment(pid, day, value):
    return {
        "id": pid,
        "created": f"2026-07-{day:02d} 10:00:00.000000",
        "amount": {"value": value, "currency": "EUR"},
        "description": f"betaling {pid}",
        "counterparty_alias": {},
    }


class DedupTests(unittest.TestCase):
    def test_skips_payments_already_in_moneybird(self):
        payments = [
            _bunq_payment(1, 10, "-10.00"),
            _bunq_payment(2, 10, "-10.00"),
            _bunq_payment(3, 11, "25.50"),
            _bunq_payment(4, 12, "-3.00"),
        ]
        existing = [
            # Eén van de twee tientjes op de 10e staat er al; formattering
            # met extra decimalen moet ook matchen.
            {"date": "2026-07-10", "amount": "-10.0"},
            {"date": "2026-07-11", "amount": "25.50"},
        ]
        new_payments, skipped = filter_new_payments(payments, existing)
        self.assertEqual(skipped, 2)
        self.assertEqual([p["id"] for p in new_payments], [2, 4])

    def test_nothing_existing_keeps_everything(self):
        payments = [_bunq_payment(1, 10, "-10.00")]
        new_payments, skipped = filter_new_payments(payments, [])
        self.assertEqual((len(new_payments), skipped), (1, 0))

    def test_gap_left_by_old_link_is_imported(self):
        # De oude koppeling miste betaling 2; alleen die moet alsnog mee.
        payments = [
            _bunq_payment(1, 10, "-10.00"),
            _bunq_payment(2, 10, "-99.99"),
            _bunq_payment(3, 11, "5.00"),
        ]
        existing = [
            {"date": "2026-07-10", "amount": "-10.00"},
            {"date": "2026-07-11", "amount": "5.00"},
        ]
        new_payments, skipped = filter_new_payments(payments, existing)
        self.assertEqual(skipped, 2)
        self.assertEqual([p["id"] for p in new_payments], [2])


def _page(payments, older_url=None):
    return {
        "Response": [{"Payment": p} for p in payments],
        "Pagination": {"older_url": older_url},
    }


class PaginationTests(unittest.TestCase):
    """fetch_payments moet stoppen bij de laatst gesynchroniseerde id en
    oudste-eerst teruggeven."""

    def _client(self, tmp):
        client = BunqClient.__new__(BunqClient)
        client._user_id = 1
        client._session_token = "tok"
        client._ensure_session = lambda: None
        return client

    def test_stops_at_known_id_across_pages(self):
        def payment(pid):
            return {"id": pid, "created": f"2026-07-{pid:02d} 10:00:00.000000"}

        pages = {
            "/v1/user/1/monetary-account/9/payment?count=200": _page(
                [payment(30), payment(29)], older_url="/older-1"
            ),
            "/older-1": _page([payment(28), payment(27)]),
        }
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp)
            client._request = lambda method, path, payload=None: pages[path]
            result = client.fetch_payments(9, after_payment_id=28)
        self.assertEqual([p["id"] for p in result], [29, 30])


if __name__ == "__main__":
    unittest.main()
