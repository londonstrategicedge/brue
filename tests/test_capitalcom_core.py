"""Capital.com adapter: what it decides with no broker at all.

Configuration (demo unless asked, `mode` from the hosts), the cold
handshake (terms, credential form, identity, capabilities, and no network),
the refusals' codes, the catalog from Capital.com's documented market
details, opening hours, bars at the bid, the pace and the bar store. No
network: a test that would reach one fails.
"""
import contextlib
import http.client
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from brueconnect import protocol
from brueconnect.adapters.capitalcom import capitalcom_adapter as mod

DEMO = "https://demo-api-capital.backend-capital.com/api/v1"
LIVE = "https://api-capital.backend-capital.com/api/v1"
CREDS = {"BRUE_CRED_API_KEY": "KEYabcdef0123456789",
         "BRUE_CRED_IDENTIFIER": "someone@example.com",
         "BRUE_CRED_PASSWORD": "pass word with spaces"}

TERMS = {"data_local_only": True, "orders_by_hand_only": True,
         "summary": "Capital.com's prices are for your own trading and stay "
                    "on this computer, and on a live account its terms have "
                    "you place each order yourself.",
         "url": "https://capital.com/en-gb/terms-and-policies"}

# GET /markets/{epic}, in the shape and with the values of the reference's
# SILVER sample (documented: https://open-api.capital.com/)
SILVER = {
    "instrument": {
        "epic": "SILVER", "symbol": "Silver", "expiry": "-", "name": "Silver",
        "lotSize": 1, "type": "COMMODITIES", "guaranteedStopAllowed": True,
        "streamingPricesAvailable": True, "currency": "USD",
        "marginFactor": 10, "marginFactorUnit": "PERCENTAGE",
        "openingHours": {
            "mon": ["00:00 - 21:59", "23:05 - 00:00"],
            "tue": ["00:00 - 21:59", "23:05 - 00:00"],
            "wed": ["00:00 - 21:59", "23:05 - 00:00"],
            "thu": ["00:00 - 21:59", "23:05 - 00:00"],
            "fri": ["00:00 - 21:59"], "sat": [],
            "sun": ["23:05 - 00:00"], "zone": "UTC"},
        "overnightFee": {"longRate": -0.0199715, "shortRate": 0.0117515,
                         "swapChargeTimestamp": 1707948000000,
                         "swapChargeInterval": 1440}},
    "dealingRules": {
        "minStepDistance": {"unit": "POINTS", "value": 0.001},
        "minDealSize": {"unit": "POINTS", "value": 1},
        "maxDealSize": {"unit": "POINTS", "value": 100000},
        "minSizeIncrement": {"unit": "POINTS", "value": 0.1},
        "minGuaranteedStopDistance": {"unit": "PERCENTAGE", "value": 1},
        "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01},
        "maxStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 60},
        "marketOrderPreference": "AVAILABLE_DEFAULT_ON",
        "trailingStopsPreference": "NOT_AVAILABLE"},
    "snapshot": {"marketStatus": "TRADEABLE", "netChange": 0.042,
                 "percentageChange": 0.17, "updateTime": "2022-04-06T13:18:00",
                 "delayTime": 0, "bid": 24.285, "offer": 24.305,
                 "high": 24.4, "low": 24.0, "decimalPlacesFactor": 3,
                 "scalingFactor": 1, "marketModes": ["REGULAR"]}}

# The same market in Capital.com's 2022 saved example (documented:
# https://github.com/capital-com-sv/capital-api-postman): older names, no
# maxDealSize and no minSizeIncrement, a window that ends "22:00"
SILVER_2022 = {
    "instrument": dict(SILVER["instrument"], openingHours={
        "mon": ["00:00 - 22:00", "23:05 - 00:00"], "fri": ["00:00 - 22:00"],
        "sat": [], "sun": ["23:05 - 00:00"], "zone": "UTC"}),
    "dealingRules": {
        "minStepDistance": {"unit": "POINTS", "value": 0.001},
        "minDealSize": {"unit": "POINTS", "value": 0.1},
        "minControlledRiskStopDistance": {"unit": "PERCENTAGE", "value": 2},
        "minNormalStopOrLimitDistance": {"unit": "PERCENTAGE",
                                         "value": 0.01},
        "maxStopOrLimitDistance": {"unit": "PERCENTAGE", "value": 60},
        "marketOrderPreference": "AVAILABLE_DEFAULT_ON",
        "trailingStopsPreference": "NOT_AVAILABLE"},
    "snapshot": SILVER["snapshot"]}


def env(**kw):
    out = dict(CREDS)
    out.update(kw)
    return out


def adapter(argv=("--paper",), **kw):
    e = env(**kw)
    e.setdefault("BRUE_BROKER_STATE", tempfile.mkdtemp(
        prefix="cc-core-", dir=os.environ.get("TMPDIR")))
    return mod.CapitalComAdapter(argv=list(argv), environ=e,
                                 out=io.StringIO())


def answers(a):
    return [json.loads(line) for line in a._out.getvalue().splitlines()]



def _clear_urlsplit_cache():
    """Clear urlsplit's cache: it has one (functools.lru_cache) from Python
    3.11 on; on 3.10 there is none, and nothing to clear."""
    clear = getattr(urllib.parse.urlsplit, "cache_clear", None)
    if clear is not None:
        clear()

