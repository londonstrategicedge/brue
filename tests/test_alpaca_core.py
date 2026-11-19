"""Alpaca adapter: the parts with no broker behind them.

Paper or live decided cold, numbers and times at the boundary, Alpaca's
refusals as SPEC codes (the bodies are the ones Alpaca's paper API returned
on 2026-11-10), and the catalog built from an asset list. No network.
"""
import datetime
import io
import json
import random
import unittest

from brueconnect import protocol
from brueconnect.adapters.alpaca import alpaca_adapter as mod
from brueconnect.conformance.run import REQUIRED_SPEC_FIELDS

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


class ColdMode(unittest.TestCase):
    """LIVE IS OPT-IN: paper unless `--live` or ALPACA_ENV=live is given.
    The key's prefix selects nothing. `mode` follows the ENDPOINTS: paper
    only for Alpaca's paper host or this machine."""

    def env(self, argv=(), **environ):
        return mod.resolve_environment(list(argv), environ)

    def check(self, cfg, env, mode, host):
        self.assertEqual((cfg["env"], cfg["mode"], cfg["trading_url"]),
                         (env, mode, host), cfg["why"])

    def test_no_key_is_paper(self):
        self.check(self.env(), "paper", "paper", PAPER)

    def test_the_key_prefix_never_selects_live(self):
        # Before: anything but a PK key was taken to the LIVE host unasked.
        for key in ("PKABC", "AKABC", "CKABC", "pkabc", "anything"):
            self.check(self.env(ALPACA_API_KEY=key), "paper", "paper", PAPER)
            self.check(self.env(BRUE_CRED_API_KEY=key), "paper", "paper",
                       PAPER)

    def test_alpaca_env_live_is_one_of_the_two_ways_to_ask(self):
        self.check(self.env(ALPACA_ENV="live", ALPACA_API_KEY="PKABC"),
                   "live", "live", LIVE)
        self.check(self.env(ALPACA_ENV=" LIVE "), "live", "live", LIVE)
        self.check(self.env(ALPACA_ENV="paper", ALPACA_API_KEY="AKABC"),
                   "paper", "paper", PAPER)
        self.check(self.env(ALPACA_ENV=" Paper "), "paper", "paper", PAPER)

    def test_an_unreadable_alpaca_env_is_not_a_request_for_live(self):
        cfg = self.env(ALPACA_ENV="sandbox", ALPACA_API_KEY="AKABC")
        self.check(cfg, "paper", "paper", PAPER)
        self.assertIn("neither paper nor live", cfg["why"])

    def test_a_flag_beats_the_variable(self):
        self.check(self.env(["--paper"], ALPACA_ENV="live",
                            ALPACA_API_KEY="AKABC"), "paper", "paper", PAPER)
        self.check(self.env(["--live"], ALPACA_ENV="paper",
                            ALPACA_API_KEY="PKABC"), "live", "live", LIVE)

    def test_both_flags_is_live(self):
        self.check(self.env(["--paper", "--live"], ALPACA_API_KEY="PKABC"),
                   "live", "live", LIVE)

    def test_every_combination_is_live_only_when_live_was_asked_for(self):
        for flags in ([], ["--paper"], ["--live"], ["--paper", "--live"]):
            for named in (None, "paper", "live", "junk"):
                for key in (None, "PKX", "AKX"):
                    environ = {}
                    if named:
                        environ["ALPACA_ENV"] = named
                    if key:
                        environ["BRUE_CRED_API_KEY"] = key
                    cfg = mod.resolve_environment(flags, environ)
                    if "--live" in flags:
                        want = "live"
                    elif flags:
                        want = "paper"
                    else:
                        want = "live" if named == "live" else "paper"
                    self.assertEqual(cfg["env"], want, (flags, named, key))
                    self.assertEqual(cfg["mode"], want, (flags, named, key))
                    self.assertEqual(cfg["trading_url"],
                                     PAPER if want == "paper" else LIVE)
                    self.assertEqual(cfg["stream_url"].startswith(
                        "wss://paper-api."), want == "paper")

    def test_mode_follows_the_endpoint_not_the_flag(self):
        # Every one of these was "paper" before: only the literal name
        # api.alpaca.markets made an endpoint live.
        for url in ("https://api.alpaca.markets./",
                    "https://paper-api.alpaca.markets./",
                    "https://198.51.100.7", "https://forwarder.example",
                    "http://paper-api.alpaca.markets.example"):
            cfg = self.env(["--paper"], ALPACA_TRADING_URL=url)
            self.assertEqual((cfg["env"], cfg["mode"]), ("paper", "live"),
                             url)
        for url in ("https://paper-api.alpaca.markets:443/",
                    "http://localhost:8123", "http://127.0.0.1:9",
                    "http://[::1]:9"):
            self.assertEqual(self.env(["--paper"],
                                      ALPACA_TRADING_URL=url)["mode"],
                             "paper", url)
        # asked for live, pointed at a stand-in on this machine: the
        # endpoint decides, and it cannot move real money
        cfg = self.env(["--live"], ALPACA_TRADING_URL="http://127.0.0.1:9",
                       ALPACA_STREAM_URL="ws://127.0.0.1:9/stream")
        self.assertEqual((cfg["env"], cfg["mode"]), ("live", "paper"))

    def test_a_key_or_secret_that_cannot_be_one_is_found_cold(self):
        ok = self.env(BRUE_CRED_API_KEY=" PKX\n", BRUE_CRED_API_SECRET="s/+=")
        self.assertEqual((ok["key"], ok["cred_fault"]), ("PKX", ""))
        for name, value in (("BRUE_CRED_API_SECRET", "first\nsecond"),
                            ("BRUE_CRED_API_SECRET", "two words"),
                            ("BRUE_CRED_API_KEY", "PK\tX"),
                            ("ALPACA_API_SECRET", "caf\u00e9"),
                            ("BRUE_CRED_API_SECRET", "bell\x07")):
            environ = {"BRUE_CRED_API_KEY": "PKX",
                       "BRUE_CRED_API_SECRET": "fine"}
            environ[name] = value
            if name.startswith("ALPACA"):
                del environ["BRUE_CRED_API_SECRET"]
            fault = mod.resolve_environment([], environ)["cred_fault"]
            self.assertTrue(fault, (name, value))
            self.assertIn("key ID" if name.endswith("KEY") else "secret",
                          fault)
            self.assertNotIn(value, fault)          # names the FIELD only

    def test_platform_credentials_come_first(self):
        cfg = self.env(BRUE_CRED_API_KEY="PKPLATFORM",
                       BRUE_CRED_API_SECRET="one", ALPACA_API_KEY="AKHAND",
                       ALPACA_API_SECRET="two")
        self.assertEqual((cfg["key"], cfg["secret"]), ("PKPLATFORM", "one"))
        self.assertEqual(cfg["mode"], "paper")

    def test_a_stand_in_endpoint_keeps_the_environment(self):
        cfg = self.env(["--paper"], ALPACA_TRADING_URL="http://127.0.0.1:9/",
                       ALPACA_DATA_URL="http://127.0.0.1:9",
                       ALPACA_STREAM_URL="ws://127.0.0.1:9/stream")
        self.assertEqual(cfg["mode"], "paper")
        self.assertEqual(cfg["trading_url"], "http://127.0.0.1:9")
        self.assertEqual(cfg["stream_url"], "ws://127.0.0.1:9/stream")

    def test_the_live_host_is_live_whatever_was_asked(self):
        cfg = self.env(["--paper"], ALPACA_TRADING_URL=LIVE)
        self.assertEqual(cfg["mode"], "live")
        cfg = self.env(["--paper"],
                       ALPACA_STREAM_URL="wss://api.alpaca.markets/stream")
        self.assertEqual(cfg["mode"], "live")

    def test_class_default_is_live_and_hello_is_cold(self):
        self.assertEqual(mod.AlpacaAdapter.mode, "live")
        for argv, environ, want in (
                (["--paper"], {"BRUE_CRED_API_KEY": "PKX"}, "paper"),
                ([], {"BRUE_CRED_API_KEY": "PKX"}, "paper"),
                ([], {"BRUE_CRED_API_KEY": "AKX"}, "paper"),
                ([], {"BRUE_CRED_API_KEY": "AKX", "ALPACA_ENV": "live"},
                 "live"),
                (["--live"], {"BRUE_CRED_API_KEY": "PKX"}, "live")):
            out = io.StringIO()
            a = mod.AlpacaAdapter(argv=argv, environ=environ, out=out)

            def boom(*args, **kw):
                raise AssertionError("hello touched the broker")
            a.client.call = boom
            a._dispatch("r1", "hello", {"protocol": 1})
            h = json.loads(out.getvalue())["result"]
            self.assertEqual(h["mode"], want)
            self.assertEqual(h["account_currency"], "USD")
            self.assertEqual(h["account_model"], "netting")
            caps = h["capabilities"]
            self.assertIs(caps["flatten_all"], True)
            # The key form travels in the handshake (SPEC 3.2), cold too,
            # and its keys are the BRUE_CRED_* names the adapter reads.
            self.assertEqual(
                [(f["key"], f["type"]) for f in h["credential_form"]],
                [("api_key", "text"), ("api_secret", "password")])
            self.assertTrue(caps["orders"]["pending_types"])
            self.assertTrue(caps["orders"]["expiry"])
            for absent in ("trailing", "oco", "close_by"):
                self.assertIs(caps["orders"][absent], False)
            self.assertIs(caps["data"]["operations"], False)
            self.assertNotIn("PKX", out.getvalue())

    def test_no_key_refuses_the_first_call_without_a_request(self):
        out = io.StringIO()
        a = mod.AlpacaAdapter(argv=[], environ={}, out=out)
        from tests.test_alpaca_orders import answer
        err = answer(a, "r1", "account.get", {})["error"]
        self.assertEqual(err["code"], "unauthorized")


