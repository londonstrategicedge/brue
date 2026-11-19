"""OANDA adapter: the parts with no broker behind them.

Practice or live decided cold, the handshake (terms, credential form,
capabilities, broker_info), OANDA's refusals as SPEC codes, the catalog built
from OANDA's Instrument objects, times and numbers at the boundary, and the
events made from OANDA's transactions (fed in by hand). No network.

Run: python3 -m unittest tests.test_oanda_core -v
"""
import contextlib
import io
import json
import socket
import time
import unittest
from decimal import Decimal
from unittest import mock

from brueconnect import protocol
from brueconnect.adapter import AdapterError
from brueconnect.adapters.oanda import oanda_adapter as mod
from brueconnect.conformance.run import REQUIRED_SPEC_FIELDS

PRACTICE = "https://api-fxpractice.oanda.com"
PRACTICE_S = "https://stream-fxpractice.oanda.com"
_host = mod._host
LIVE = "https://api-fxtrade.oanda.com"
LIVE_S = "https://stream-fxtrade.oanda.com"



def _clear_urlsplit_cache():
    """Clear urlsplit's cache: it has one (functools.lru_cache) from Python
    3.11 on; on 3.10 there is none, and nothing to clear."""
    clear = getattr(mod.urllib.parse.urlsplit, "cache_clear", None)
    if clear is not None:
        clear()