class TestEnvironment(unittest.TestCase):
    def test_demo_is_the_default(self):
        cfg = mod.resolve_environment([], env())
        self.assertEqual((cfg["env"], cfg["api_url"], cfg["stream_url"],
                          cfg["mode"]), ("demo", DEMO, mod.STREAM_URL,
                                         "paper"))

    def test_live_must_be_asked_for(self):
        for argv, e, want in (([], {"CAPITALCOM_ENV": "live"}, "live"),
                              (["--live"], {}, "live"),
                              (["--paper", "--live"], {}, "live"),
                              (["--paper"], {"CAPITALCOM_ENV": "live"},
                               "demo"),
                              (["--demo"], {"CAPITALCOM_ENV": "live"},
                               "demo"),
                              ([], {"CAPITALCOM_ENV": "LIVE "}, "live"),
                              ([], {"CAPITALCOM_ENV": "real"}, "demo"),
                              ([], {"CAPITALCOM_ENV": "paper"}, "demo")):
            cfg = mod.resolve_environment(argv, env(**e))
            self.assertEqual(cfg["env"], want, (argv, e))
        cfg = mod.resolve_environment(["--live"], env())
        self.assertEqual((cfg["api_url"], cfg["mode"]), (LIVE, "live"))

    def test_mode_is_paper_only_on_the_demo_host_or_loopback(self):
        ok = ("http://127.0.0.1:5000/api/v1", "http://localhost:9/api/v1",
              "http://[::1]:9/api/v1", DEMO,
              "https://DEMO-API-CAPITAL.backend-capital.com/api/v1")
        for url in ok:
            cfg = mod.resolve_environment([], env(CAPITALCOM_API_URL=url))
            self.assertEqual(cfg["mode"], "paper", url)
        for url in ("https://demo-api-capital.backend-capital.com./api/v1",
                    "https://demo-api-capital.backend-capital.com.example/"
                    "api/v1", "https://api-capital.backend-capital.com/api/v1",
                    "http://10.0.0.1/api/v1", "https://example.com/api/v1",
                    "http://demo-api-capital.backend-capital.com/api/v1"):
            # what live is pointed at: never taken for the demo host
            cfg = mod.resolve_environment(["--live"],
                                          env(CAPITALCOM_API_URL=url))
            self.assertEqual(cfg["mode"], "live", url)
            # demo does not go there at all: it keeps the demo host, and the
            # first call says why (see test_capitalcom_faults.TestEndpoints)
            cfg = mod.resolve_environment(["--paper"],
                                          env(CAPITALCOM_API_URL=url))
            self.assertEqual((cfg["api_url"], cfg["mode"]), (DEMO, "paper"),
                             url)
            self.assertIn("CAPITALCOM_API_URL", cfg["endpoint_fault"], url)
        # the stream counts too: an unknown host is not known to be demo
        cfg = mod.resolve_environment(["--live"], env(
            CAPITALCOM_STREAM_URL="wss://prices.example/connect"))
        self.assertEqual(cfg["mode"], "live")
        cfg = mod.resolve_environment([], env(
            CAPITALCOM_STREAM_URL="ws://127.0.0.1:5001/connect"))
        self.assertEqual((cfg["mode"], cfg["endpoint_fault"]), ("paper", ""))

    def test_a_bare_host_gets_the_documented_root(self):
        cfg = mod.resolve_environment([], env(
            CAPITALCOM_API_URL="http://127.0.0.1:5000/"))
        self.assertEqual(cfg["api_url"], "http://127.0.0.1:5000/api/v1")

    # The mode and the cleartext rule are judged by the host a URL names
    # after any user part (urlparse's hostname); urllib connects to all that
    # stands before the path, user part and all, its %-escapes decoded.
    NOT_PLAIN = (
        "https://api-capital.backend-capital.com@127.0.0.1:9/api/v1",
        "https://api-capital.backend-capital.com\\@127.0.0.1:9/api/v1",
        "http://api-capital.backend-capital.com@127.0.0.1:9/api/v1",
        "https://me:secret-pw@demo-api-capital.backend-capital.com/api/v1",
        "https://forwarder.example@[::1]:9/api/v1",
        "https://127.0.0.1\\forwarder.example/api/v1",
        "https://%31%32%37.0.0.1:9/api/v1",
        "https://127.0.0.1:9x/api/v1")

    def test_an_endpoint_that_is_not_a_plain_host_is_never_used(self):
        """A URL with more than a host and a port before its path (a user
        part, a backslash, a %-escape, a port that is not a number) is an
        endpoint fault in either environment: the process keeps its own
        environment's endpoint, as for every other fault, and the fault
        quotes nothing of the URL."""
        for url in self.NOT_PLAIN:
            for argv, home, mode in ((["--paper"], DEMO, "paper"),
                                     (["--live"], LIVE, "live")):
                cfg = mod.resolve_environment(argv, env(
                    CAPITALCOM_API_URL=url))
                self.assertEqual((cfg["api_url"], cfg["mode"]), (home, mode),
                                 (argv, url))
                self.assertIn("CAPITALCOM_API_URL", cfg["endpoint_fault"],
                              (argv, url))
                self.assertNotIn("@", cfg["endpoint_fault"], (argv, url))
                self.assertNotIn("secret-pw", cfg["endpoint_fault"])
        for url in ("wss://api-streaming-capital.backend-capital.com@"
                    "127.0.0.1:9/connect", "ws://me:pw@127.0.0.1:9/connect"):
            for argv, mode in ((["--paper"], "paper"), (["--live"], "live")):
                cfg = mod.resolve_environment(argv, env(
                    CAPITALCOM_STREAM_URL=url))
                self.assertEqual((cfg["stream_url"], cfg["mode"]),
                                 (mod.STREAM_URL, mode), (argv, url))
                self.assertIn("CAPITALCOM_STREAM_URL", cfg["endpoint_fault"],
                              (argv, url))
                self.assertNotIn("@", cfg["endpoint_fault"], (argv, url))

    def test_the_host_judged_is_the_host_connected_to(self):
        """Whatever endpoint is kept, the host its mode and its cleartext
        rule were judged from is the host urllib connects to (read from
        http.client's own parsing, without connecting)."""
        for url in self.NOT_PLAIN + (
                "http://127.0.0.1:5000/api/v1", "http://[::1]:9/api/v1",
                DEMO, LIVE, "https://example.com/api/v1"):
            for argv in (["--paper"], ["--live"]):
                api = mod.resolve_environment(argv, env(
                    CAPITALCOM_API_URL=url))["api_url"]
                host = http.client.HTTPConnection(
                    urllib.request.Request(api).host).host
                self.assertEqual(host.lower(), mod._host(api), (argv, url))

    # -- an endpoint is judged as it is written ---------------------------

    def test_brackets_in_the_host_are_refused_by_any_python(self):
        """Python 3.10.0-3.10.15 and 3.11.0-3.11.3 (inside requires-python)
        have no bracket check in urlsplit: there
        http://<live host>[::1]:9 is judged '::1' (paper, plain http
        allowed), and urllib looks up '<live host>[::1]'. Emulated here by
        taking that check out where it exists (create=True: on a Python
        without it the parser is the older one already, and the attribute
        is made for this test only); the URL is refused either way, by what
        it says, not by the interpreter's parser. urlsplit's cache, which it
        has from 3.11 on, is cleared around it (_clear_urlsplit_cache: on
        3.10 there is none to clear)."""
        urls = ("http://api-capital.backend-capital.com[::1]:9/api/v1",
                "http://[::1]api-capital.backend-capital.com:9/api/v1",
                "https://[::1]x/api/v1", "http://[127.0.0.1]:9/api/v1",
                "http://[1:2:3]:9/api/v1")
        for older in (False, True):
            patch = (mock.patch.object(urllib.parse, "_check_bracketed_netloc",
                                       lambda netloc: None, create=True)
                     if older else contextlib.nullcontext())
            with patch:
                _clear_urlsplit_cache()
                for url in urls:
                    for argv, home, mode in ((["--paper"], DEMO, "paper"),
                                             (["--live"], LIVE, "live")):
                        with self.subTest(older=older, url=url, argv=argv):
                            cfg = mod.resolve_environment(argv, env(
                                CAPITALCOM_API_URL=url))
                            self.assertEqual((cfg["api_url"], cfg["mode"]),
                                             (home, mode))
                            self.assertIn("CAPITALCOM_API_URL has brackets",
                                          cfg["endpoint_fault"])
            _clear_urlsplit_cache()
        # one IPv6 address in brackets, with or without a port, is plain
        for url in ("http://[::1]:9/api/v1", "http://[::1]/api/v1"):
            cfg = mod.resolve_environment([], env(CAPITALCOM_API_URL=url))
            self.assertEqual((cfg["mode"], cfg["endpoint_fault"]),
                             ("paper", ""), url)

    def test_a_url_with_a_space_or_a_line_break_is_never_used(self):
        """urlsplit drops tabs and line breaks before it reads a URL, so
        http://127.0.0.1<TAB>:9 was taken; urllib kept the tab, http.client
        refused it (InvalidURL), and the call was told the connection broke
        while Capital.com was answering, though nothing had connected."""
        for url in ("http://127.0.0.1\t:9/api/v1", "http://127.0.0.1:9/ap\ni/v1",
                    "http://127.0.0.1:9/api\r/v1", "http://127.0.0.1:9/a pi/v1",
                    "http://127.0.0.1:9/api\x00/v1"):
            for name, key, home in (("CAPITALCOM_API_URL", "api_url", DEMO),
                                    ("CAPITALCOM_STREAM_URL", "stream_url",
                                     mod.STREAM_URL)):
                with self.subTest(url=url, name=name):
                    if name == "CAPITALCOM_STREAM_URL":
                        url = url.replace("http://", "ws://")
                    cfg = mod.resolve_environment([], env(**{name: url}))
                    self.assertEqual(cfg[key], home)
                    self.assertIn(name + " has a space, a tab or a line "
                                  "break in it", cfg["endpoint_fault"])

    def test_each_fault_says_what_it_found(self):
        """The fault for a port that is not a number said the URL had a user
        name, a backslash or a %-escape before its path, which it had not."""
        for url, what in (
                ("https://127.0.0.1:9x/api/v1", "has a port that is not a "
                 "number from 1 to 65535"),
                ("https://127.0.0.1:99999/api/v1", "has a port that is not a "
                 "number from 1 to 65535"),
                ("https://127.0.0.1:0/api/v1", "has a port that is not a "
                 "number from 1 to 65535"),
                ("https://me@127.0.0.1:9/api/v1", "has a user name before its "
                 "host"),
                ("https://127.0.0.1\\x.example/api/v1", "has a backslash "
                 "before its path"),
                ("https://%31%32%37.0.0.1:9/api/v1", "has a %-escape in its "
                 "host"),
                ("https://ex ample.com/api/v1", "has a space, a tab or a "
                 "line break in it"),
                ("https://ex*ample.com/api/v1", "has a host that is not a "
                 "plain name or address")):
            with self.subTest(url=url):
                fault = mod.resolve_environment(["--live"], env(
                    CAPITALCOM_API_URL=url))["endpoint_fault"]
                self.assertTrue(fault.startswith("CAPITALCOM_API_URL " + what),
                                fault)
                self.assertNotIn("@", fault)
                self.assertNotIn("x.example", fault)

    def test_the_platforms_values_win_over_the_hand_set_ones(self):
        cfg = mod.resolve_environment([], dict(
            CREDS, CAPITALCOM_API_KEY="other", CAPITALCOM_IDENTIFIER="x@y",
            CAPITALCOM_PASSWORD="z"))
        self.assertEqual((cfg["key"], cfg["identifier"], cfg["password"]), (
            CREDS["BRUE_CRED_API_KEY"], CREDS["BRUE_CRED_IDENTIFIER"],
            CREDS["BRUE_CRED_PASSWORD"]))
        cfg = mod.resolve_environment([], {"CAPITALCOM_API_KEY": "k1",
                                           "CAPITALCOM_IDENTIFIER": "a@b",
                                           "CAPITALCOM_PASSWORD": "p"})
        self.assertEqual((cfg["key"], cfg["identifier"], cfg["password"]),
                         ("k1", "a@b", "p"))

    def test_a_value_that_cannot_be_a_credential_is_named_never_quoted(self):
        cases = ((dict(BRUE_CRED_API_KEY="KEY with space"), "API key"),
                 (dict(BRUE_CRED_API_KEY="KEYéx"), "API key"),
                 (dict(BRUE_CRED_IDENTIFIER="a\tb@c"), "login email"),
                 (dict(BRUE_CRED_PASSWORD="pa\nss"), "API key password"))
        for e, label in cases:
            cfg = mod.resolve_environment([], env(**e))
            self.assertIn(label, cfg["cred_fault"])
            value = list(e.values())[0]
            self.assertNotIn(value.strip(), cfg["cred_fault"])
        # a space in the password is part of it; a pasted line break at its
        # end is not
        cfg = mod.resolve_environment([], env(
            BRUE_CRED_PASSWORD=" my pass \r\n"))
        self.assertEqual((cfg["cred_fault"], cfg["password"]),
                         ("", " my pass "))