class ExactNames(unittest.TestCase):
    """A handler found by method.replace(".", "_") lets another spelling of
    a mutating method reach its handler on the reads lane, past the orders
    lane and the platform's arming, which knows the method by its exact
    name. Only the protocol's exact names reach a handler (SPEC 1, 4)."""

    ALIASES = ("order_place", "flatten.all", "position_close", "Order.Place",
               "order_cancel", "order_amend", "order_modify", "positions_list",
               "order.place ")
    HANDLERS = ("order_place", "flatten_all", "position_close", "order_cancel",
                "order_amend", "order_modify", "positions_list")

    def test_another_spelling_reaches_no_handler(self):
        import time
        out = io.StringIO()
        a = mod.AlpacaAdapter(argv=["--paper"], environ={
            "ALPACA_TRADING_URL": "http://127.0.0.1:9",
            "ALPACA_DATA_URL": "http://127.0.0.1:9",
            "ALPACA_STREAM_URL": "ws://127.0.0.1:9"}, out=out)
        a._opened = True                  # no session to open: nothing to do
        ran = []
        for name in self.HANDLERS:
            setattr(a, "handle_" + name,
                    lambda p, n=name: ran.append(n) or {"closed": 0})
        for i, name in enumerate(self.ALIASES):
            a._dispatch("r%d" % i, name, {"client_id": "x%d" % i})
        end = time.monotonic() + 3.0
        while True:
            said = [json.loads(ln) for ln in out.getvalue().splitlines()]
            if len(said) >= len(self.ALIASES) or time.monotonic() > end:
                break
            time.sleep(0.02)
        self.assertEqual(ran, [])
        self.assertEqual(sorted(m["id"] for m in said),
                         sorted("r%d" % i for i in range(len(self.ALIASES))))
        for m in said:
            self.assertEqual(m["error"]["code"], "unsupported", m)