class ColdMode(unittest.TestCase):
    """DEMO IS THE DEFAULT: practice unless --live or OANDA_ENV=live. `mode`
    is "paper" only on OANDA's practice hosts exactly, or loopback."""

    def env(self, argv=(), **environ):
        return mod.resolve_environment(list(argv), environ)

    def test_nothing_asked_is_practice(self):
        cfg = self.env()
        self.assertEqual((cfg["env"], cfg["mode"], cfg["rest_url"],
                          cfg["stream_url"]),
                         ("practice", "paper", PRACTICE, PRACTICE_S))

    def test_live_is_asked_for_by_flag_or_variable(self):
        for cfg in (self.env(["--live"]), self.env(OANDA_ENV="live"),
                    self.env(OANDA_ENV=" LIVE ")):
            self.assertEqual((cfg["env"], cfg["mode"], cfg["rest_url"],
                              cfg["stream_url"]),
                             ("live", "live", LIVE, LIVE_S), cfg["why"])

    def test_a_flag_beats_the_variable_and_both_flags_are_live(self):
        self.assertEqual(self.env(["--paper"], OANDA_ENV="live")["mode"],
                         "paper")
        self.assertEqual(self.env(["--live"], OANDA_ENV="practice")["mode"],
                         "live")
        self.assertEqual(self.env(["--paper", "--live"])["mode"], "live")

    def test_an_unreadable_variable_is_not_a_request_for_live(self):
        for named in ("sandbox", "fxtrade", "1", "yes"):
            cfg = self.env(OANDA_ENV=named)
            self.assertEqual((cfg["env"], cfg["mode"]), ("practice", "paper"),
                             named)
        self.assertIn("neither practice nor live",
                      self.env(OANDA_ENV="x")["why"])

    def test_every_combination_is_live_only_when_live_was_asked_for(self):
        for flags in ([], ["--paper"], ["--live"], ["--paper", "--live"]):
            for named in (None, "practice", "paper", "demo", "live", "junk"):
                environ = {"OANDA_ENV": named} if named else {}
                cfg = mod.resolve_environment(flags, environ)
                want = ("live" if "--live" in flags else "practice" if flags
                        else "live" if named == "live" else "practice")
                self.assertEqual(cfg["env"], want, (flags, named))
                self.assertEqual(cfg["mode"],
                                 "paper" if want == "practice" else "live")

    OFF_PRACTICE = (
        ("https://api-fxtrade.oanda.com", PRACTICE_S),
        (PRACTICE, "https://stream-fxtrade.oanda.com"),
        ("https://api-fxpractice.oanda.com.evil.example", PRACTICE_S),
        ("https://forwarder.example", PRACTICE_S),
        ("https://198.51.100.7", PRACTICE_S),
        # each host in its own role only
        ("https://stream-fxpractice.oanda.com", PRACTICE_S))

    def test_mode_follows_the_hosts_not_the_flag(self):
        # Asked for live, the endpoints are taken as given, and `mode` is
        # what the hosts are: live unless every host is practice or this
        # machine.
        for rest, stream in self.OFF_PRACTICE:
            cfg = self.env(["--live"], OANDA_REST_URL=rest,
                           OANDA_STREAM_URL=stream)
            self.assertEqual(cfg["mode"], "live", (rest, stream))
        for rest, stream in ((PRACTICE + ":443/", PRACTICE_S),
                             ("http://127.0.0.1:9", "http://127.0.0.1:10"),
                             ("http://localhost:9", "http://[::1]:10")):
            for flags in ([], ["--live"]):
                cfg = self.env(flags, OANDA_REST_URL=rest,
                               OANDA_STREAM_URL=stream)
                self.assertEqual(cfg["mode"], "paper", (flags, rest, stream))
        # asked for live, pointed at this machine: cannot move real money
        cfg = self.env(["--live"], OANDA_REST_URL="http://127.0.0.1:9",
                       OANDA_STREAM_URL="http://127.0.0.1:10")
        self.assertEqual((cfg["env"], cfg["mode"]), ("live", "paper"))

    def test_practice_never_takes_an_endpoint_off_practice(self):
        """The built-in profile is `--paper`, and the platform's own
        environment, which it inherits, may carry another tool's
        OANDA_REST_URL / OANDA_STREAM_URL for the live hosts: the `--paper`
        process would then reach api-fxtrade. On practice an endpoint that
        is neither OANDA's practice host nor this machine is ignored, and
        said."""
        for flags in ([], ["--paper"]):
            for rest, stream in self.OFF_PRACTICE:
                cfg = self.env(flags, OANDA_REST_URL=rest,
                               OANDA_STREAM_URL=stream, OANDA_ENV="practice")
                self.assertEqual(cfg["env"], "practice")
                self.assertIn(cfg["rest_url"], (PRACTICE, rest))
                self.assertEqual(_host(cfg["rest_url"]),
                                 "api-fxpractice.oanda.com", (rest, stream))
                self.assertEqual(_host(cfg["stream_url"]),
                                 "stream-fxpractice.oanda.com",
                                 (rest, stream))
                self.assertEqual(cfg["mode"], "paper", (flags, rest, stream))
                self.assertTrue(cfg["ignored"])
                self.assertIn("ignored", cfg["why"])
        # one of the two only: the other is still taken
        cfg = self.env(["--paper"], OANDA_REST_URL="https://api-fxtrade."
                       "oanda.com", OANDA_STREAM_URL="http://127.0.0.1:10")
        self.assertEqual((cfg["rest_url"], cfg["stream_url"], cfg["ignored"]),
                         (PRACTICE, "http://127.0.0.1:10", ["OANDA_REST_URL"]))
        # OANDA_ENV=live is overruled by --paper here too
        cfg = self.env(["--paper"], OANDA_ENV="live",
                       OANDA_REST_URL="https://api-fxtrade.oanda.com")
        self.assertEqual((cfg["env"], cfg["rest_url"], cfg["mode"]),
                         ("practice", PRACTICE, "paper"))

    def test_plain_http_off_this_machine_is_refused(self):
        cfg = self.env(OANDA_REST_URL="http://api-fxpractice.oanda.com")
        self.assertIn("must be https", cfg["fault"])
        self.assertEqual(self.env(OANDA_REST_URL="http://127.0.0.1:1")["fault"],
                         "")

    USER_PART = ("https://api-fxtrade.oanda.com@127.0.0.1:9",
                 "https://api-fxtrade.oanda.com\\@127.0.0.1:9",
                 "https://me:secret-pw@api-fxpractice.oanda.com",
                 "http://forwarder.example@[::1]:9",
                 "https://%31%32%37.0.0.1:9")

    def test_the_pool_connects_to_the_host_the_mode_was_judged_from(self):
        """The mode is judged by urlsplit's hostname, and the pool connects
        to that same hostname and port, whatever stands before it (urllib,
        used by another adapter, connects to the whole of it). Read from
        what http.client itself asks the socket for, nothing connected (the
        pool's host alone does not show it: http://[::1], with no port, is
        split by http.client into host ':' and port 1)."""
        connects = []

        def nowhere(address, *a, **k):
            connects.append(address)
            raise OSError("not connected: a test")

        for url in self.USER_PART + ("http://127.0.0.1:9", PRACTICE, LIVE):
            for flags in (["--paper"], ["--live"]):
                cfg = self.env(flags, OANDA_REST_URL=url,
                               OANDA_STREAM_URL=url)
                for key in ("rest_url", "stream_url"):
                    pool = mod._Pool(cfg[key], mod._Gate(0.0), None)
                    self.assertEqual(pool.host, _host(cfg[key]),
                                     (flags, url, key))
                self.assertEqual(cfg["rest_host"], _host(cfg["rest_url"]))
        both = (["--paper"], ["--live"])
        for url, host, port, flagsets in (
                ("http://[::1]", "::1", 80, both),
                ("https://[::1]", "::1", 443, both),
                ("http://[0:0:0:0:0:0:0:1]", "0:0:0:0:0:0:0:1", 80, both),
                ("http://[::1]:9", "::1", 9, both),
                ("http://127.0.0.1", "127.0.0.1", 80, both),
                ("http://localhost:9", "localhost", 9, both),
                (PRACTICE, "api-fxpractice.oanda.com", 443, both),
                (LIVE + ":443", "api-fxtrade.oanda.com", 443, (["--live"],))):
            for flags in flagsets:
                with self.subTest(url=url, flags=flags):
                    cfg = self.env(flags, OANDA_REST_URL=url)
                    self.assertEqual(cfg["fault"], "")
                    self.assertEqual(cfg["rest_host"], host)
                    pool = mod._Pool(cfg["rest_url"], mod._Gate(0.0),
                                     mod.ssl.create_default_context())
                    del connects[:]
                    with mock.patch("socket.create_connection", nowhere):
                        with self.assertRaises(mod.OandaError):
                            pool.connect(1.0, None)
                    self.assertEqual(connects, [(host, port)])

    def test_a_backslash_or_a_space_in_an_endpoint_is_refused(self):
        """No test pinned the backslash: the only one in USER_PART also has
        a user part. And a URL with a tab or a line break in it was taken
        as it read once urlsplit had dropped them."""
        for url, what in (
                ("https://localhost\\.api-fxtrade.oanda.com/v3",
                 "has a backslash before its path"),
                ("https://127.0.0.1\\api-fxtrade.oanda.com",
                 "has a backslash before its path"),
                ("http://127.0.0.1\t:9", "has a space, a tab or a line "
                 "break in it"),
                ("http://127.0.0.1:9/v\n3", "has a space, a tab or a line "
                 "break in it"),
                ("https://127.0.0.1:9x", "has a port that is not a number "
                 "from 1 to 65535"),
                ("https://127.0.0.1:65536", "has a port that is not a number "
                 "from 1 to 65535")):
            for name in ("OANDA_REST_URL", "OANDA_STREAM_URL"):
                with self.subTest(url=url, name=name):
                    cfg = self.env(["--live"], **{name: url})
                    self.assertTrue(cfg["fault"].startswith(
                        "%s %s" % (name, what)), cfg["fault"])
                    self.assertNotIn("api-fxtrade.oanda.com/", cfg["fault"])

    def test_brackets_in_the_host_are_refused_by_any_python(self):
        """Python 3.10.0-3.10.15 and 3.11.0-3.11.3 have no bracket check in
        urlsplit, so http://<name>[::1]:9 is judged '::1' there; on a newer
        Python urlsplit raises, and the adapter did not start at all. Either
        way it is refused by what the URL says, and the process keeps its
        own endpoint (emulated here by taking the check out where it exists;
        create=True: on a Python without it the parser is the older one
        already, and the attribute is made for this test only). urlsplit's
        cache, which it has from 3.11 on, is cleared around it
        (_clear_urlsplit_cache: on 3.10 there is none to clear)."""
        urls = ("http://api-fxtrade.oanda.com[::1]:9",
                "http://[::1]api-fxtrade.oanda.com:9", "http://[1:2:3]:9")
        for older in (False, True):
            patch = (mock.patch.object(mod.urllib.parse,
                                       "_check_bracketed_netloc",
                                       lambda netloc: None, create=True)
                     if older else contextlib.nullcontext())
            with patch:
                _clear_urlsplit_cache()
                for url in urls:
                    for flags, home in ((["--paper"], PRACTICE),
                                        (["--live"], LIVE)):
                        with self.subTest(older=older, url=url, flags=flags):
                            cfg = self.env(flags, OANDA_REST_URL=url)
                            if "OANDA_REST_URL" in cfg["ignored"]:
                                self.assertEqual(cfg["fault"], "")
                            else:
                                self.assertIn("OANDA_REST_URL has brackets",
                                              cfg["fault"])
                            self.assertEqual(cfg["rest_url"], home)
                            a = mod.OandaAdapter(argv=flags, environ={
                                "BRUE_CRED_TOKEN": "t" * 24,
                                "OANDA_REST_URL": url}, out=io.StringIO())
                            self.assertEqual(a.client.rest.host, _host(home))
            _clear_urlsplit_cache()

    def test_a_refused_endpoint_is_never_reached_nor_its_stand_in(self):
        """With an endpoint refused, the process keeps its environment's own
        in its place (below), and reaches neither: the client refuses every
        request before anything connects, whatever path asks it, not only
        the session's opening (read from what http.client would ask the
        socket for: nothing)."""
        cfg = self.env(["--live"], BRUE_CRED_TOKEN="t" * 24,
                       OANDA_REST_URL="https://api-fxtrade.oanda.com@"
                                      "127.0.0.1:9")
        self.assertTrue(cfg["fault"])
        client = mod.OandaClient(cfg)
        connects = []

        def nowhere(address, *a, **k):
            connects.append(address)
            raise OSError("not connected: a test")

        with mock.patch("socket.create_connection", nowhere):
            with self.assertRaises(mod.OandaError) as cm:
                client.call("GET", "/v3/accounts")
            with self.assertRaises(mod.OandaError) as cs:
                client.open_stream("/v3/accounts/x/pricing/stream")
        self.assertEqual(connects, [])
        for e in (cm.exception, cs.exception):
            self.assertEqual((e.code, e.sent), ("endpoint", False))
            err = mod.to_adapter_error(e)
            self.assertEqual((err.code, err.retryable),
                             ("x_oanda_endpoint", False))
            self.assertIn("OANDA_REST_URL has a user name", str(err))

    def test_an_endpoint_that_is_refused_is_not_used(self):
        """A refused endpoint is not kept beside the fault: the process
        keeps its environment's own (every call is refused with the fault,
        x_oanda_endpoint), and its mode is that endpoint's, as in the other
        adapter: --live with a user part naming this machine said paper."""
        cfg = self.env(["--live"], OANDA_REST_URL="https://api-fxtrade."
                       "oanda.com@127.0.0.1:9")
        self.assertIn("OANDA_REST_URL has a user name", cfg["fault"])
        self.assertEqual((cfg["rest_url"], cfg["rest_host"], cfg["mode"]),
                         (LIVE, "api-fxtrade.oanda.com", "live"))

    def test_an_endpoint_with_a_user_part_is_refused(self):
        """Nothing a user part could do here, as above; refused all the
        same: an OANDA endpoint has none, and one written with one is not
        what it seems. The fault quotes nothing of the URL."""
        for url in self.USER_PART:
            for flags in (["--paper"], ["--live"]):
                for name, key in (("OANDA_REST_URL", "rest_url"),
                                  ("OANDA_STREAM_URL", "stream_url")):
                    cfg = self.env(flags, **{name: url})
                    if name in cfg["ignored"]:
                        # practice, a host that is not this role's: not used
                        self.assertNotEqual(cfg[key], url, (flags, url))
                        self.assertEqual(flags, ["--paper"])
                    else:
                        self.assertIn(name, cfg["fault"], (flags, url))
                    self.assertNotIn("@", cfg["fault"], (flags, url))
                    self.assertNotIn("secret-pw", cfg["fault"])
        # a user part on a host practice does not take is ignored, as before
        cfg = self.env(["--paper"], OANDA_REST_URL="https://api-fxpractice."
                       "oanda.com@api-fxtrade.oanda.com")
        self.assertEqual((cfg["rest_url"], cfg["fault"]), (PRACTICE, ""))

    def test_the_token_is_read_from_the_platform_variable_only(self):
        self.assertEqual(self.env(BRUE_CRED_TOKEN=" tok-1 \n")["token"],
                         "tok-1")
        # No other variable is taken for it: a stray OANDA_* token in the
        # environment must never be used in its place.
        for name in ("OANDA_TOKEN", "OANDA_API_KEY", "OANDA_API_TOKEN",
                     "OANDA_ACCESS_TOKEN", "BRUE_CRED_API_TOKEN"):
            self.assertEqual(self.env(**{name: "x" * 20})["token"], "", name)

    def test_a_token_that_cannot_be_one_is_found_cold_and_never_quoted(self):
        for value in ("ab cd", "ab\tcd", "café", "bell\x07"):
            cfg = self.env(BRUE_CRED_TOKEN=value)
            self.assertTrue(cfg["cred_fault"], value)
            self.assertNotIn(value.strip(), cfg["cred_fault"])


def _answers(out, count, timeout=3.0):
    """The answers written to `out`, once `count` have come (or the time
    is up: a lane answers on a thread of its own)."""
    end = time.monotonic() + timeout
    while True:
        said = [json.loads(ln) for ln in out.getvalue().splitlines()]
        if len(said) >= count or time.monotonic() > end:
            return said
        time.sleep(0.02)


