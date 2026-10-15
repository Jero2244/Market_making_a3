"""Account imports with mixed instruments, signed positions and no credentials."""
import copy
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from market_making.contracts import ggal_expiry
from market_making.ppi.client import PPIError
from market_making.ppi.remarkets import Client, Credentials
from market_making.scenarios.account import AccountDataError, normalize_account, select_account
from market_making.scenarios.calculation import Position, calculate


def fixture():
    return json.loads((Path(__file__).parent / "fixtures" / "remarkets_account_mixed.json").read_text())


def normalize(data):
    return normalize_account(data["account"], data["positions"], data["details"], data["report"], data["instruments"])


class AccountNormalizationTests(unittest.TestCase):
    def test_mixed_portfolio_keeps_only_ggal_futures_with_signed_contracts(self):
        data = fixture()
        before = copy.deepcopy(data)
        result = normalize(data)
        self.assertEqual([p.symbol for p in result.positions], ["GGAL/OCT26", "GGAL/DIC26", "GGAL/JUN27"])
        self.assertEqual([p.contracts for p in result.positions], [3, -4, 2])
        self.assertEqual([p.entry_price for p in result.positions], [6000, 6400, 6800])
        self.assertEqual([p.current_price for p in result.positions], [6125, 6360, 6900])
        self.assertEqual(result.positions[2].maturity, date(2027, 6, 30))
        self.assertEqual(result.ignored_instruments, 3)
        self.assertEqual(result.current_cash, 158000000)
        self.assertEqual(result.available, 155500000)
        self.assertEqual(result.margin, 2000000)
        self.assertEqual(result.calculated_at.hour, 11)
        self.assertEqual(data, before)
        pnl = calculate(6000, 5, [Position(p.expiry, p.contracts, p.entry_price, p.current_price, p.maturity, p.symbol)
                                 for p in result.positions], date(2026, 10, 14), assumed_tna=0)
        self.assertEqual(pnl.total_from_today, 3 * 100 * 175 - 4 * 100 * -60 + 2 * 100 * -600)

    def test_risk_report_uses_internal_references_from_position_summary(self):
        data = fixture()
        futures = data["details"]["detailedPosition"]["report"]["FUTURE"]
        for row, reference in zip(data["positions"]["positions"][:3],
                                  ("GGAL102026", "GGAL122026", "GGAL062027")):
            row["instrument"] = {"symbolReference": reference}
            detail = futures.pop(row["symbol"])
            detail["detailedPositions"][0]["symbolReference"] = reference
            futures[reference] = detail
        result = normalize(data)
        self.assertEqual([p.symbol for p in result.positions], ["GGAL/OCT26", "GGAL/DIC26", "GGAL/JUN27"])
        self.assertEqual([p.contracts for p in result.positions], [3, -4, 2])
        self.assertEqual([p.current_price for p in result.positions], [6125, 6360, 6900])

    def test_conflicting_and_duplicate_risk_references_reject_import(self):
        cases = []
        data = fixture()
        data["positions"]["positions"][0]["instrument"] = {"symbolReference": "GGAL/DIC26"}
        cases.append(data)
        data = fixture()
        for row in data["positions"]["positions"][:2]:
            row["instrument"] = {"symbolReference": "GGAL102026"}
        cases.append(data)
        data = fixture()
        data["positions"]["positions"][0]["instrument"] = {"symbolReference": "GGAL102026"}
        futures = data["details"]["detailedPosition"]["report"]["FUTURE"]
        futures["GGAL102026"] = copy.deepcopy(futures["GGAL/OCT26"])
        cases.append(data)
        data = fixture()
        data["details"]["detailedPosition"]["report"]["FUTURE"]["GGAL/OCT26"]["detailedPositions"][0]["symbolReference"] = "GGAL/DIC26"
        cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(AccountDataError):
                normalize(data)

    def test_empty_account_and_flat_ggal_are_valid(self):
        data = fixture()
        data["positions"]["positions"] = []
        data["details"]["detailedPosition"]["report"] = {}
        result = normalize(data)
        self.assertEqual(result.positions, ())
        self.assertEqual(result.available, 155500000)
        data["positions"]["positions"] = [{"symbol": "GGAL/OCT26", "buySize": 2, "sellSize": 2}]
        self.assertEqual(normalize(data).positions, ())

    def test_exact_future_symbols_exclude_options_stocks_and_bad_months(self):
        for symbol in ("GGAL", "GGAL/OCT26 6000 C", "GGAL/OCT26 ", "GGAL/XXX26", "DLR/OCT26", None):
            self.assertIsNone(ggal_expiry(symbol))
        self.assertEqual(ggal_expiry("GGAL/ENE27"), "2027-01")

    def test_missing_prices_are_never_replaced_with_zero_or_another_instrument(self):
        data = fixture()
        data["positions"]["positions"][0]["buyPrice"] = 0
        row = data["details"]["detailedPosition"]["report"]["FUTURE"]["GGAL/OCT26"]["detailedPositions"][0]
        row["marketPrice"] = None
        result = normalize(data)
        self.assertIsNone(result.positions[0].entry_price)
        self.assertIsNone(result.positions[0].current_price)
        data["positions"]["positions"][0]["originalBuyPrice"] = 5900
        self.assertEqual(normalize(data).positions[0].entry_price, 5900)

    def test_account_errors_mismatched_quantities_and_contract_units_reject_import(self):
        cases = []
        for key, value in (("accountName", "OTHER"), ("hasError", True)):
            data = fixture()
            data["report"]["accountData"][key] = value
            cases.append(data)
        for key, value in (("contractMultiplier", 1), ("currency", "USD"), ("cficode", "OCAFXS"),
                           ("maturityDate", "20261130"), ("priceConvertionFactor", 100)):
            data = fixture()
            data["instruments"]["GGAL/OCT26"]["instrument"][key] = value
            cases.append(data)
        for key, value in (("buySize", 1.5), ("sellSize", -1), ("buySize", True), ("buySize", float("nan"))):
            data = fixture()
            data["positions"]["positions"][0][key] = value
            cases.append(data)
        data = fixture()
        data["details"]["detailedPosition"]["report"]["FUTURE"]["GGAL/OCT26"]["detailedPositions"][0]["totalCurrentSize"] = 99
        cases.append(data)
        data = fixture()
        data["positions"]["positions"].append(copy.deepcopy(data["positions"]["positions"][0]))
        cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(AccountDataError):
                normalize(data)

    def test_new_login_auto_selects_its_sole_account_instead_of_old_account_setting(self):
        data = fixture()
        self.assertEqual(select_account(data["accounts"], "OLD_ACCOUNT"), "REMTEST")
        data["accounts"]["accounts"].append({"name": "REMSECOND", "status": True})
        self.assertEqual(select_account(data["accounts"], "REMSECOND"), "REMSECOND")
        with self.assertRaisesRegex(AccountDataError, "PRIMARY_ACCOUNT"):
            select_account(data["accounts"])