class Numbers(unittest.TestCase):
    def test_parse_ts_reads_what_alpaca_sends(self):
        want = 1794317148854
        for text in ("2026-11-10T13:25:48.854Z",
                     "2026-11-10T13:25:48.854001008Z",
                     "2026-11-10T08:25:48.854001008-05:00",
                     "2026-11-10T14:25:48.854+0100",
                     "2026-11-10 13:25:48.854999"):
            self.assertEqual(mod.parse_ts(text), want, text)
        self.assertEqual(mod.parse_ts("2026-11-10T13:25:48Z"), 1794317148000)
        self.assertEqual(mod.parse_ts("2026-11-10"), 1794268800000)
        for junk in (None, "", "soon", 12, "2026-13-40T99:00:00Z"[:4]):
            self.assertIsNone(mod.parse_ts(junk))
        self.assertEqual(mod.iso_ms(want), "2026-11-10T13:25:48.854Z")
        self.assertEqual(mod.parse_ts(mod.iso_ms(want)), want)

    def test_fixed_point_text_never_has_an_exponent(self):
        self.assertEqual(mod._fixed(1e-05), "0.00001")
        self.assertEqual(mod._fixed(1e-09), "0.000000001")
        self.assertEqual(mod._fixed(65.88930260400001), "65.889302604")
        self.assertEqual(mod._fixed(0.00030000000000000003), "0.0003")
        self.assertEqual(mod._fixed(100.12), "100.12")
        self.assertEqual(mod._fixed(3.0), "3")
        self.assertEqual(mod._fixed("0.0001995"), "0.0001995")
        self.assertEqual(mod._decimals("0.000000001"), 9)
        self.assertEqual(mod._decimals("0.01"), 2)
        self.assertEqual(mod._decimals("1"), 0)

    def test_step_test_refuses_the_suites_off_step_size(self):
        for low in (0.000013362, 0.007584376, 5.646846236, 197628.458498023):
            self.assertFalse(mod.step_ok(low + 1e-9 / 3.0, 1e-9), low)
            self.assertTrue(mod.step_ok(low, 1e-9), low)
        self.assertFalse(mod.step_ok(1.0 + 1.0 / 3.0, 1.0))
        self.assertFalse(mod.step_ok(1.5, 1.0))
        self.assertTrue(mod.step_ok(3.0, 1.0))
        for bad in (0, -1.0, float("nan")):
            self.assertFalse(mod.step_ok(bad, 1e-9))

    def test_step_test_never_refuses_a_rounded_float(self):
        # The size the suite's margin scenario sends is
        # round(need / step) * step, a float with noise in its last digits.
        rnd = random.Random(7)
        for _ in range(20000):
            equity = rnd.uniform(1e3, 1e7)
            ask = rnd.uniform(1e-5, 1e5)
            need = equity * 20 / (ask * rnd.choice((1.0, 0.5)))
            qty = round(need / 1e-9) * 1e-9
            self.assertTrue(mod.step_ok(qty, 1e-9), qty)

    def test_snap_puts_a_noisy_float_on_the_grid(self):
        self.assertEqual(mod._fixed(mod.snap(0.07956712300000001, 1e-9)),
                         "0.079567123")
        self.assertEqual(mod._fixed(mod.snap(2.0000000001, 1.0)), "2")