class ExactNames(unittest.TestCase):
    """A handler found by method.replace(".", "_") sends a name that is not
    a mutating method of the protocol to the reads lane, past the orders
    lane and _mutate's idempotency: "order_place" would POST an order on a
    connection the platform had not armed, and "flatten.all" would close
    the book. Only the protocol's exact names reach a handler (SPEC 1, 4);
    any other is unsupported."""

    ALIASES = ("order_place", "flatten.all", "position_close", "Order.Place",
               "order_cancel", "order_amend", "order_modify", "positions_list",
               "order.place ")
    HANDLERS = ("order_place", "flatten_all", "position_close", "order_cancel",
                "order_amend", "order_modify", "positions_list")

    def test_another_spelling_reaches_no_handler(self):
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--paper"], environ={
            "BRUE_CRED_TOKEN": "t" * 24,
            "OANDA_REST_URL": "http://127.0.0.1:9",
            "OANDA_STREAM_URL": "http://127.0.0.1:9"}, out=out)
        a._opened = True                  # no session to open: nothing to do
        ran = []
        for name in self.HANDLERS:
            setattr(a, "handle_" + name,
                    lambda p, n=name: ran.append(n) or {"closed": 0})
        for i, name in enumerate(self.ALIASES):
            a._dispatch("r%d" % i, name, {"client_id": "x%d" % i})
        said = _answers(out, len(self.ALIASES))
        self.assertEqual(ran, [])
        self.assertEqual(sorted(m["id"] for m in said),
                         sorted("r%d" % i for i in range(len(self.ALIASES))))
        for m in said:
            self.assertEqual(m["error"]["code"], "unsupported", m)
        # the exact name still reaches its handler (on the orders lane)
        a._dispatch("ok", "order.place", {"client_id": "x-ok"})
        _answers(out, len(self.ALIASES) + 1)
        self.assertEqual(ran, ["order_place"])


class _Answers:
    """A pool whose connections answer from a list (status, headers, body),
    writing down each request (host, method, url, headers). No network."""

    def __init__(self, scheme, host, port, answers, log):
        self.scheme, self.host, self.port, self.prefix = scheme, host, port, ""
        self.answers, self.log = list(answers), log

    def take(self, timeout, until, max_idle=None):
        return _AnswerConn(self), True

    def give(self, conn):
        pass


class _AnswerConn:
    sock, timeout = None, None

    def __init__(self, pool):
        self.pool = pool

    def request(self, method, url, body=None, headers=None):
        self.pool.log.append((self.pool.host, method, url, dict(headers)))

    def getresponse(self):
        answer = self.pool.answers.pop(0)
        if isinstance(answer, Exception):        # the answer was lost
            raise answer
        status, headers, body = answer
        resp = type("Resp", (), {"will_close": False, "status": status})()
        resp.read = lambda: body
        resp.getheader = lambda name, default=None: headers.get(name, default)
        return resp

    def close(self):
        pass