class TestExactNames(unittest.TestCase):
    """A handler found by method.replace(".", "_") lets "order_place" reach
    the order handler on the reads lane, past the orders lane and the
    platform's arming and by-hand rules, which know order.place by its
    exact name. Only the protocol's exact names reach a handler (SPEC 1,
    4)."""

    ALIASES = ("order_place", "flatten.all", "position_close", "Order.Place",
               "order_cancel", "order_amend", "order_modify", "positions_list",
               "order.place ")
    HANDLERS = ("order_place", "flatten_all", "position_close", "order_cancel",
                "order_amend", "order_modify", "positions_list")

    def test_another_spelling_reaches_no_handler(self):
        a = adapter(CAPITALCOM_API_URL="http://127.0.0.1:9/api/v1",
                    CAPITALCOM_STREAM_URL="ws://127.0.0.1:9/connect")
        a._opened = True                  # no session to open: nothing to do
        ran = []
        for name in self.HANDLERS:
            setattr(a, "handle_" + name,
                    lambda p, n=name: ran.append(n) or {"closed": 0})
        for i, name in enumerate(self.ALIASES):
            a._dispatch("r%d" % i, name, {"client_id": "x%d" % i})
        end = time.monotonic() + 3.0
        while len(answers(a)) < len(self.ALIASES) and time.monotonic() < end:
            time.sleep(0.02)
        said = answers(a)
        self.assertEqual(ran, [])
        self.assertEqual(sorted(m["id"] for m in said),
                         sorted("r%d" % i for i in range(len(self.ALIASES))))
        for m in said:
            self.assertEqual(m["error"]["code"], "unsupported", m)
        a._dispatch("ok", "order.place", {"client_id": "x-ok"})
        end = time.monotonic() + 3.0
        while not ran and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(ran, ["order_place"])