def refusal(status, message, code=None):
    return mod.to_adapter_error(mod.AlpacaError(status, message, code),
                                "paper")


class Refusals(unittest.TestCase):
    """HTTP status and body as observed; the SPEC code a platform gets."""

    SEEN = [
        (422, 42210000, 'asset "NOTASYMBOL" not found', "unknown_symbol"),
        (422, 42210000, 'asset "ZZZ/USD" not found', "unknown_symbol"),
        (403, 40310000, "cost basis must be >= minimal amount of order 10",
         "invalid_qty"),
        (403, 40310000, "order notional 75742956 exceeds max notional per "
         "order 200000", "invalid_qty"),
        (422, 42210000, "invalid crypto time_in_force", "invalid_expiry"),
        (422, 40010001, "invalid order type for crypto order",
         "unsupported"),
        (422, 40010001, "qty must be > 0", "invalid_qty"),
        (422, 42210000, "fractional orders must be DAY orders",
         "invalid_qty"),
        (403, 40310000, 'asset "YI" is not fractionable', "invalid_qty"),
        (403, 40310000, "insufficient buying power", "insufficient_margin"),
        (422, 42210000, 'asset "PMNT" is not tradable', "trade_restricted"),
        (422, 42210000, 'asset "YI" cannot be sold short',
         "trade_restricted"),
        (422, 42210000, "extended hours order must be DAY or GTC limit "
         "orders", "unsupported"),
        (422, 40010001, "invalid order_id", "not_found"),
        (404, 40410000, "order not found for 0000", "not_found"),
        (404, 40410000, "position does not exist", "not_found"),
        (422, 42210000, "order is not open", "not_resting"),
        (403, 40310000, "account is not authorized to trade",
         "trade_restricted"),
        (429, 42910000, "rate limit exceeded", "rate_limited"),
    ]

    def test_observed_bodies(self):
        for status, code, message, want in self.SEEN:
            e = refusal(status, message, code)
            self.assertEqual(e.code, want, message)
            self.assertIn(message, str(e))
            self.assertIn(e.code, protocol.ERROR_CODES)

    def test_extensions_carry_like_where_a_standard_code_fits(self):
        e = refusal(403, "potential wash trade detected. use complex orders",
                    40310000)
        self.assertEqual((e.code, e.like), ("x_alpaca_wash_trade",
                                            "trade_restricted"))
        e = refusal(403, "insufficient qty available for order (requested: "
                    "5, available: 0)", 40310000)
        self.assertEqual((e.code, e.like), ("x_alpaca_qty_held",
                                            "invalid_qty"))
        e = refusal(422, "invalid limit_price 100.123. sub-penny increment "
                    "does not fulfill minimum pricing criteria", 42210000)
        self.assertEqual((e.code, e.like), ("x_alpaca_invalid_price", None))
        e = refusal(403, "some new rule nobody has seen", 40310000)
        self.assertEqual((e.code, e.like), ("x_alpaca_403",
                                            "trade_restricted"))
        e = refusal(422, "some new validation", 40010001)
        self.assertEqual((e.code, e.like), ("x_alpaca_422", None))
        for err in (refusal(403, "potential wash trade", 1),
                    refusal(422, "new", 1)):
            self.assertIsNone(protocol.sanitize_like("market_closed",
                                                     err.like))

    def test_one_sentence_two_meanings(self):
        sell = mod.to_adapter_error(mod.AlpacaError(
            403, "insufficient balance for BTC (requested: 0.0002, "
            "available: 0.0001995)", 40310000), "paper", "sell", "USD")
        self.assertEqual(sell.code, "trade_restricted")
        buy = mod.to_adapter_error(mod.AlpacaError(
            403, "insufficient balance for USDT (requested: 15.18, "
            "available: 0)", 40310000), "paper", "buy", "USD")
        self.assertEqual(buy.code, "insufficient_margin")

    def test_bad_keys_name_the_environment_tried(self):
        for status, message in ((401, "unauthorized"), (403, "forbidden.")):
            e = mod.to_adapter_error(mod.AlpacaError(status, message),
                                     "paper")
            self.assertEqual(e.code, "unauthorized")
            self.assertIn("paper host", str(e))
        e = mod.to_adapter_error(mod.AlpacaError(401, "unauthorized"), "live")
        self.assertIn("live host", str(e))

    def test_a_data_403_is_not_a_bad_key(self):
        e = refusal(403, "subscription does not permit querying recent SIP "
                    "data")
        self.assertEqual(e.code, "x_alpaca_subscription")

    def test_unreachable_and_broken_are_retryable(self):
        for err in (refusal(0, "TimeoutError: timed out"),
                    refusal(503, "upstream"), refusal(429, "slow down")):
            self.assertTrue(err.retryable, str(err))
        self.assertEqual(refusal(0, "x").code, "internal")
        self.assertFalse(refusal(422, "qty must be > 0").retryable)