class Redirects(unittest.TestCase):
    """OANDA's live API answered two account summary reads HTTP 307 on
    2026-11-15. The rule: a request answered 307 or 308 is followed only if
    its method is GET, only once, and only to the same scheme, host and port
    as the endpoint it was sent to (its path and query may change), on REST
    and the streams alike. Nothing else is followed, and a refusal says
    which rule it met and names the target's host, never its path or query.
    A request that is not a GET answered 307 or 308 was not carried out,
    and is never sent again."""

    HERE = "/v3/accounts/101-001-1-001/summary"

    def client(self, flags=("--live",), **environ):
        env = {"BRUE_CRED_TOKEN": "t" * 24,
               "OANDA_REST_URL": "http://127.0.0.1:9",
               "OANDA_STREAM_URL": "http://127.0.0.1:10"}
        env.update(environ)
        return mod.OandaClient(mod.resolve_environment(list(flags), env))

    def refused(self, c, pool, location, status=307, hops=0):
        with self.assertRaises(mod.OandaError) as cm:
            c._follow(pool, self.HERE, "GET", status, location, hops)
        e = cm.exception
        self.assertEqual((e.status, e.code), (status, "redirect"))
        self.assertIn("not followed", e.message)
        for part in (self.HERE, "/v3", "?", "evil-path"):
            self.assertNotIn(part, e.message)       # never its path or query
        return e.message

    def test_a_read_is_followed_only_on_its_own_endpoint(self):
        c = self.client()
        for loc, tail in (
                ("/v3/accounts/x/summary", "/v3/accounts/x/summary"),
                ("http://127.0.0.1:9/v3/x", "/v3/x"),
                ("http://127.0.0.1:9/v3/x?a=1&b=2", "/v3/x?a=1&b=2"),
                ("HTTP://127.0.0.1:9/v3/y", "/v3/y"),
                ("summary2", "/v3/accounts/101-001-1-001/summary2")):
            for status in (307, 308):
                with self.subTest(location=loc, status=status):
                    self.assertEqual(c._follow(c.rest, self.HERE, "GET",
                                               status, loc, 0), tail)
        for loc, names in (
                ("https://api-fxtrade.oanda.com/evil-path", "api-fxtrade.oanda.com"),
                ("https://api-fxpractice.oanda.com/evil-path",
                 "api-fxpractice.oanda.com"),
                ("https://oanda.com/evil-path", "oanda.com"),
                ("http://localhost:9/evil-path", "localhost"),
                ("https://127.0.0.1:9/evil-path", "https"),
                ("http://127.0.0.1:10/evil-path", "port 10"),
                ("http://127.0.0.1/evil-path", "port 80"),
                ("ftp://127.0.0.1:9/evil-path", "ftp")):
            with self.subTest(location=loc):
                said = self.refused(c, c.rest, loc)
                self.assertIn(names, said)
                self.assertIn("scheme, host and port", said)
        for loc in ("https://api-fxtrade.oanda.com@127.0.0.1:9/evil-path",
                    "http://user:pw@127.0.0.1:9/evil-path",
                    "http://evil.example\\@127.0.0.1:9/evil-path",
                    "http://127.0.0.1:9\t/evil-path", "", "  ", None):
            with self.subTest(location=loc):
                said = self.refused(c, c.rest, loc)
                self.assertIn("no address that can be followed", said)
                for part in ("evil.example", "user:", "pw@", "\t",
                             "api-fxtrade"):
                    self.assertNotIn(part, said)

    def test_practice_is_never_sent_to_another_host(self):
        """A practice client answered 307 toward OANDA's live host is not
        sent there, its token with it. And the REST host's read is not sent
        to the stream host either, nor the stream to the REST host."""
        c = mod.OandaClient(mod.resolve_environment(
            ["--paper"], {"BRUE_CRED_TOKEN": "t" * 24}))
        self.assertEqual((c.rest.host, c.stream.host),
                         ("api-fxpractice.oanda.com",
                          "stream-fxpractice.oanda.com"))
        for pool, loc, names in (
                (c.rest, "https://api-fxtrade.oanda.com/evil-path",
                 "api-fxtrade.oanda.com"),
                (c.rest, "https://stream-fxpractice.oanda.com/evil-path",
                 "stream-fxpractice.oanda.com"),
                (c.stream, "https://api-fxpractice.oanda.com/evil-path",
                 "api-fxpractice.oanda.com"),
                (c.stream, "https://stream-fxtrade.oanda.com/evil-path",
                 "stream-fxtrade.oanda.com"),
                (c.rest, "https://api-fxpractice.oanda.com:8443/evil-path",
                 "port 8443"),
                (c.rest, "http://api-fxpractice.oanda.com/evil-path", "http")):
            with self.subTest(pool=pool.host, location=loc):
                said = self.refused(c, pool, loc)
                self.assertIn(names, said)
        self.assertEqual(c._follow(
            c.rest, self.HERE, "GET", 308,
            "https://api-fxpractice.oanda.com:443/v3/accounts/x", 0),
            "/v3/accounts/x")

    def test_a_read_is_followed_once_with_its_headers(self):
        c = self.client()
        log = []
        c.rest = _Answers("http", "127.0.0.1", 9, [
            (307, {"Location": "/v3/accounts/x?y=1"}, b""),
            (200, {}, b'{"account": {"id": "x"}}')], log)
        self.assertEqual(c.call("GET", self.HERE), {"account": {"id": "x"}})
        self.assertEqual([(h, m, u) for h, m, u, _ in log],
                         [("127.0.0.1", "GET", self.HERE),
                          ("127.0.0.1", "GET", "/v3/accounts/x?y=1")])
        self.assertEqual(log[0][3], log[1][3])           # the same headers
        # a second redirect is not followed
        log.clear()
        c.rest.answers = [(308, {"Location": "/v3/y"}, b""),
                          (307, {"Location": "/v3/z"}, b"")]
        with self.assertRaises(mod.OandaError) as cm:
            c.call("GET", "/v3/x")
        e = cm.exception
        self.assertEqual((e.status, e.code), (307, "redirect"))
        self.assertIn("followed once", e.message)
        self.assertIn("127.0.0.1", e.message)
        self.assertEqual(len(log), 2)
        err = mod.to_adapter_error(e)
        self.assertEqual((err.code, err.like, err.retryable),
                         ("x_oanda_redirected", "internal", False))

    def test_another_redirect_of_a_read_is_not_followed(self):
        for status in (300, 301, 302, 303, 304, 305, 306):
            with self.subTest(status=status):
                c = self.client()
                log = []
                c.rest = _Answers("http", "127.0.0.1", 9, [
                    (status, {"Location": "/v3/y"}, b""),
                    (200, {}, b"{}")], log)
                with self.assertRaises(mod.OandaError) as cm:
                    c.call("GET", "/v3/x")
                e = cm.exception
                self.assertEqual((e.status, e.code), (status, "redirect"))
                self.assertIn("only a GET answered 307 or 308", e.message)
                self.assertIn("127.0.0.1", e.message)
                self.assertEqual(len(log), 1)

    def test_a_request_that_is_not_a_get_is_never_followed(self):
        """Decided by the call's kind, the level-keeping PUT (kind
        background) answered 307 would be followed and sent again, with its
        body. Whatever its kind, a request that is not a GET is sent once:
        answered 307 or 308 it was not carried out, after another 3xx its
        fate is not known."""
        for kind in ("background", "write", "flatten", "read"):
            for status, code in ((307, "redirect"), (308, "redirect"),
                                 (301, "lost"), (302, "lost"), (303, "lost")):
                with self.subTest(kind=kind, status=status):
                    c = self.client()
                    log = []
                    c.rest = _Answers("http", "127.0.0.1", 9, [
                        (status, {"Location": "/v3/accounts/x/trades/7/"
                                              "orders"}, b""),
                        (200, {}, b"{}")], log)
                    with self.assertRaises(mod.OandaError) as cm:
                        c.call("PUT", "/v3/accounts/x/trades/7/orders",
                               body={"stopLoss": {"price": "1.10000"}},
                               kind=kind)
                    e = cm.exception
                    self.assertEqual((e.status, e.code, e.sent),
                                     (status, code, True))
                    self.assertEqual(len(log), 1)
                    self.assertIn("127.0.0.1", e.message)
                    self.assertNotIn("/v3", e.message)
                    if code == "redirect":
                        said = str(mod.to_adapter_error(e))
                        self.assertIn("never", said)
                        if kind not in ("background", "flatten"):
                            self.assertIn("not carried out", said[:80])
                    else:
                        # A change answered 301, 302 or 303 says "a redirect
                        # to ..., which is not followed" together with
                        # "OANDA answered HTTP 303".
                        self.assertIn("OANDA answered HTTP %d" % status,
                                      e.message)
                        self.assertIn("a redirect to", e.message)

    # -- a request that is not a GET, and a Location as it is written --------

    def test_a_change_of_the_adapters_own_jobs_has_words_of_its_own(self):
        """The level-keeping PUT and flatten_all's closes are not refused in
        the words for a platform's order ("... nor sent again: send it again
        as a new request ..."), which their jobs would then follow with
        "; tried again in 5 s" and ". Ask again.". A change made by one of
        the adapter's own jobs is said in a clause that ends nothing: its
        job says what comes next."""
        for kind in ("background", "flatten"):
            for status in (307, 308):
                with self.subTest(kind=kind, status=status):
                    c = self.client()
                    log = []
                    c.rest = _Answers("http", "127.0.0.1", 9, [
                        (status, {"Location": "/v3/x"}, b"")], log)
                    with self.assertRaises(mod.OandaError) as cm:
                        c.call("PUT", "/v3/accounts/x/trades/7/orders",
                               body={"stopLoss": {"price": "1.1"}},
                               kind=kind)
                    said = cm.exception.message
                    self.assertEqual(said, "OANDA answered HTTP %d, a "
                                     "redirect to 127.0.0.1, the endpoint "
                                     "itself, and a redirect is never "
                                     "followed for a change" % status)
                    self.assertEqual(len(log), 1)
        # an order, a close, a cancel or an amend the platform sent: as before
        c = self.client()
        c.rest = _Answers("http", "127.0.0.1", 9, [
            (307, {"Location": "/v3/x"}, b"")], [])
        with self.assertRaises(mod.OandaError) as cm:
            c.call("POST", "/v3/accounts/x/orders", body={}, kind="write")
        self.assertTrue(cm.exception.message.startswith(
            "It was not carried out: OANDA answered HTTP 307"))
        self.assertIn("send it again as a new request",
                      cm.exception.message)

    def test_a_request_that_is_not_a_get_is_sent_once_whatever_its_kind(self):
        """With retries decided by kind, the level-keeping PUT (kind
        background) would be sent twice after a 502, 503 or 504, or after an
        answer that was lost or could not be read. By method alone: a
        request that is not a GET is sent once by the HTTP layer, whatever
        its kind."""
        import http.client
        for kind in ("background", "read", "write", "flatten"):
            for answer in ((502, {}, b"<title>Bad Gateway</title>"),
                           (503, {}, b"{}"), (504, {}, b""),
                           http.client.RemoteDisconnected("no answer"),
                           ConnectionResetError("reset"),
                           http.client.IncompleteRead(b"{")):
                with self.subTest(kind=kind, answer=answer):
                    c = self.client()
                    log = []
                    c.rest = _Answers("http", "127.0.0.1", 9, [
                        answer, (200, {}, b"{}")], log)
                    with self.assertRaises(mod.OandaError):
                        c.call("PUT", "/v3/accounts/x/trades/7/orders",
                               body={"stopLoss": {"price": "1.1"}},
                               kind=kind)
                    self.assertEqual(len(log), 1, log)
        # a GET is asked again, whatever its kind
        for kind in ("background", "read", "flatten"):
            with self.subTest(get=kind):
                c = self.client()
                log = []
                c.rest = _Answers("http", "127.0.0.1", 9, [
                    (502, {}, b""), (200, {}, b'{"ok": 1}')], log)
                self.assertEqual(c.call("GET", "/v3/x", kind=kind),
                                 {"ok": 1})
                self.assertEqual(len(log), 2)

    def test_the_clause_of_a_change_keeps_the_sign_of_a_cut(self):
        """_clause does not strip the "..." off a message that was cut (an
        errorMessage cut at 200, a page title at 80), or flatten_all's and
        the levels job's words would end in a cut word with no sign of the
        cut ("...respond t" then "; tried again in 5 s"). A trailing full
        stop is dropped (it is a clause of a longer line), but a cut is
        kept."""
        for msg, want in (
                ("the market could not respond t...",
                 "the market could not respond t..."),
                ("...liquidity has ret...", "...liquidity has ret..."),
                ("a documented reason.", "a documented reason"),
                ("INSUFFICIENT_MARGIN", "INSUFFICIENT_MARGIN"),
                ("ended with a stop . ", "ended with a stop"),
                ("", "HTTP 502")):
            with self.subTest(msg=msg):
                self.assertEqual(mod._clause(mod.OandaError(502, msg)), want)

    def test_call_docstring_matches_its_429_rule(self):
        """A request that is not a GET answered 429 is paused and sent once
        more (OANDA documents a 429 as a refusal it did not carry out).
        call()'s docstring says which rule was chosen -- the 429 retry --
        and the behaviour matches it."""
        c = self.client()
        log = []
        c.rest = _Answers("http", "127.0.0.1", 9, [
            (429, {}, b'{"errorMessage": "Rate limit"}'),
            (200, {}, b"{}")], log)
        self.assertEqual(c.call("PUT", "/v3/accounts/x/trades/7/orders",
                                body={"stopLoss": {"price": "1.1"}},
                                kind="flatten"), {})
        self.assertEqual(len(log), 2)        # sent once more after the 429
        doc = mod.OandaClient.call.__doc__
        self.assertIn("429", doc)
        self.assertNotIn("never sent twice", doc)

    # Locations a reader that follows WHATWG (a browser) takes to another
    # host, or that are not ASCII, each read as written: refused, before
    # they are joined to the endpoint (urljoin reads them as odd paths on the
    # endpoint's own host).
    NOT_PLAIN = (
        ("/\\api-fxtrade.oanda.com/x", "backslash"),
        ("\\\\api-fxtrade.oanda.com\\v3", "backslash"),
        ("/v3/accounts\\x", "backslash"),
        ("http://127.0.0.1:9\\@api-fxtrade.oanda.com/x", "backslash"),
        ("https:///api-fxtrade.oanda.com/x", "no host"),
        ("https:////api-fxtrade.oanda.com/x", "no host"),
        ("http:///v3/x", "no host"), ("///api-fxtrade.oanda.com/x",
                                      "no host"),
        ("//", "no host"),
        # An authority with only a port, or only a user part, has no host:
        # refused as "no host", not as "a port that is not a number" or "a
        # user name before its host".
        ("//:9/x", "no host"), ("//@", "no host"),
        ("//@/x", "no host"), ("//:9", "no host"),
        ("https://:443/x", "no host"), ("//user@:9/x", "no host"),
        ("/v3/accounts/r\xe9sum\xe9", "not ASCII"),
        ("/v3/accounts/\xc3\xa9t\xc3\xa9", "not ASCII"),
        ("http://\u0435xample.com/v3/x", "not ASCII"),
    )

    def test_a_location_that_is_not_a_plain_url_is_refused_as_written(self):
        """It is refused naming only the endpoint's host, never what the
        Location holds."""
        for flags, host in ((("--live",), "127.0.0.1"),
                            (("--paper",), "api-fxpractice.oanda.com")):
            c = (self.client(flags) if host == "127.0.0.1" else
                 mod.OandaClient(mod.resolve_environment(
                     list(flags), {"BRUE_CRED_TOKEN": "t" * 24})))
            for loc, why in self.NOT_PLAIN:
                with self.subTest(location=loc, endpoint=host):
                    said = self.refused(c, c.rest, loc)
                    self.assertIn("not a plain URL", said)
                    self.assertIn(why, said)
                    self.assertIn(host, said)
                    for part in ("fxtrade", "xample", "\\", "sum",
                                 "\xe9", "\xc3", "\u0435"):
                        self.assertNotIn(part, said)
        # what is plain is followed as before
        c = self.client()
        for loc, tail in (("//127.0.0.1:9/v3/x", "/v3/x"),
                          ("http:/v3/y", "/v3/y"),
                          ("summary2", "/v3/accounts/101-001-1-001/summary2")):
            with self.subTest(followed=loc):
                self.assertEqual(c._follow(c.rest, self.HERE, "GET", 307,
                                           loc, 0), tail)

    def test_a_character_that_cannot_be_printed_is_said_so(self):
        """A character that is not printable is not said to be "a space, a
        tab or a line break"."""
        c = self.client()
        for loc, words in (
                ("/v3/a\x7fb", "a character in it that cannot be printed"),
                ("/v3/a\xadb", "a character in it that cannot be printed"),
                ("/v3/a\x00b", "a character in it that cannot be printed"),
                ("/v3/\xef\xbc\x91", "a character in it that cannot be "
                                         "printed"),
                ("/v3/a b", "a space, a tab or a line break"),
                ("/v3/a\tb", "a space, a tab or a line break"),
                ("/v3/a\x85b", "a space, a tab or a line break"),
                ("/v3/caf\xe9", "a character in it that is not ASCII")):
            with self.subTest(location=loc):
                said = self.refused(c, c.rest, loc)
                self.assertIn(words, said)

    def test_a_request_that_cannot_be_written_is_refused_and_not_reused(self):
        """A GET sent on to a path with a latin-1 letter does not escape
        call() as UnicodeEncodeError (which the platform would get as
        "internal: UnicodeEncodeError: ..."), with its connection neither
        closed nor given back. Any error building the request: that
        connection closed, never given back, and the request refused in
        plain words, not retryable."""
        import http.client
        for err in (UnicodeEncodeError("ascii", "/r\xe9", 2, 3, "ordinal "
                                       "not in range(128)"),
                    http.client.InvalidURL("can't contain control "
                                           "characters"),
                    ValueError("Invalid header value")):
            for method, kind in (("GET", "read"), ("PUT", "write")):
                with self.subTest(err=type(err).__name__, method=method):
                    c = self.client()
                    made, given = [], []

                    class Broken:
                        sock, timeout, closed = None, None, False

                        def request(self, *a, **k):
                            raise err

                        def close(self):
                            self.closed = True

                    class Pool(_Answers):
                        def take(self, timeout, until, max_idle=None):
                            made.append(Broken())
                            return made[-1], True

                        def give(self, conn):
                            given.append(conn)

                    c.rest = Pool("http", "127.0.0.1", 9, [], [])
                    with self.assertRaises(mod.OandaError) as cm:
                        c.call(method, "/v3/x", body=None if method == "GET"
                               else {}, kind=kind)
                    e = cm.exception
                    self.assertEqual((e.status, e.sent), (0, False))
                    self.assertEqual([m.closed for m in made], [True])
                    self.assertEqual(given, [])
                    said = mod.to_adapter_error(e)
                    self.assertFalse(said.retryable)
                    self.assertIn("could not be written", str(said))
                    self.assertIn("was not sent", str(said))
                    self.assertNotIn("\xe9", str(said))

    def test_a_stream_is_followed_once_on_its_own_host(self):
        """A stream is a GET: it is followed once to its own host, and
        nowhere else."""
        c = self.client()
        asked = []

        class Resp:
            def __init__(self, status, location=None):
                self.status, self.location = status, location

            def read(self):
                return b""

            def getheader(self, name, default=None):
                return self.location if name == "Location" else default

        def answering(*answers):
            answers = list(answers)

            class Conn:
                sock = None

                def request(self, method, url, body=None, headers=None):
                    asked.append((method, url, headers["Authorization"]))

                def getresponse(self):
                    return answers.pop(0)

                def close(self):
                    pass
            return lambda *a, **k: Conn()

        path = "/v3/accounts/x/pricing/stream"
        c.stream.connect = answering(
            Resp(307, "http://127.0.0.1:10" + path + "?instruments=EUR_USD"),
            Resp(200))
        conn, resp = c.open_stream(path, {"instruments": "GBP_USD"})
        self.assertEqual(resp.status, 200)
        self.assertEqual([u for _, u, _ in asked],
                         [path + "?instruments=GBP_USD",
                          path + "?instruments=EUR_USD"])
        self.assertEqual({a for _, _, a in asked}, {"Bearer " + "t" * 24})
        for answers, words in (
                ((Resp(307, "/v3/a"), Resp(308, "/v3/b"), Resp(200)),
                 "followed once"),
                ((Resp(307, "http://127.0.0.1:9" + path), Resp(200)),
                 "port 9"),
                ((Resp(302, path), Resp(200)), "only a GET answered 307"),
                ((Resp(307, "https://stream-fxtrade.oanda.com" + path),
                  Resp(200)), "stream-fxtrade.oanda.com")):
            with self.subTest(words=words):
                del asked[:]
                c.stream.connect = answering(*answers)
                with self.assertRaises(mod.OandaError) as cm:
                    c.open_stream(path)
                self.assertEqual(cm.exception.code, "redirect")
                self.assertIn(words, cm.exception.message)
                self.assertNotIn("/v3", cm.exception.message)
                self.assertEqual(len(asked), len(answers) - 1)