class _Recorder:
    """An HTTP server on this machine that writes down what reaches it (the
    method, the path and the header names) and answers with `answer`:
    (status, headers, body)."""

    def __init__(self, answer):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                seen.append((self.command, self.path,
                             sorted(k.lower() for k in self.headers)))
                status, headers, body = answer(self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_CONNECT = _go

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class TestWhereRequestsGo(unittest.TestCase):
    """The key, the login and the session's tokens go to the endpoint this
    process was given and nowhere else, whatever the endpoint's answer or
    the environment says: urllib by itself follows a redirect to any host
    with the X-Cap-Api-Key header, and sends a plain http endpoint on this
    machine through a proxy named in the environment."""

    def server(self, answer):
        s = _Recorder(answer)
        self.addCleanup(s.stop)
        return s

    def client(self, url):
        return mod.CapitalClient(mod.resolve_environment(
            [], env(CAPITALCOM_API_URL=url + "/api/v1")))

    def test_a_redirect_is_never_followed(self):
        other = self.server(lambda h: (200, {}, b"{}"))
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                there = self.server(lambda h, s=status: (
                    s, {"Location": other.url + "/api/v1/session"}, b""))
                c = self.client(there.url)
                with self.assertRaises(mod.CapitalError) as cm:
                    c._http("POST", there.url + "/api/v1/session",
                            {"X-CAP-API-KEY": CREDS["BRUE_CRED_API_KEY"]},
                            {"identifier": "someone@example.com"}, 5.0)
                e = cm.exception
                self.assertEqual((e.status, e.code), (status, "redirect"))
                self.assertIn("not followed", e.message)
                self.assertEqual(len(there.seen), 1)
                # 307 and 308: the request was not carried out at this
                # address; the others may follow a request carried out
                self.assertEqual(e.unanswered, status not in (307, 308))
                err = mod.to_adapter_error(e, "demo")
                self.assertEqual((err.code, err.like, err.retryable),
                                 ("x_capitalcom_redirected", "internal",
                                  False))
        self.assertEqual(other.seen, [])

    def test_no_proxy_is_used(self):
        proxy = self.server(lambda h: (200, {}, b'{"proxied": true}'))
        here = self.server(lambda h: (200, {"Content-Type":
                                            "application/json"}, b"{}"))
        names = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY")
        # urllib's shared opener reads the proxies when it is first built: it
        # is built afresh here, as in an adapter process started with them
        with mock.patch.dict(os.environ, {n: proxy.url for n in names}), \
                mock.patch.object(urllib.request, "_opener", None):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            c = self.client(here.url)
            status, _, raw = c._http(
                "POST", here.url + "/api/v1/session",
                {"X-CAP-API-KEY": CREDS["BRUE_CRED_API_KEY"]},
                {"identifier": "someone@example.com"}, 5.0)
        self.assertEqual((status, raw), (200, b"{}"))
        self.assertEqual([s[:2] for s in here.seen],
                         [("POST", "/api/v1/session")])
        self.assertEqual(proxy.seen, [])

    def test_a_url_http_client_refuses_was_never_sent(self):
        """InvalidURL is raised before anything connects; it was told as a
        connection that broke while Capital.com was answering (sent)."""
        c = self.client("http://127.0.0.1:9")
        with mock.patch.object(c, "_open", side_effect=http.client.InvalidURL(
                "URL can't contain control characters")):
            with self.assertRaises(mod.CapitalError) as cm:
                c._http("GET", "http://127.0.0.1:9/api/v1/time", {}, None, 1.0)
        self.assertFalse(cm.exception.sent)
        self.assertNotIn("control characters", cm.exception.message)


class TestHello(unittest.TestCase):
    def setUp(self):
        def no_network(*a, **k):
            raise AssertionError("hello reached the network")
        self.patches = [mock.patch.object(urllib.request, "urlopen",
                                          no_network),
                        mock.patch.object(socket, "create_connection",
                                          no_network)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def hello(self, a):
        a._dispatch("h1", "hello", {"protocol": 1})
        msg = answers(a)[-1]
        self.assertEqual(msg["id"], "h1")
        return msg["result"]

    def test_hello_is_cold_and_says_what_this_is(self):
        a = adapter()
        h = self.hello(a)
        self.assertEqual(h["protocol"], 1)
        self.assertEqual(h["adapter"], {"name": "capitalcom",
                                        "version": mod.VERSION})
        self.assertEqual(h["mode"], "paper")
        self.assertEqual(h["account_model"], "hedging")
        self.assertTrue(h["capabilities"]["flatten_all"])
        self.assertFalse(a._opened)
        self.assertFalse(a.client.has_session())

    def test_live_says_live(self):
        self.assertEqual(self.hello(adapter(argv=["--live"]))["mode"], "live")
        self.assertEqual(self.hello(adapter(argv=[], CAPITALCOM_ENV="live"))
                         ["mode"], "live")

    def test_terms_are_capitalcoms_and_reach_the_platform_whole(self):
        h = self.hello(adapter())
        self.assertEqual(h["terms"], TERMS)
        self.assertEqual(protocol.sanitize_terms(h["terms"]), TERMS)
        self.assertLessEqual(len(TERMS["summary"]), protocol.TERMS_SUMMARY)

    def test_the_credential_form_names_the_three_values(self):
        h = self.hello(adapter())
        want = [{"key": "api_key", "label": "API key", "type": "password"},
                {"key": "identifier", "label": "Login email", "type": "text"},
                {"key": "password", "label": "API key password",
                 "type": "password"}]
        self.assertEqual(h["credential_form"], want)
        self.assertEqual(protocol.sanitize_credential_form(want), want)
        # the keys are the environment the adapter reads
        for f in want:
            self.assertIn("BRUE_CRED_" + f["key"].upper(), CREDS)

    def test_broker_info_survives_the_sanitiser_whole(self):
        a = adapter()
        raw = a.broker_info()
        self.assertEqual(protocol.sanitize_broker_info(raw), raw)
        h = self.hello(a)
        self.assertEqual(h["broker_info"]["display_name"], "Capital.com")
        self.assertEqual(h["broker_info"]["regulated_by"],
                         ["FCA 793714", "CySEC 319/17"])

    def test_capabilities_are_what_capitalcom_serves(self):
        caps = self.hello(adapter())["capabilities"]
        o, d = caps["orders"], caps["data"]
        self.assertEqual(o["pending_types"], ["limit", "stop"])
        self.assertEqual(o["expiry"], ["gtc", "gtd"])
        self.assertTrue(o["pending"] and o["brackets"] and o["modify"]
                        and o["trailing"] and o["can_list"])
        self.assertFalse(o["partial_close"] or o["oco"] or o["close_by"])
        self.assertIs(o["reserves_margin"], False)
        self.assertEqual((d["max_quote_symbols"],
                          d["max_symbols_per_request"]), (40, 40))
        self.assertTrue(d["quotes"] and d["bars"] and d["history"]
                        and d["operations"])
        self.assertEqual(caps["accounts"], {"multi": True})
        self.assertNotIn("auth", caps)

    def test_no_secret_in_hello(self):
        a = adapter()
        self.hello(a)
        text = a._out.getvalue()
        for v in CREDS.values():
            self.assertNotIn(v, text)


class TestErrors(unittest.TestCase):
    def code(self, status, code=None, sent=True):
        e = mod.to_adapter_error(mod.CapitalError(status, code or "x",
                                                  code=code, sent=sent),
                                 "demo")
        return e.code, e.like, e.retryable

    def test_capitalcoms_documented_codes(self):
        self.assertEqual(self.code(429, "error.too-many.requests"),
                         ("rate_limited", None, True))
        self.assertEqual(self.code(404, "error.not-found.epic")[0],
                         "unknown_symbol")
        self.assertEqual(self.code(404, "error.not-found.dealId")[0],
                         "not_found")
        self.assertEqual(self.code(404, "error.not-found.dealReference")[0],
                         "not_found")
        for c in ("error.invalid.stoploss.minvalue: 17.146",
                  "error.invalid.takeprofit.maxvalue: 31.817"):
            self.assertEqual(self.code(400, c)[0], "invalid_stops")
        self.assertEqual(self.code(400, "error.not-in-future.goodTillDate")
                         [0], "invalid_expiry")
        self.assertEqual(self.code(401, "error.invalid.details")[0],
                         "unauthorized")
        self.assertEqual(self.code(400, "error.invalid.accountId")[:2],
                         ("x_capitalcom_account", "not_found"))

    def test_the_rest(self):
        self.assertEqual(self.code(500), ("internal", None, True))
        self.assertEqual(self.code(0, sent=False)[0::2], ("internal", True))
        self.assertEqual(self.code(429, "budget"), ("rate_limited", None,
                                                    True))
        self.assertEqual(self.code(400, "error.invalid.size.minvalue: 1")[0],
                         "invalid_qty")
        c, like, _ = self.code(400, "error.something.new")
        self.assertTrue(c.startswith("x_capitalcom_"))
        self.assertIn(c, protocol.res_error("r", c, "m"))

    def test_rejections_read_loosely_and_never_guess(self):
        self.assertEqual(mod.reject_code("INSUFFICIENT_FUNDS")[0],
                         "insufficient_margin")
        self.assertEqual(mod.reject_code("MARKET_CLOSED")[0], "market_closed")
        self.assertEqual(mod.reject_code("SOMETHING_ELSE")[0],
                         "x_capitalcom_rejected")
        self.assertEqual(mod.reject_code(None)[0], "x_capitalcom_rejected")

    def test_a_lost_answer_is_unanswered_a_refusal_is_not(self):
        self.assertTrue(mod.CapitalError(0, "x", sent=True).unanswered)
        self.assertTrue(mod.CapitalError(502, "x").unanswered)
        self.assertFalse(mod.CapitalError(0, "x", sent=False).unanswered)
        self.assertFalse(mod.CapitalError(400, "x").unanswered)
        self.assertFalse(mod.CapitalError(429, "x", code="budget",
                                          sent=False).unanswered)

    def test_a_login_with_no_account_on_the_host_is_said_so(self):
        """2026-11-15, a real login: the demo host refused POST /session
        with 401 error.null.accountId, and the adapter said Capital.com had
        refused the API key, with advice about lost and paused keys. It says
        that Capital.com found no account for this login on that host, and
        the likely cause with what to do: a key made while the platform was
        on the other account may not reach this one (which the code marks
        assumed, verify on demo: said as likely, never as fact), in the
        adapter's own words (no platform's words such as "the demo row")."""
        for argv, host, needs in (
                (["--paper"], "demo host", (
                    "Likely the API key was made while Capital.com's "
                    "platform was on a live account: such a key may not "
                    "reach the demo account. Make the key with the platform "
                    "switched to Demo (Settings > API integrations)")),
                (["--live"], "live host", (
                    "Likely the API key was made while Capital.com's "
                    "platform was on the Demo account: such a key may not "
                    "reach a live account. Make the key with the platform "
                    "switched to the live account (Settings > API "
                    "integrations)"))):
            with self.subTest(host=host):
                e = adapter(argv)._login_error(mod.CapitalError(
                    401, "error.null.accountId",
                    code="error.null.accountId"))
                self.assertEqual((e.code, e.like),
                                 ("x_capitalcom_no_account", "unauthorized"))
                msg = str(e)
                self.assertTrue(msg.startswith(
                    "Capital.com found no account for this login on its "
                    + host + " (error.null.accountId). "), msg)
                self.assertIn(needs, msg)
                for wrong in ("refused the API key", "paused", "suspended",
                              "lasts a year", " row", "Row ", "needs an API",
                              "works on the live host only",
                              "works on the demo host only"):
                    self.assertNotIn(wrong, msg)
                for v in CREDS.values():
                    self.assertNotIn(v, msg)

    def test_a_page_that_is_not_json_is_its_title_on_one_line(self):
        """With raw[:120] kept of a body that is not JSON, a gateway's 502
        page goes into the message with its CRLFs (and flatten_all's
        failure list quotes it). Said by the page's title, on one line,
        credentials taken out before anything is cut."""
        a = adapter()
        page = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n"
                b"<body>\r\n<center><h1>502 Bad Gateway</h1></center>\r\n"
                b"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")
        e = a.client._error(502, {}, page)
        self.assertEqual((e.status, e.message, e.code), (502, "Bad Gateway",
                                                         None))
        e = a.client._error(503, {}, b"<p>Service\r\n\r\nUnavailable "
                            + b"x" * 400 + b"</p>")
        self.assertRegex(e.message, r"^Service Unavailable x+\.\.\.$")
        self.assertLessEqual(len(e.message), 80)
        key = CREDS["BRUE_CRED_API_KEY"]
        esc = "".join("&#%d;" % ord(ch) for ch in key)
        for lead in (0, 40, 60, 75):
            e = a.client._error(502, {}, ("<title>%s %s</title>"
                                          % ("x" * lead, esc)).encode())
            for i in range(len(key) - 5):
                self.assertNotIn(key[i:i + 6], e.message)
        # Capital.com's own code is kept, on one line
        e = a.client._error(400, {}, b'{"errorCode": "error.bad\\r\\ncode"}')
        self.assertEqual((e.code, e.message), ("error.bad code",
                                               "error.bad code"))
        for e in (a.client._error(502, {}, page),
                  a.client._error(400, {}, b'{"errorCode": "a\\nb"}')):
            err = mod.to_adapter_error(e, "demo")
            self.assertNotRegex(str(err), r"[\r\n<>]")

    # What a page may put inside a secret that a reader does not see, or
    # reads past: a tag, a soft hyphen, a character that prints as nothing.
    SPLITS = ("<wbr>", "<b>", "</b><i>", "<!-- -->", "&lt;wbr&gt;", "&shy;",
              "&#173;", "\xad", "\u200b", "&#8203;", "&ZeroWidthSpace;",
              "&zwnj;", "&zwj;", "\u2060", "\ufeff", "\u200e")

    @staticmethod
    def split(secret, by, at=(0.5,)):
        """`secret` with `by` written into it at each fraction `at`."""
        cuts = sorted({max(1, min(len(secret) - 1, int(len(secret) * f)))
                       for f in at})
        out, last = "", 0
        for k in cuts:
            out += secret[last:k] + by
            last = k
        return out + secret[last:]

    def test_a_secret_split_in_a_page_is_hidden(self):
        """A page that splits a secret with a tag (<wbr>, <b>), a soft
        hyphen or a character that prints as nothing does not put it in the
        words in readable pieces, for each of the five (the password, the
        login, the API key and both session tokens), and no < or > of the
        tags inside a <title> reaches the message. The title's text is
        taken without its tags, what prints as nothing is dropped, then the
        secrets are taken out (also where white space splits one), then the
        words are cut."""
        a = self.client_with_session()
        cl = a.client
        secrets = {"password": CREDS["BRUE_CRED_PASSWORD"],
                   "identifier": CREDS["BRUE_CRED_IDENTIFIER"],
                   "key": CREDS["BRUE_CRED_API_KEY"],
                   "cst": cl._cst, "xst": cl._xst}
        for name, secret in secrets.items():
            for by in self.SPLITS:
                for at in ((0.5,), (1 / 3,), (0.25, 0.75), (0.06,), (0.94,)):
                    written = self.split(secret, by, at)
                    for lead in (0, 30):
                        for where in ("title", "body"):
                            with self.subTest(secret=name, by=by, at=at,
                                              lead=lead, where=where):
                                page = ("<html><head><title>%s %s</title>"
                                        "</head><body>z</body></html>"
                                        if where == "title" else
                                        "<p>%s %s</p>") % ("z" * lead,
                                                           written)
                                e = cl._error(502, {}, page.encode())
                                err = mod.to_adapter_error(e, "demo")
                                for text in (e.message, str(err)):
                                    for i in range(len(secret) - 5):
                                        self.assertNotIn(secret[i:i + 6],
                                                         text)
                                    self.assertNotRegex(text, r"[<>]")
                                self.assertIn("[hidden]", e.message)
        # and a gateway's own words are still its words
        for status, page, words in (
                (502, b"<title>502 Bad<b> Gateway</b></title>", "Bad Gateway"),
                (503, b"<title>503 Service &lt;i&gt;Unavailable</title>",
                 "Service Unavailable"),
                (502, b"<title>Bad\xc2\xad Gate\xe2\x80\x8bway</title>",
                 "Bad Gateway")):
            with self.subTest(page=page):
                self.assertEqual(cl._error(status, {}, page).message, words)

    @staticmethod
    def client_with_session():
        """An adapter whose client holds both session tokens (as after a
        POST /session), long enough to be taken out of any text."""
        a = adapter()
        a.client._cst = "CST-" + "c" * 20 + "-0001"
        a.client._xst = "XST-" + "w" * 20 + "-0002"
        return a

    def test_what_a_lost_answer_says_names_no_one(self):
        """The words after "Not known yet whether Capital.com took the
        order: " (the order and the close said "Capital.com did not answer
        (Capital.com's answer was not JSON (17 bytes)); whether it took the
        order is not known yet": the name twice, and the not-known words
        last)."""
        for e, words in (
                (mod.CapitalError(0, "Capital.com's answer was not JSON (17 "
                                  "bytes)", sent=True, cause="not JSON, 17 "
                                  "bytes", answered=True),
                 "its answer could not be read (not JSON, 17 bytes)"),
                (mod.CapitalError(0, "the connection broke while Capital.com "
                                  "was answering (TimeoutError)", sent=True,
                                  cause="TimeoutError"),
                 "it did not answer (TimeoutError)"),
                (mod.CapitalError(502, "Bad Gateway"),
                 "it answered HTTP 502 (Bad Gateway)"),
                (mod.CapitalError(500, "error.internal",
                                  code="error.internal"),
                 "it answered HTTP 500 (error.internal)"),
                (mod.CapitalError(503, "HTTP 503"), "it answered HTTP 503"),
                (mod.CapitalError(303, "x", code="redirect"),
                 "it answered HTTP 303, a redirect, which is not followed")):
            with self.subTest(words=words):
                self.assertTrue(e.unanswered)
                self.assertEqual(mod.unanswered(e), words)

    def test_the_login_refusal_says_where_and_who_may_use_it(self):
        a = adapter()
        e = a._login_error(mod.CapitalError(401, "error.invalid.details",
                                            code="error.invalid.details"))
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("demo host", str(e))
        self.assertIn("United States", str(e))
        for v in CREDS.values():
            self.assertNotIn(v, str(e))


class TestHelpers(unittest.TestCase):
    def test_parse_utc_reads_capitalcoms_times(self):
        self.assertEqual(mod.parse_utc("2022-04-06T07:32:19.193"),
                         1649230339193)
        self.assertEqual(mod.parse_utc("2022-02-24T00:00:00"), 1645660800000)
        self.assertIsNone(mod.parse_utc("yesterday"))
        self.assertEqual(mod.utc_text(1645660800000), "2022-02-24T00:00:00")

    def test_step_and_grid(self):
        self.assertTrue(mod.step_ok(0.3, 0.1))
        self.assertTrue(mod.step_ok(65.88930260400001, 0.000000001))
        self.assertFalse(mod.step_ok(1 + 0.1 / 3, 0.1))
        self.assertTrue(mod.on_grid(1.08501, 0.00001))
        self.assertFalse(mod.on_grid(1.085015, 0.00001))
        self.assertEqual(mod._wire(0.1), 0.1)
        self.assertEqual(mod._wire(1000.0), 1000)

    def test_a_bar_is_read_at_the_bid_and_in_utc(self):
        row = {"snapshotTime": "2022-02-24T01:00:00",
               "snapshotTimeUTC": "2022-02-24T00:00:00",
               "openPrice": {"bid": 1.1, "ask": 1.2},
               "highPrice": {"bid": 1.3, "ask": 1.4},
               "lowPrice": {"bid": 1.0, "ask": 1.05},
               "closePrice": {"bid": 1.25, "ask": 1.3},
               "lastTradedVolume": 42}
        self.assertEqual(mod.bar_row(row),
                         [1645660800000, 1.1, 1.3, 1.0, 1.25, 42.0])
        # LSE's rule: where there is no bid, the price there is
        half = dict(row, closePrice={"ask": 1.31})
        self.assertEqual(mod.bar_row(half)[4], 1.31)
        self.assertIsNone(mod.bar_row(dict(row, snapshotTimeUTC=None)))


class TestHours(unittest.TestCase):
    def test_both_ways_of_writing_the_same_window(self):
        self.assertEqual(mod.hours_windows("00:00 - 21:59"), [(0, 1320)])
        self.assertEqual(mod.hours_windows("00:00 - 22:00"), [(0, 1320)])
        self.assertEqual(mod.hours_windows("23:05 - 00:00"), [(1385, 1440)])
        self.assertEqual(mod.hours_windows("00:00 - 00:00"), [(0, 1440)])
        self.assertEqual(mod.hours_windows("22:00 - 02:00"),
                         [(1320, 1440), (0, 120)])
        self.assertEqual(mod.hours_windows("rubbish"), [])

    def test_sessions_from_the_documented_week(self):
        s = mod.sessions_from_hours(SILVER["instrument"]["openingHours"])
        self.assertIn({"days": [1, 2, 3, 4, 5], "open": "00:00",
                       "close": "22:00", "tz": "UTC"}, s)
        self.assertIn({"days": [1, 2, 3, 4, 7], "open": "23:05",
                       "close": "24:00", "tz": "UTC"}, s)
        self.assertEqual(len(s), 2)

    def test_a_window_past_midnight_runs_into_the_next_day(self):
        s = mod.sessions_from_hours({"fri": ["22:00 - 02:00"],
                                     "sun": ["23:00 - 01:00"], "zone": "UTC"})
        self.assertIn({"days": [5], "open": "22:00", "close": "24:00",
                       "tz": "UTC"}, s)
        self.assertIn({"days": [6], "open": "00:00", "close": "02:00",
                       "tz": "UTC"}, s)
        self.assertIn({"days": [1], "open": "00:00", "close": "01:00",
                       "tz": "UTC"}, s)

    def test_always_open_is_said_and_unknown_is_empty(self):
        week = {d: ["00:00 - 00:00"] for d in mod._DAYS}
        week["zone"] = "UTC"
        self.assertEqual(mod.sessions_from_hours(week), [
            {"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00",
             "close": "24:00", "tz": "UTC"}])
        self.assertEqual(mod.sessions_from_hours(None), [])
        self.assertEqual(mod.sessions_from_hours({}), [])

    def test_in_sessions_on_the_brokers_clock(self):
        s = mod.sessions_from_hours(SILVER["instrument"]["openingHours"])
        sat = 1794614400000          # 2026-11-14, a Saturday, 00:00 UTC
        self.assertFalse(mod.in_sessions(s, sat + 3600_000))
        self.assertTrue(mod.in_sessions(s, sat - 3 * 86400_000 + 3600_000))
        self.assertIsNone(mod.in_sessions([], sat))


class TestCatalog(unittest.TestCase):
    def test_the_documented_sample(self):
        spec, priv = mod.build_spec(SILVER, {"tickSize": 0.001})
        self.assertEqual((spec["symbol"], spec["asset_class"],
                          spec["quote_ccy"], spec["digits"],
                          spec["tick_size"], spec["lot_step"],
                          spec["min_qty"], spec["max_qty"],
                          spec["contract_size"]),
                         ("SILVER", "commodity", "USD", 3, 0.001, 0.1, 1,
                          100000, 1))
        self.assertAlmostEqual(spec["margin_rate"], 0.10)
        self.assertAlmostEqual(spec["margin_maintenance_rate"], 0.05)
        self.assertTrue(spec["tradable"])
        self.assertEqual(spec["trade_mode"], "full")
        # 0.01% of 24.295 is 0.0024: 3 ticks, with a tenth to spare and one
        # point more: 4
        self.assertEqual(spec["stops_level_points"], 4)
        self.assertEqual(spec["fees"]["mode"], "none")
        self.assertEqual(spec["swap_mode"], "pct_annual")
        self.assertEqual(spec["swap_3day_dow"], 3)      # silver: Wednesday
        self.assertEqual(priv["trailing"], "NOT_AVAILABLE")
        for f in ("symbol", "asset_class", "quote_ccy", "digits", "tick_size",
                  "lot_step", "min_qty", "max_qty", "contract_size",
                  "margin_rate", "sessions", "holidays", "tradable",
                  "shortable"):
            self.assertIn(f, spec)

    def test_the_older_names_are_read_too(self):
        spec, priv = mod.build_spec(SILVER_2022)
        self.assertEqual((spec["min_qty"], spec["lot_step"]), (0.1, 0.1))
        self.assertEqual(spec["max_qty"], 1e9)
        self.assertEqual(spec["stops_level_points"], 4)
        self.assertEqual(priv["stop_max"], ("PERCENTAGE", 60.0))
        s = [x for x in spec["sessions"] if x["open"] == "00:00"]
        self.assertEqual(s[0]["close"], "22:00")

    def test_points_and_view_only(self):
        d = json.loads(json.dumps(SILVER))
        d["dealingRules"]["minStopOrProfitDistance"] = {"unit": "POINTS",
                                                        "value": 0.005}
        d["snapshot"]["marketModes"] = ["VIEW_ONLY"]
        d["snapshot"]["marketStatus"] = "CLOSED"
        spec, _ = mod.build_spec(d)
        self.assertEqual(spec["stops_level_points"], 5)
        self.assertEqual((spec["trade_mode"], spec["tradable"]),
                         ("disabled", False))

    def test_fx_has_a_base_and_its_triple_day(self):
        d = json.loads(json.dumps(SILVER))
        d["instrument"].update(epic="USDCAD", symbol="USD/CAD",
                               type="CURRENCIES", currency="CAD")
        spec, _ = mod.build_spec(d)
        self.assertEqual((spec["asset_class"], spec["base_ccy"],
                          spec["quote_ccy"], spec["swap_3day_dow"]),
                         ("fx", "USD", "CAD", 4))

    def test_not_a_market(self):
        self.assertEqual(mod.build_spec({}), (None, None))
        self.assertEqual(mod.build_spec({"instrument": {}}), (None, None))


class TestPace(unittest.TestCase):
    def test_order_posts_keep_apart(self):
        p = mod._Pace()
        t0 = time.monotonic()
        for _ in range(3):
            p.take_order(2.0)
        self.assertGreaterEqual(time.monotonic() - t0, 2 * mod.ORDER_GAP)

    def test_logins_one_a_second(self):
        p = mod._Pace()
        p.take_login(0.0)
        with self.assertRaises(mod.CapitalError) as cm:
            p.take_login(0.2)
        self.assertEqual(cm.exception.code, "budget")
        self.assertFalse(cm.exception.sent)

    def test_the_demo_hour(self):
        p = mod._Pace(hour_cap=12)
        p.take_order(1.0)
        with p._lock:
            p._order_hour.extend([time.monotonic()] * 10)
            p._order_last = -1e9
        with self.assertRaises(mod.CapitalError) as cm:
            p.take_order(1.0)
        self.assertIn("an hour", cm.exception.message)

    def test_a_full_second_refuses_a_caller_that_cannot_wait(self):
        p = mod._Pace(per_second=2)
        p.take("read", 0.0)
        p.take("read", 0.0)
        with self.assertRaises(mod.CapitalError):
            p.take("read", 0.05)
        # flatten_all has a place of its own, and honours no pause
        p.block(5.0)
        p.take("flatten", 0.0)

    def test_background_leaves_room_while_an_order_is_in_hand(self):
        p = mod._Pace(per_second=8)
        p.in_hand(+1)
        for _ in range(5):
            p.take("read", 0.0)
        with self.assertRaises(mod.CapitalError):
            p.take("background", 0.05)
        p.take("write", 0.0)


class TestBarStore(unittest.TestCase):
    W = 60_000

    def store(self, now=None):
        s = mod._BarStore(self.W, now or 1_000_000 * self.W)
        return s

    def test_windows_join_without_a_hole_and_the_answer_is_oldest_first(self):
        s = self.store()
        top = s.top
        s.put((top - 996 * self.W, top),
              [[top - k * self.W, 1, 2, 0.5, 1.5, 1] for k in range(1, 997)],
              False, top + self.W)
        s.head_at = time.monotonic()
        w = s.take()
        self.assertEqual(w[1], top - 997 * self.W)
        s.put(w, [[t, 1, 2, 0.5, 1.5, 1] for t in range(
            w[0], w[1] + 1, self.W)], False, top + self.W)
        rows = s.newest(1500)
        self.assertEqual(len(rows), 1500)
        ts = [r[0] for r in rows]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(b - a == self.W for a, b in zip(ts, ts[1:])))

    def test_a_window_that_comes_back_full_is_split(self):
        s = self.store()
        s.head_at = time.monotonic()
        s.span = mod.BARS_SPAN * 4
        w = s.take()
        s.put(w, [], True, s.top)
        self.assertEqual(len(s.pending), 2)
        self.assertFalse(s.covered)
        a, b = s.pending
        self.assertEqual((a[1], b[0]), (w[1], w[0]))
        self.assertEqual(a[0] - b[1], self.W)

    def test_a_long_empty_stretch_is_the_start_of_history(self):
        s = self.store()
        s.head_at = time.monotonic()
        s.put((s.top - 996 * self.W, s.top), [[s.top - self.W, 1, 1, 1, 1,
                                                0]], False, s.top + self.W)
        for _ in range(12):
            w = s.take()
            if w is None:
                break
            s.put(w, [], False, s.top + self.W)
        self.assertTrue(s.done)
        self.assertIsNone(s.take())
        self.assertEqual(len(s.newest(10)), 1)

    def test_the_forming_bar_is_not_kept(self):
        s = self.store()
        now = s.top + 30_000
        s.put((s.top - 5 * self.W, s.top), [[s.top, 1, 1, 1, 1, 0],
                                            [s.top - self.W, 1, 1, 1, 1, 0]],
              False, now)
        self.assertNotIn(s.top, s.rows)
        self.assertIn(s.top - self.W, s.rows)