ACCOUNT = {"account_number": "PA1", "currency": "USD", "cash": "100000",
           "equity": "100000", "initial_margin": "0", "multiplier": "4",
           "shorting_enabled": True, "crypto_tier": 1,
           "non_marginable_buying_power": "100000"}


def share(symbol, **kw):
    a = {"id": "id-" + symbol, "class": "us_equity", "exchange": "NASDAQ",
         "symbol": symbol, "name": symbol + " Inc. Common Stock",
         "status": "active", "tradable": True, "marginable": True,
         "maintenance_margin_requirement": 30,
         "margin_requirement_long": "30", "shortable": True,
         "easy_to_borrow": True, "borrow_status": "easy_to_borrow",
         "fractionable": True}
    a.update(kw)
    return a


def coin(symbol, **kw):
    a = {"id": "id-" + symbol, "class": "crypto", "exchange": "CRYPTO",
         "symbol": symbol, "name": symbol.replace("/", " / "),
         "status": "active", "tradable": True, "marginable": False,
         "shortable": False, "fractionable": True,
         "min_order_size": "0.000013362",
         "min_trade_increment": "0.000000001",
         "price_increment": "0.000000001"}
    a.update(kw)
    return a


CALENDAR = [{"date": d, "open": "09:30", "close": c} for d, c in (
    ("2026-11-10", "16:00"), ("2026-11-11", "16:00"), ("2026-11-12", "16:00"),
    ("2026-11-13", "16:00"), ("2026-11-16", "16:00"), ("2026-11-17", "16:00"),
    ("2026-11-18", "16:00"), ("2026-11-19", "16:00"), ("2026-11-20", "16:00"),
    ("2026-11-23", "16:00"), ("2026-11-24", "16:00"), ("2026-11-25", "16:00"),
    ("2026-11-27", "13:00"), ("2026-11-30", "16:00"))]