class Handshake(unittest.TestCase):
    def hello(self, **environ):
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--paper"], environ=environ, out=out)
        a._dispatch("h1", "hello", {"protocol": 1,
                                    "client": {"name": "t", "version": "1"}})
        return json.loads(out.getvalue().splitlines()[-1])["result"], a

    def test_hello_carries_the_terms_oanda_requires(self):
        h, _ = self.hello()
        self.assertEqual(h["terms"], {
            "data_local_only": True, "orders_by_hand_only": False,
            "summary": mod.TERMS_SUMMARY, "url": mod.TERMS_URL})
        self.assertEqual(mod.TERMS_URL, "https://legal.oanda.com/?code="
                         "api_licence_agreement&language=en")
        # what a platform acts on, through the SDK's own sanitiser
        clean = protocol.sanitize_terms(h["terms"])
        self.assertIs(clean["data_local_only"], True)
        self.assertIs(clean["orders_by_hand_only"], False)
        self.assertEqual(clean["summary"], mod.TERMS_SUMMARY)
        self.assertLessEqual(len(mod.TERMS_SUMMARY), protocol.TERMS_SUMMARY)
        for words in ("licensed to you alone", "stay on this computer",
                      "tested on practice", "watched while it runs"):
            self.assertIn(words, mod.TERMS_SUMMARY)

    def test_hello_carries_the_credential_form(self):
        h, _ = self.hello()
        self.assertEqual(h["credential_form"], [
            {"key": "token", "label": "API token", "type": "password"}])
        self.assertEqual(protocol.sanitize_credential_form(
            h["credential_form"]), h["credential_form"])

    def test_hello_is_cold_and_paper_on_practice(self):
        with mock.patch.object(socket, "create_connection",
                               side_effect=AssertionError("no network")):
            h, a = self.hello(BRUE_CRED_TOKEN="t" * 40)
        self.assertEqual(h["mode"], "paper")
        self.assertFalse(a._opened)
        self.assertEqual(h["adapter"], {"name": "oanda",
                                        "version": mod.VERSION})
        self.assertNotIn("t" * 40, json.dumps(h))

    def test_hello_is_live_on_live(self):
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--live"], environ={}, out=out)
        a._dispatch("h", "hello", {"protocol": 1})
        self.assertEqual(json.loads(out.getvalue())["result"]["mode"], "live")

    def test_capabilities_are_what_oanda_serves(self):
        h, _ = self.hello()
        c = h["capabilities"]
        o = c["orders"]
        self.assertTrue(c["flatten_all"])
        self.assertEqual(o["pending_types"], ["limit", "stop"])
        self.assertEqual(o["expiry"], ["gtc", "day", "gtd"])
        self.assertFalse(o["oco"])
        self.assertFalse(o["close_by"])
        self.assertFalse(o["reserves_margin"])
        self.assertTrue(o["trailing"] and o["brackets"] and o["modify"]
                        and o["partial_close"] and o["can_list"])
        self.assertTrue(c["data"]["quotes"] and c["data"]["bars"]
                        and c["data"]["history"] and c["data"]["operations"])
        self.assertTrue(c["accounts"]["multi"])
        self.assertNotIn("auth", c)

    def test_broker_info_survives_the_sanitiser_whole(self):
        h, a = self.hello()
        raw = a.broker_info()
        self.assertEqual(protocol.sanitize_broker_info(raw), raw)
        self.assertEqual(h["broker_info"]["display_name"], "OANDA")
        self.assertIn("FCA No: 542574", h["broker_info"]["regulated_by"])

    def test_a_remembered_account_decides_the_cold_answer(self):
        with mock.patch.object(mod.OandaAdapter, "_load_state",
                               lambda self: self._accounts_cache.update({
                                   "101-004-1-002": {"currency": "USD",
                                                     "hedging": True}})):
            h, _ = self.hello(BRUE_ACCOUNT_ID="101-004-1-002")
            self.assertEqual((h["account_currency"], h["account_model"]),
                             ("USD", "hedging"))
            h, _ = self.hello(BRUE_ACCOUNT_ID="101-004-1-009")
            self.assertEqual((h["account_currency"], h["account_model"]),
                             ("GBP", "netting"))