class AccountTransportTests(unittest.TestCase):
    def test_account_reads_use_fixed_simulated_host_and_token_only(self):
        session = Mock()
        payloads = [{"status": "OK", "accounts": []}, {"status": "OK", "positions": []},
                    {"status": "OK", "detailedPosition": {}}, {"status": "OK", "accountData": {}},
                    fixture()["instruments"]["GGAL/OCT26"]]
        responses = []
        for payload in [None] + payloads:
            response = Mock(status_code=200, headers={"X-Auth-Token": "private-token"} if payload is None else {})
            response.iter_content.return_value = [json.dumps(payload).encode()]
            responses.append(response)
        session.request.side_effect = responses
        client = Client(Credentials({"PRIMARY_USER": "private-user", "PRIMARY_PASSWORD": "private-password"}),
                        live=True, max_requests=32, session=session)
        self.addCleanup(client.close)
        client.login()
        client.accounts()
        for kind in ("positions", "details", "report"):
            client.account_read(kind, "REMTEST")
        client.ggal_instrument("GGAL/OCT26")
        paths = [call.args[1] for call in session.request.call_args_list]
        self.assertEqual(paths[1:], ["https://api.remarkets.primary.com.ar" + path for path in (
            "/rest/accounts", "/rest/risk/position/getPositions/REMTEST", "/rest/risk/detailedPosition/REMTEST",
            "/rest/risk/accountReport/REMTEST", "/rest/instruments/detail")])
        for call in session.request.call_args_list[1:]:
            self.assertEqual(call.args[0], "GET")
            self.assertEqual(call.kwargs["headers"]["X-Auth-Token"], "private-token")
            self.assertNotIn("X-Password", call.kwargs["headers"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["verify"])
        count = session.request.call_count
        for method in (lambda: client.account_read("orders", "REMTEST"),
                       lambda: client.account_read("report", "../../order/newSingleOrder"),
                       lambda: client.ggal_instrument("GGAL/OCT26 6000 C"),
                       lambda: client._request(path="/rest/order/newSingleOrder")):
            with self.assertRaises(PPIError):
                method()
        self.assertEqual(session.request.call_count, count)