TODAY = datetime.date(2026, 11, 10)


class Catalog(unittest.TestCase):
    def build(self, account=None, shares=None, coins=None):
        shares = shares if shares is not None else [
            share("MSFT"), share("AAPL"), share("PMNT", tradable=False),
            share("YI", shortable=False, easy_to_borrow=False,
                  borrow_status="hard_to_borrow"),
            share("OTCX", marginable=False,
                  maintenance_margin_requirement=100,
                  margin_requirement_long="100"),
            share("OLD", status="inactive")]
        coins = coins if coins is not None else [
            coin("ETH/USD"), coin("BTC/USDT"), coin("BTC/USD"),
            coin("AAVE/USD", min_order_size="0.007584376"),
            coin("ETH/BTC")]
        return mod.build_catalog(shares, coins, CALENDAR,
                                 account or ACCOUNT, TODAY)

    def test_crypto_first_then_shares_each_sorted_tradable_only(self):
        rows, ids = self.build()
        self.assertEqual([r["symbol"] for r in rows],
                         ["AAVE/USD", "BTC/USD", "ETH/USD",
                          "AAPL", "MSFT", "OTCX", "YI"])
        self.assertEqual(ids["id-BTC/USD"], "BTC/USD")
        self.assertEqual(ids["id-AAPL"], "AAPL")
        self.assertNotIn("id-BTC/USDT", ids)

    def test_every_row_has_every_required_field(self):
        rows, _ = self.build()
        for r in rows:
            for field in REQUIRED_SPEC_FIELDS:
                self.assertIn(field, r, r["symbol"])
            self.assertIs(r["tradable"], True)
        json.dumps(rows, allow_nan=False)

    def test_crypto_terms(self):
        btc = {r["symbol"]: r for r in self.build()[0]}["BTC/USD"]
        self.assertEqual((btc["lot_step"], btc["min_qty"], btc["tick_size"],
                          btc["digits"]), (1e-9, 0.000013362, 1e-9, 9))
        self.assertEqual(btc["min_notional"], 10.0)
        self.assertEqual(btc["margin_rate"], 1.0)
        self.assertEqual((btc["shortable"], btc["trade_mode"]),
                         (False, "long_only"))
        self.assertEqual(btc["sessions"], [{
            "days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00",
            "tz": "UTC"}])
        self.assertEqual(btc["fees"]["rate"], 0.0025)
        self.assertEqual((btc["base_ccy"], btc["quote_ccy"]), ("BTC", "USD"))

    def test_share_terms_calendar_and_margin(self):
        rows = {r["symbol"]: r for r in self.build()[0]}
        aapl = rows["AAPL"]
        self.assertEqual((aapl["lot_step"], aapl["min_qty"],
                          aapl["tick_size"], aapl["digits"]), (1, 1, 0.01, 2))
        self.assertEqual(aapl["sessions"], [{
            "days": [1, 2, 3, 4, 5], "open": "09:30", "close": "16:00",
            "tz": "America/New_York"}])
        self.assertEqual(aapl["holidays"], ["2026-11-26"])
        self.assertEqual(aapl["early_closes"], [{
            "date": "2026-11-27", "close": "13:00",
            "tz": "America/New_York"}])
        self.assertEqual((aapl["margin_rate"],
                          aapl["margin_maintenance_rate"]), (0.5, 0.3))
        self.assertIs(aapl["shortable"], True)
        self.assertNotIn("trade_mode", aapl)       # the default, "full"
        self.assertEqual((rows["OTCX"]["margin_rate"],
                          rows["OTCX"]["margin_maintenance_rate"]),
                         (1.0, 1.0))
        self.assertEqual((rows["YI"]["shortable"], rows["YI"]["trade_mode"]),
                         (False, "long_only"))

    def test_the_account_decides_margin_and_shorting(self):
        cash = dict(ACCOUNT, multiplier="1", shorting_enabled=False)
        rows = {r["symbol"]: r for r in self.build(cash)[0]}
        self.assertEqual(rows["AAPL"]["margin_rate"], 1.0)
        self.assertIs(rows["AAPL"]["shortable"], False)
        self.assertEqual(rows["AAPL"]["trade_mode"], "long_only")

    def test_min_qty_sits_on_the_step_grid(self):
        rows, _ = self.build(coins=[coin(
            "ODD/USD", min_order_size="0.0000133625",
            min_trade_increment="0.000000001")])
        self.assertEqual(rows[0]["min_qty"], 0.000013363)
        self.assertTrue(mod.step_ok(rows[0]["min_qty"], rows[0]["lot_step"]))