class Secrets(unittest.TestCase):
    def test_an_unexpected_exception_never_carries_the_token(self):
        tok = "abcdef0123456789-" + "9" * 16
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--paper"],
                             environ={"BRUE_CRED_TOKEN": tok}, out=out)
        a._opened = True

        def boom(p):
            raise RuntimeError("the header was Bearer " + tok)
        a.handle_account_get = boom
        # the SDK's own path, which formats the exception itself
        mod.Adapter._dispatch(a, "r1", "account.get", {})
        self.assertNotIn(tok, out.getvalue())
        self.assertIn("[hidden]", out.getvalue())

    def test_a_refusal_never_carries_the_token(self):
        tok = "abcdef0123456789-" + "9" * 16
        client = mod.OandaClient(mod.resolve_environment(
            [], {"BRUE_CRED_TOKEN": tok}))
        e = client._error(400, json.dumps({
            "errorMessage": "echoed: Bearer %s" % tok}).encode())
        self.assertNotIn(tok, str(mod.to_adapter_error(e)))


class Refusals(unittest.TestCase):
    def test_reasons_map_to_spec_codes(self):
        want = {
            "INSUFFICIENT_MARGIN": "insufficient_margin",
            "MARKET_HALTED": "market_closed",
            "INSTRUMENT_BID_HALTED": "market_closed",
            "UNITS_MINIMUM_NOT_MET": "invalid_qty",
            "UNITS_LIMIT_EXCEEDED": "invalid_qty",
            "UNITS_PRECISION_EXCEEDED": "invalid_qty",
            "CLOSE_TRADE_UNITS_EXCEED_TRADE_SIZE": "invalid_qty",
            # the fill price would trigger the level: the market moved
            # between the quote and the fill (the adapter refuses a level
            # already wrong for the prices it sent before sending)
            "STOP_LOSS_ON_FILL_LOSS": "requote",
            "TAKE_PROFIT_ON_FILL_LOSS": "requote",
            "LOSING_TAKE_PROFIT": "requote",
            "STOP_LOSS_ON_FILL_PRICE_PRECISION_EXCEEDED": "invalid_stops",
            "TRAILING_STOP_LOSS_ON_FILL_PRICE_DISTANCE_MINIMUM_NOT_MET":
                "invalid_stops",
            "TIME_IN_FORCE_GTD_TIMESTAMP_IN_PAST": "invalid_expiry",
            "ACCOUNT_LOCKED": "trade_restricted",
            "ACCOUNT_ORDER_CREATION_LOCKED": "trade_restricted",
            "INSTRUMENT_NOT_TRADEABLE": "trade_restricted",
            "INSTRUMENT_PRICE_UNKNOWN": "no_price",
            "CLIENT_ORDER_ID_ALREADY_EXISTS": "duplicate_client_id",
            "TAKE_PROFIT_ON_FILL_CLIENT_ORDER_ID_ALREADY_EXISTS":
                "duplicate_client_id",
            "ORDER_DOESNT_EXIST": "not_found",
            "TRADE_DOESNT_EXIST": "not_found",
            "BOUNDS_VIOLATION": "requote",
            "INSUFFICIENT_FUNDS": "insufficient_funds",
            "PRICE_PRECISION_EXCEEDED": "x_oanda_price_precision",
            "INSUFFICIENT_LIQUIDITY": "x_oanda_insufficient_liquidity",
            "FIFO_VIOLATION": "x_oanda_fifo",
            "SOMETHING_NEW": "x_oanda_something_new",
        }
        for reason, code in want.items():
            self.assertEqual(mod.map_reason(reason)[0], code, reason)
        self.assertEqual(mod.map_reason("INSUFFICIENT_LIQUIDITY")[1:],
                         ("requote", True))
        self.assertEqual(mod.map_reason("FIFO_VIOLATION")[1],
                         "trade_restricted")
        # every like is in the closed set; every plain code is too
        for reason in list(want) + ["INTERNAL_SERVER_ERROR"]:
            code, like, _ = mod.map_reason(reason)
            self.assertTrue(code in protocol.ERROR_CODES
                            or code.startswith("x_oanda_"), reason)
            if like:
                self.assertIn(like, protocol.ERROR_CODES)

    def err(self, status, body, **kw):
        e = mod.OandaError(status, body.get("errorMessage", ""),
                           mod.reject_reason(body), body, **kw)
        return mod.to_adapter_error(e, "practice", "api-fxpractice.oanda.com",
                                    "101-004-1-001")

    def test_http_statuses(self):
        # documented: troubleshooting-errors
        e = self.err(401, {"errorMessage": "Insufficient authorization to "
                                           "perform request."})
        self.assertEqual(e.code, "unauthorized")
        self.assertIn("practice", str(e))
        self.assertIn("OANDA TMS Brokers", str(e))
        for status in (401, 403):
            e = self.err(status, {"errorMessage": "The provided request was "
                                                  "forbidden."})
            self.assertEqual((e.code, e.like),
                             ("x_oanda_account_not_reachable", "unauthorized"))
        e = self.err(403, {"errorMessage": "Insufficient authorization to "
                                           "perform request."})
        self.assertEqual((e.code, e.like), ("x_oanda_forbidden",
                                            "trade_restricted"))
        e = self.err(429, {"errorMessage": "Rate limit"})
        self.assertEqual((e.code, e.retryable), ("rate_limited", True))
        e = self.err(503, {"errorMessage": "Service Unavailable"})
        self.assertEqual((e.code, e.retryable), ("internal", True))
        e = self.err(404, {"errorMessage": "The order ID specified does not "
                                           "exist"})
        self.assertEqual(e.code, "not_found")

    def test_a_reject_transaction_names_the_reason(self):
        body = {"orderRejectTransaction": {"type": "MARKET_ORDER_REJECT",
                                           "rejectReason":
                                               "UNITS_MINIMUM_NOT_MET"},
                "errorMessage": "The units specified is less than the "
                                "minimum", "errorCode": "SOMETHING_ELSE"}
        self.assertEqual(mod.reject_reason(body), "UNITS_MINIMUM_NOT_MET")
        e = self.err(400, body)
        self.assertEqual(e.code, "invalid_qty")
        self.assertIn("OANDA:", str(e))

    def test_local_codes(self):
        for code, want in (("flatten", "x_oanda_flattening"),
                           ("deadline", "internal"),
                           ("budget", "rate_limited")):
            e = mod.to_adapter_error(mod.OandaError(0, "x", code=code))
            self.assertEqual(e.code, want, code)
            self.assertTrue(e.retryable, code)


OANDA_EUR_USD = {
    # shaped as documented (primitives-df Instrument); values plausible
    "name": "EUR_USD", "type": "CURRENCY", "displayName": "EUR/USD",
    "pipLocation": -4, "displayPrecision": 5, "tradeUnitsPrecision": 0,
    "minimumTradeSize": "1", "maximumTrailingStopDistance": "1.00000",
    "minimumTrailingStopDistance": "0.00050", "maximumPositionSize": "0",
    "maximumOrderUnits": "100000000", "marginRate": "0.0333",
    "commission": {"commission": "0", "unitsTraded": "1",
                   "minimumCommission": "0"},
    "guaranteedStopLossOrderMode": "DISABLED",
    "financing": {"longRate": "-0.0371", "shortRate": "0.0098",
                  "financingDaysOfWeek": [
                      {"dayOfWeek": d, "daysCharged": n} for d, n in (
                          ("MONDAY", 1), ("TUESDAY", 1), ("WEDNESDAY", 3),
                          ("THURSDAY", 1), ("FRIDAY", 1), ("SATURDAY", 0),
                          ("SUNDAY", 0))]},
    "tags": [{"type": "ASSET_CLASS", "name": "CURRENCY"}]}