class TestRegistry(unittest.TestCase):
    def test_the_builtin_profile_is_pinned_to_demo(self):
        from brueconnect import Connector, registry
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as home:
            with mock.patch.dict(os.environ, {"BRUE_CONNECT_HOME": home,
                                              "CAPITALCOM_ENV": "live"}):
                prof = registry.load()["brokers"]["capitalcom"]
                self.assertTrue(prof["builtin"])
                self.assertTrue(prof["argv"][1].endswith(os.path.join(
                    "adapters", "capitalcom", "capitalcom_adapter.py")))
                self.assertEqual(prof["argv"][2:], ["--paper"])
                argv, e, sdir = registry.resolve("capitalcom")
                self.assertEqual(e["BRUE_BROKER_STATE"], sdir)
                # the flag wins over CAPITALCOM_ENV=live
                cfg = mod.resolve_environment(argv[2:], e)
                self.assertEqual((cfg["env"], cfg["mode"]), ("demo", "paper"))
                c = Connector.for_broker("capitalcom")
                try:
                    h = c.hello()
                finally:
                    c.close()
                    for pipe in (c.proc.stdin, c.proc.stdout):
                        pipe.close()
                self.assertEqual((h["mode"], h["adapter"]["name"]),
                                 ("paper", "capitalcom"))
                self.assertEqual(c.terms, TERMS)