class Symbols(unittest.TestCase):
    def setUp(self):
        self.a = mod.AlpacaAdapter(argv=["--paper"], environ={},
                                   out=io.StringIO())
        rows, ids = mod.build_catalog(
            [share("AAPL"), share("ABR.PRD")],
            [coin("BTC/USD"), coin("ETH/USD")], CALENDAR, ACCOUNT, TODAY)
        self.a._install_catalog(rows, ids, 1.0)

    def test_every_spelling_becomes_the_catalogs(self):
        s = self.a._sym
        self.assertEqual(s("BTC/USD"), "BTC/USD")
        # A position: no slash, and the COIN's asset id, in no asset list.
        self.assertEqual(s("BTCUSD", "64bbff51-coin"), "BTC/USD")
        self.assertEqual(s("ETHUSD"), "ETH/USD")
        self.assertEqual(s("AAPL", "id-AAPL"), "AAPL")
        self.assertEqual(s("renamed", "id-AAPL"), "AAPL")
        self.assertEqual(s("ABR.PRD"), "ABR.PRD")

    def test_an_unknown_holding_keeps_the_brokers_name(self):
        self.assertEqual(self.a._sym("DOGEUSD", "x"), "DOGEUSD")
        self.assertEqual(self.a._sym("ZZZZ"), "ZZZZ")


if __name__ == "__main__":
    unittest.main()