class Catalog(unittest.TestCase):
    def test_an_instrument_becomes_a_full_spec(self):
        spec, own = mod.build_instrument(OANDA_EUR_USD)
        for f in REQUIRED_SPEC_FIELDS:
            self.assertIn(f, spec)
        self.assertEqual((spec["symbol"], spec["name"], spec["asset_class"],
                          spec["base_ccy"], spec["quote_ccy"]),
                         ("EUR_USD", "EUR/USD", "fx", "EUR", "USD"))
        self.assertEqual((spec["digits"], spec["tick_size"], spec["lot_step"],
                          spec["min_qty"], spec["max_qty"],
                          spec["contract_size"]),
                         (5, 0.00001, 1.0, 1.0, 100000000.0, 1))
        self.assertAlmostEqual(spec["margin_rate"], 0.0333)
        self.assertAlmostEqual(spec["margin_maintenance_rate"], 0.01665)
        self.assertEqual(spec["swap_3day_dow"], 3)
        self.assertEqual(spec["swap_mode"], "pct_annual")
        self.assertAlmostEqual(spec["swap_long"], -3.71)
        self.assertEqual(spec["fees"]["mode"], "none")
        self.assertEqual(spec["stops_level_points"], 0)
        self.assertEqual(own["tick"], Decimal("0.00001"))
        self.assertEqual(own["min_trail"], Decimal("0.00050"))

    def test_forex_hours_are_new_york_and_cfds_publish_none(self):
        spec, _ = mod.build_instrument(OANDA_EUR_USD)
        s = spec["sessions"]
        self.assertTrue(all(w["tz"] == "America/New_York" for w in s))
        self.assertIn({"days": [7], "open": "17:05", "close": "24:00",
                       "tz": "America/New_York"}, s)
        self.assertIn({"days": [5], "open": "00:00", "close": "16:59",
                       "tz": "America/New_York"}, s)
        cfd = dict(OANDA_EUR_USD, name="UK100_GBP", type="CFD",
                   displayPrecision=1, tradeUnitsPrecision=1,
                   minimumTradeSize="0.1",
                   tags=[{"type": "ASSET_CLASS", "name": "INDEX"}])
        spec, own = mod.build_instrument(cfd)
        self.assertEqual(spec["sessions"], [])        # unknown, not shut
        self.assertEqual((spec["asset_class"], spec["base_ccy"],
                          spec["quote_ccy"], spec["lot_step"]),
                         ("index", None, "GBP", 0.1))
        tr = dict(OANDA_EUR_USD, name="USD_TRY")
        self.assertEqual(mod.build_instrument(tr)[0]["sessions"][0]["tz"],
                         "Europe/Berlin")
        gold = dict(OANDA_EUR_USD, name="XAU_USD", type="METAL")
        self.assertEqual(mod.build_instrument(gold)[0]["sessions"][0]["open"],
                         "18:05")

    def test_a_row_missing_what_is_needed_is_left_out(self):
        self.assertIsNone(mod.build_instrument({"name": "X"}))


class TimesAndNumbers(unittest.TestCase):
    def test_oanda_times(self):
        self.assertEqual(mod.to_ms("1731600000.123456789"), 1731600000123)
        self.assertEqual(mod.to_ms("1731600000"), 1731600000000)
        self.assertEqual(mod.to_ms("2024-11-14T16:00:00.123456789Z"),
                         1731600000123)
        self.assertEqual(mod.unix_s(1731600000123), "1731600000.123000000")
        self.assertIsNone(mod.to_ms("yesterday"))

    def test_gfd_ends_at_five_in_new_york_across_dst(self):
        # winter: 17:00 New York is 22:00 UTC
        t = mod.to_ms("2026-11-14T15:00:00Z")
        self.assertEqual(mod.next_new_york_five(t),
                         mod.to_ms("2026-11-14T22:00:00Z"))
        # after 17:00 New York: the next day's
        t = mod.to_ms("2026-11-14T23:00:00Z")
        self.assertEqual(mod.next_new_york_five(t),
                         mod.to_ms("2026-11-15T22:00:00Z"))
        # summer: 21:00 UTC
        t = mod.to_ms("2026-07-01T12:00:00Z")
        self.assertEqual(mod.next_new_york_five(t),
                         mod.to_ms("2026-07-01T21:00:00Z"))
        # the DST changes of 2026 (8 March, 1 November)
        self.assertEqual(mod.new_york_offset(mod.to_ms(
            "2026-03-08T06:59:00Z") / 1000), -5 * 3600)
        self.assertEqual(mod.new_york_offset(mod.to_ms(
            "2026-03-08T07:00:00Z") / 1000), -4 * 3600)
        self.assertEqual(mod.new_york_offset(mod.to_ms(
            "2026-11-01T05:59:00Z") / 1000), -4 * 3600)
        self.assertEqual(mod.new_york_offset(mod.to_ms(
            "2026-11-01T06:00:00Z") / 1000), -5 * 3600)

    def test_client_ids(self):
        self.assertEqual(mod.oanda_client_id("conf-0a1b2c3d4e5f"),
                         "conf-0a1b2c3d4e5f")
        for odd in ("a/b", "@x", "x" * 80, "with space", "café", ""):
            got = mod.oanda_client_id(odd) if odd else mod.oanda_client_id(
                "-")
            self.assertRegex(got, r"^brue-[0-9a-f]{32}$", odd)
        self.assertEqual(mod.oanda_client_id("a/b"),
                         mod.oanda_client_id("a/b"))

    def test_steps_and_formats(self):
        self.assertTrue(mod.step_ok(0.30000000000000004, 0.1))
        self.assertFalse(mod.step_ok(1 + 1 / 3, 1.0))
        self.assertEqual(mod._fmt(Decimal("1.2"), 5), "1.20000")
        self.assertEqual(mod._fmt(Decimal("100"), 0), "100")
        self.assertEqual(mod.snap(0.30000000000000004, Decimal("0.1")),
                         Decimal("0.3"))