class TestScrub(unittest.TestCase):
    def test_every_credential_and_token_is_taken_out(self):
        cfg = mod.resolve_environment([], env())
        c = mod.CapitalClient(cfg)
        c._cst, c._xst = "CSTtoken12345", "XSTtoken67890"
        text = "a %s b %s c %s d %s e %s" % (
            CREDS["BRUE_CRED_API_KEY"], CREDS["BRUE_CRED_IDENTIFIER"],
            CREDS["BRUE_CRED_PASSWORD"], "CSTtoken12345", "XSTtoken67890")
        out = c.scrub(text)
        for v in list(CREDS.values()) + ["CSTtoken12345", "XSTtoken67890"]:
            self.assertNotIn(v, out)


class TestScrubGaps(unittest.TestCase):
    """The secret scrub, in the cases where a scrub leaves a secret
    readable or runs slow.

    (a) a control character _one_line turns into a space but white space
    does not (NUL, DEL, the C0 and C1 controls), raw or (through a page)
    written as a character reference, is a gap in _hide;
    (b) a character that prints as nothing but is not category Cf (a
    Default_Ignorable_Code_Point) does not leave the secret whole;
    (c) markup whose own text holds '>' (a comment '<!-- > -->', a quoted
    attribute title='>') is not ended at that first '>';
    (d) a password that itself holds '<' is not cut up by the tag removal,
    which runs after the scrub;
    (e) the gap match does not backtrack quadratically on the password (it
    holds a space) against a page with a long white-space run."""

    KEY = CREDS["BRUE_CRED_API_KEY"]
    split = staticmethod(TestErrors.split)

    def client(self, **cfg):
        return adapter(**cfg).client

    def assertNoPiece(self, secret, text, n=6):
        for i in range(len(secret) - (n - 1)):
            self.assertNotIn(secret[i:i + n], text, repr(text))

    CONTROLS = ("\x00", "\x01", "\x08", "\x0e", "\x1b", "\x7f", "\x81",
                "\x90", "\x9f")

    def test_a_a_control_character_that_is_not_white_space_is_a_gap(self):
        key = self.KEY
        for ch in self.CONTROLS:
            written = self.split(key, ch, (0.3, 0.6))
            with self.subTest(ch=hex(ord(ch)), via="page"):
                e = self.client()._error(502, {}, ("<p>x %s</p>"
                                                   % written).encode())
                self.assertNoPiece(key, e.message)
                self.assertIn("[hidden]", e.message)
            with self.subTest(ch=hex(ord(ch)), via="json"):
                raw = json.dumps({"errorCode": "x " + written}).encode()
                e = self.client()._error(400, {}, raw)
                self.assertNoPiece(key, e.message)
                self.assertIn("[hidden]", e.message)
        for code in (1, 8, 127, 129, 0x90):
            for ref in ("&#%d;" % code, "&#x%x;" % code):
                written = self.split(key, ref, (0.3, 0.6))
                with self.subTest(ref=ref):
                    e = self.client()._error(
                        502, {}, ("<title>x %s</title>" % written).encode())
                    self.assertNoPiece(key, e.message)
                    self.assertIn("[hidden]", e.message)

    IGNORABLE = ("͏", "ᅟ", "ᅠ", "឴", "឵", "᠋",
                 "᠏", "ㅤ", "︀", "️", "ﾠ",
                 "\U000e0100", "\U000e01ef")

    def test_b_default_ignorable_characters_are_dropped(self):
        key = self.KEY
        for ch in self.IGNORABLE:
            self.assertNotEqual(mod.unicodedata.category(ch), "Cf", hex(
                ord(ch)))
            written = self.split(key, ch, (0.3, 0.6))
            with self.subTest(ch=hex(ord(ch)), via="page"):
                e = self.client()._error(502, {}, ("<p>x %s</p>"
                                                   % written).encode())
                self.assertNoPiece(key, e.message)
                self.assertIn("[hidden]", e.message)
            with self.subTest(ch=hex(ord(ch)), via="json"):
                raw = json.dumps({"errorCode": "x " + written}).encode()
                e = self.client()._error(400, {}, raw)
                self.assertNoPiece(key, e.message)
        self.assertEqual(mod._visible("a͏b︀c\U000e0100dㅤe"),
                         "abcde")

    def test_c_markup_whose_text_holds_a_gt_is_removed_whole(self):
        key = self.KEY
        for by in ("<!-- > -->", "<!-- a > b -->", '<a title=">">',
                   '<b title="x>y">', "<!--\n>\n-->"):
            written = self.split(key, by, (0.5,))
            for where in ("title", "body"):
                page = ("<html><head><title>x %s</title></head></html>"
                        if where == "title" else "<p>x %s</p>") % written
                with self.subTest(by=by, where=where):
                    e = self.client()._error(502, {}, page.encode())
                    self.assertNoPiece(key, e.message)
                    self.assertNotRegex(e.message, r"[<>]")
                    self.assertIn("[hidden]", e.message)

    def test_d_a_password_that_holds_lt_is_scrubbed_before_tags_removed(self):
        # Capital.com's password may hold any character, '<' among them: it
        # must still be taken out of a page that reflects it raw.
        for pw in ("abc<def<ghij", "a<b>cdefghij", "x<<<<yzabcd",
                   "Secret<12345678"):
            c = self.client(BRUE_CRED_PASSWORD=pw)
            for where in ("title", "body"):
                for form in (pw, "Rejected: " + pw + " post"):
                    page = ("<title>%s</title>" if where == "title"
                            else "<p>%s</p>") % form
                    with self.subTest(pw=pw, where=where, form=form):
                        e = c._error(502, {}, page.encode())
                        self.assertNoPiece(pw, e.message, 4)
                        self.assertNotRegex(e.message, r"[<>]")

    def test_e_the_gap_match_is_linear(self):
        # The password holds a space; a pattern that backtracks
        # quadratically on it against a page with a long white-space run
        # takes 6.1 s at 64 KiB.
        pw = CREDS["BRUE_CRED_PASSWORD"]  # "pass word with spaces"
        self.assertIn(" ", pw)
        for page in ("head " + " " * 65536 + "tail",
                     pw[:5] + " " * 65536,
                     " " * 65536 + pw):
            start = time.monotonic()
            mod._hide(page, (pw,))
            with self.subTest(page=page[:12]):
                self.assertLess(time.monotonic() - start, 0.1)
        start = time.monotonic()
        mod._page_words("<p>pass" + " " * 65536 + "y</p>", 502,
                        scrub=lambda t: mod._hide(t, (pw,)))
        self.assertLess(time.monotonic() - start, 0.1)


if __name__ == "__main__":
    unittest.main()