class Events(unittest.TestCase):
    """OANDA's transactions in, the SPEC's events out: each once."""

    def adapter(self, hedging=False):
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--paper"], environ={}, out=out)
        a._hedging = hedging
        a._acct = "101-004-1-001"
        a._catalog = {"EUR_USD": mod.build_instrument(OANDA_EUR_USD)[0]}
        a._inst = {"EUR_USD": mod.build_instrument(OANDA_EUR_USD)[1]}
        a._contig = a._high = 100
        a._cids["k1"] = "k1"
        return a, out

    @staticmethod
    def events(out):
        return [json.loads(l) for l in out.getvalue().splitlines()
                if '"type": "event"' in l]

    def market_fill(self, base=101, cid="k1", units="1000", closes=None,
                    opened=True, reason="MARKET_ORDER"):
        create = {"id": str(base), "type": "MARKET_ORDER", "batchID":
                  str(base), "instrument": "EUR_USD", "units": units,
                  "timeInForce": "FOK", "reason": "CLIENT_ORDER",
                  "time": "1794677547.000000000"}
        if cid:
            create["clientExtensions"] = {"id": cid}
        fill = {"id": str(base + 1), "type": "ORDER_FILL", "batchID":
                str(base), "orderID": str(base), "instrument": "EUR_USD",
                "units": units, "fullVWAP": "1.10000", "reason": reason,
                "commission": "0", "time": "1794677547.100000000",
                "accountBalance": "100000.0000"}
        if opened:
            fill["tradeOpened"] = {"tradeID": str(base + 1), "units": units,
                                   "price": "1.10000",
                                   "clientExtensions": {"id": cid} if cid
                                   else {}}
        if closes:
            fill["tradesClosed"] = closes
        return [create, fill]

    def test_a_fill_is_accepted_then_filled_once(self):
        a, out = self.adapter()
        txs = self.market_fill()
        a._ingest(txs)
        a._ingest(txs)            # again, from the stream
        a._ingest(list(reversed(txs)))
        names = [e["event"] for e in self.events(out)]
        self.assertEqual(names, ["order.accepted", "order.filled"])
        f = self.events(out)[1]["data"]
        self.assertEqual((f["fill_id"], f["client_id"], f["order_id"],
                          f["position_id"], f["side"], f["qty"]),
                         ("102", "k1", "101", "EUR_USD", "buy", 1000.0))
        self.assertEqual(a._contig, 102)

    def test_out_of_order_ids_are_held_until_the_gap_closes(self):
        a, out = self.adapter()
        txs = self.market_fill(base=102)
        a._ingest([txs[1]])                  # 103 arrives before 101, 102
        self.assertEqual(a._contig, 100)
        self.assertEqual(a._high, 103)
        a._ingest([{"id": "101", "type": "CLIENT_CONFIGURE"}, txs[0]])
        self.assertEqual(a._contig, 103)

    def test_hedging_positions_are_trades(self):
        a, out = self.adapter(hedging=True)
        a._ingest(self.market_fill())
        f = self.events(out)[-1]["data"]
        self.assertEqual(f["position_id"], "102")

    def test_a_netting_reduce_is_one_close_per_fill(self):
        a, out = self.adapter()
        a._ingest(self.market_fill(base=101, units="1000"))
        a._cids["k2"] = "k2"
        a._ingest(self.market_fill(base=103, cid="k2", units="1500"))
        self.assertEqual(a._net("EUR_USD"), Decimal(2500))
        a._cids["k3"] = "k3"
        a._ingest(self.market_fill(
            base=105, cid="k3", units="-2000", opened=False,
            closes=[{"tradeID": "102", "units": "-1000", "price": "1.1010",
                     "realizedPL": "1.0", "financing": "-0.1"},
                    {"tradeID": "104", "units": "-1000", "price": "1.1010",
                     "realizedPL": "1.0", "financing": "0"}]))
        evs = self.events(out)
        closed = [e["data"] for e in evs if e["event"] == "position.closed"]
        self.assertEqual(len(closed), 1)
        c = closed[0]
        self.assertEqual((c["position_id"], c["qty"], c["realized_pnl"],
                          c["reason"], c["client_id"]),
                         ("EUR_USD", 2000.0, 2.0, "manual", "k1"))
        self.assertAlmostEqual(c["swap"], -0.1)
        # the fill first, the close after it (a platform that saw the close
        # first would take the fill for a new position)
        names = [e["event"] for e in evs]
        self.assertLess(names.index("order.filled", 4),
                        names.index("position.closed"))
        self.assertEqual(a._net("EUR_USD"), Decimal(500))

    def test_a_stop_loss_fill_closes_with_reason_sl(self):
        a, out = self.adapter(hedging=True)
        a._ingest(self.market_fill())
        a._ingest([{"id": "103", "type": "STOP_LOSS_ORDER", "batchID": "101",
                    "tradeID": "102", "price": "1.09000"}])
        self.assertEqual(a._trades["102"]["sl"], Decimal("1.09000"))
        a._ingest([{"id": "104", "type": "ORDER_FILL", "batchID": "104",
                    "orderID": "103", "instrument": "EUR_USD",
                    "units": "-1000", "fullVWAP": "1.09000",
                    "reason": "STOP_LOSS_ORDER", "time": "1794677600.0",
                    "tradesClosed": [{"tradeID": "102", "units": "-1000",
                                      "price": "1.09000",
                                      "realizedPL": "-10.0",
                                      "financing": "0"}]}])
        evs = self.events(out)
        self.assertEqual(evs[-1]["event"], "position.closed")
        self.assertEqual((evs[-1]["data"]["reason"],
                          evs[-1]["data"]["position_id"]), ("sl", "102"))
        # a stop's fill is no order.filled of the platform's
        self.assertEqual(sum(e["event"] == "order.filled" for e in evs), 1)
        self.assertNotIn("102", a._trades)

    def test_a_market_order_cancelled_in_its_own_batch_is_rejected(self):
        a, out = self.adapter()
        create = self.market_fill()[0]
        a._ingest([create, {"id": "102", "type": "ORDER_CANCEL",
                            "batchID": "101", "orderID": "101",
                            "reason": "INSUFFICIENT_MARGIN"}])
        evs = self.events(out)
        self.assertEqual([e["event"] for e in evs],
                         ["order.accepted", "order.rejected"])
        self.assertEqual(evs[-1]["data"]["code"], "insufficient_margin")

    def test_a_refusal_to_retry_is_no_end_and_is_not_announced(self):
        """As in the reference adapter: a retryable refusal (a requote, a
        thin book) is answered as an error and announces nothing."""
        a, out = self.adapter()
        for n, reason in ((101, "STOP_LOSS_ON_FILL_LOSS"),
                          (103, "INSUFFICIENT_LIQUIDITY")):
            create = self.market_fill(base=n)[0]
            a._ingest([create, {"id": str(n + 1), "type": "ORDER_CANCEL",
                                "batchID": str(n), "orderID": str(n),
                                "reason": reason}])
        names = [e["event"] for e in self.events(out)]
        self.assertNotIn("order.rejected", names)
        self.assertEqual(mod.map_reason("STOP_LOSS_ON_FILL_LOSS")[2], True)

    def test_resting_order_events(self):
        a, out = self.adapter()
        a._cids["L1"] = "L1"
        a._ingest([{"id": "101", "type": "LIMIT_ORDER", "batchID": "101",
                    "instrument": "EUR_USD", "units": "100", "price": "1.0",
                    "timeInForce": "GTD", "gtdTime": "1794700000.0",
                    "reason": "CLIENT_ORDER", "time": "1794677547.0",
                    "clientExtensions": {"id": "L1"}}])
        evs = self.events(out)
        self.assertEqual([e["event"] for e in evs],
                         ["order.accepted", "order.placed"])
        self.assertEqual(evs[1]["data"]["expires_at"], 1794700000000)
        # amended: a new id, the first one kept
        a._ingest([{"id": "102", "type": "ORDER_CANCEL", "batchID": "102",
                    "orderID": "101", "reason": "CLIENT_REQUEST_REPLACED",
                    "replacedByOrderID": "103"},
                   {"id": "103", "type": "LIMIT_ORDER", "batchID": "102",
                    "instrument": "EUR_USD", "units": "100", "price": "1.01",
                    "timeInForce": "GTD", "gtdTime": "1794700000.0",
                    "reason": "REPLACEMENT", "replacesOrderID": "101",
                    "clientExtensions": {"id": "L1"}}])
        evs = self.events(out)
        self.assertEqual(evs[-1]["event"], "order.amended")
        self.assertEqual((evs[-1]["data"]["order_id"],
                          evs[-1]["data"]["price"]), ("101", 1.01))
        a._ingest([{"id": "104", "type": "ORDER_CANCEL", "batchID": "104",
                    "orderID": "103", "reason": "TIME_IN_FORCE_EXPIRED"}])
        evs = self.events(out)
        self.assertEqual(evs[-1]["event"], "order.expired")
        self.assertEqual(evs[-1]["data"], {"order_id": "101",
                                           "client_id": "L1",
                                           "expiry": "gtd"})

    def test_money_without_a_trade_is_an_operation(self):
        a, out = self.adapter()
        a._ingest([{"id": "101", "type": "TRANSFER_FUNDS", "amount": "-50.0",
                    "fundingReason": "CLIENT_FUNDING",
                    "accountBalance": "99950.0", "time": "1794677547.0"},
                   {"id": "102", "type": "DAILY_FINANCING",
                    "financing": "-1.2", "accountBalance": "99948.8",
                    "time": "1794677548.0", "positionFinancings": []},
                   {"id": "103", "type": "DIVIDEND_ADJUSTMENT",
                    "instrument": "UK100_GBP", "dividendAdjustment": "2.5",
                    "accountBalance": "99951.3", "time": "1794677549.0"}])
        ops = [e["data"] for e in self.events(out)
               if e["event"] == "balance.operation"]
        self.assertEqual([(o["kind"], o["amount"]) for o in ops],
                         [("withdrawal", -50.0), ("other", -1.2),
                          ("dividend", 2.5)])
        self.assertEqual(ops[2]["reference"], "UK100_GBP")

    def test_a_margin_call_is_a_warning(self):
        a, out = self.adapter()
        a._summary = {"NAV": "100", "marginUsed": "150"}
        a._ingest([{"id": "101", "type": "MARGIN_CALL_ENTER"}])
        w = self.events(out)[-1]
        self.assertEqual(w["event"], "margin.warning")
        self.assertEqual(w["data"]["stage"], "call")
        self.assertAlmostEqual(w["data"]["margin_level"], 66.6666, 3)


class Bars(unittest.TestCase):
    def test_a_subscription_sends_each_closed_bar_once(self):
        out = io.StringIO()
        a = mod.OandaAdapter(argv=["--paper"], environ={}, out=out)
        a._catalog = {"EUR_USD": mod.build_instrument(OANDA_EUR_USD)[0]}
        a._inst = {"EUR_USD": mod.build_instrument(OANDA_EUR_USD)[1]}
        now = 1794677547000
        a.now_ms = lambda: now
        a.handle_bars_subscribe({"symbol": "EUR_USD", "timeframe": "1m"})
        start = now // 60000 * 60000
        rows = [[start - 60000, 1, 1, 1, 1, 1], [start, 2, 2, 2, 2, 2]]
        a._bar_fetch = lambda *x, **k: (rows, 2)
        now = start + 62000
        a._poll_bars()
        a._poll_bars()
        bars = [json.loads(l)["data"] for l in out.getvalue().splitlines()
                if '"bar"' in l]
        self.assertEqual([b["bar"][0] for b in bars], [start])


if __name__ == "__main__":
    unittest.main()
