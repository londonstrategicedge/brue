"""Platform side of the protocol: spawn an adapter process and talk to it.

Connector owns the subprocess, a reader thread, request/response matching,
and the event stream. connect() performs the SPEC section 7 reconciliation
sequence (hello, catalog, account, positions) and REPLACES local state with
the results: the broker is the source of truth. An adapter that answers that
connect `reconnect` (SPEC 6) is started again by it, once (reconcile).
"""
from __future__ import annotations

import collections
import subprocess
import threading
import time

from . import __version__, protocol

# Longest single protocol line the platform will assemble. A catalog for a
# venue with tens of thousands of instruments is the biggest honest message
# and lands far under this; anything past it is a runaway or a hostile feed,
# and the platform must not be the thing that dies. Chars, not bytes: stdout
# is opened in text mode.
MAX_LINE_CHARS = 16 * 1024 * 1024


def _possessive(name: str) -> str:
    """"Northwind's", but "Fixture Markets'": a name ending in s takes the
    apostrophe alone."""
    return name + ("'" if name[-1:] in ("s", "S") else "'s")


class ConnectorError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 like: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        # The words alone, without the code before them.
        self.message = message
        self.retryable = retryable
        # Only kept when it is readable and the code is genuinely an
        # extension; see protocol.sanitize_like.
        self.like = protocol.sanitize_like(code, like)

    @property
    def effective_code(self) -> str:
        """What a platform should BRANCH on; `code` is what it should SHOW.

        A broker's own code is the truth and belongs on screen. Behaviour
        cannot key off it, because a platform only knows the closed set, so
        an extension resolves to the standard code the broker nominated
        (SPEC 6). Where the broker nominated none, this is just `code`, and
        an unknown code is handled exactly as it was before `like` existed.
        """
        if self.code in protocol.ERROR_CODES:
            return self.code
        return self.like or self.code


class NotArmed(ConnectorError):
    """A mutating call was made to a broker that is not paper and not armed,
    or armed without the by_hand word its terms require (SPEC 3.3); or arm()
    itself was refused on such a broker.

    Client-side, raised before anything reaches the broker. It is a
    ConnectorError so existing handlers catch it, with its own type so a
    platform can offer the user the arming decision instead of showing an
    error.
    """

    def __init__(self, message: str):
        super().__init__("not_armed", message)


class Disconnected(Exception):
    """The adapter process ended or its pipe broke."""


class _Pending:
    __slots__ = ("done", "msg")

    def __init__(self):
        self.done = threading.Event()
        self.msg = None


class Connector:
    # Every method that can move money or positions. THE list: the arming
    # rule below is enforced against this set and nowhere else, so a method
    # added to the protocol without being classified here is a hole. Read-only
    # methods (account.get, orders.list, quotes.*, bars.*, history.*) are
    # absent deliberately: watching a live account is not trading it. The
    # rule matches exact names, which holds because request() sends no name
    # outside protocol.METHODS (another spelling of one is refused there).
    MUTATING = frozenset({
        "order.place", "order.modify", "order.amend", "order.cancel",
        "position.close", "position.close_by", "flatten_all",
    })
    # Names that are not methods of the protocol, which only probe_unknown()
    # sends: the conformance suite asks them to see an adapter refuse them
    # (SPEC 1). The second spells a mutating method another way and goes with
    # no parameters, so an adapter that maps it onto its order handler is
    # found without an order.
    UNKNOWN_PROBES = ("no.such.method", "order_place")
    # How much of the adapter's non-protocol stdout is kept (stray_lines).
    STRAY_LINES = 20
    STRAY_CHARS = 500

    def __init__(self, cmd: list[str], env: dict | None = None, stderr=None):
        self.cmd = cmd
        self.env = env
        # Where the adapter's stderr goes: None (the default) inherits ours,
        # see start(). A platform that wants to SHOW an adapter's last words
        # (a traceback from one that died before answering hello) passes a
        # file object, or subprocess.PIPE and then drains proc.stderr itself,
        # each process's (on_start: a connect may start the adapter again);
        # an undrained pipe blocks a chatty adapter, which is why inheriting
        # stays the default.
        self.stderr = stderr
        # What on_start() registered: each called with every process started.
        self._on_start: list = []
        self.proc: subprocess.Popen | None = None
        self._pending: dict[str, _Pending] = {}
        # client_id -> signature of the request that first used it
        self._client_ids: dict[str, str] = {}
        # The write lock: request() holds it from its checks to its write,
        # arm() while it judges and records, start() while it replaces the
        # process.
        self._lock = threading.Lock()
        # The reader's lock: held while one line from the adapter is applied
        # (an answer handed over, an event tracked), and by start() while it
        # clears what the last process said, so no line of that process lands
        # on what start() cleared. Taken after the write lock, never before.
        self._read_lock = threading.Lock()
        # The start lock: one start() at a time, from its read of the process
        # it replaces to the opening of the next (see start()). Taken before
        # the write lock, and never by a call that start() itself makes.
        self._start_lock = threading.Lock()
        self._rid = 0
        self._reader: threading.Thread | None = None
        self._handlers: dict[str, list] = collections.defaultdict(list)
        # (name, data) history. Sized for the quote/bar firehose, not for
        # order events: the paper adapter emits a quote per symbol per tick,
        # so the window drains in maxlen/(symbols*ticks_per_sec) seconds.
        # 50000 keeps the window longer than the time any conformance
        # scenario (s08/s09/s10 at --tick-ms 5) spends between mark() and
        # its scan; a smaller window overruns there.
        self.events = collections.deque(maxlen=50000)
        # Total events ever appended. Cursors are counted against THIS, not
        # deque indices: when the deque overflows its maxlen, indices shift
        # and an index-based cursor silently skips or re-reads events. The
        # offset math in wait_event stays exact for as long as the cursor's
        # events are still retained, and detects (rather than hides) the
        # fell-off-the-window case.
        self._events_total = 0
        self._event_cv = threading.Condition()
        self._last_seq = 0
        self.seq_gaps = 0
        # Lines dropped for exceeding MAX_LINE_CHARS. Counted, not silent:
        # a broker that trips this is malfunctioning and the platform should
        # be able to say so.
        self.oversize_lines = 0
        # The last lines the adapter wrote to stdout that are not protocol
        # messages: a print() left in an adapter, or a file that is not an
        # adapter at all. Skipped as before, and kept (the last
        # STRAY_LINES, each cut at STRAY_CHARS) so a platform can show them
        # beside the stderr tail when the adapter fails.
        self.stray_lines: collections.deque = collections.deque(
            maxlen=self.STRAY_LINES)
        self.alive = False
        # reconciled snapshots (SPEC 7)
        self.handshake: dict = {}
        self.broker_info: dict = {}
        # What the adapter says the user must type (SPEC 3.2), sanitised:
        # [{"key", "label", "type"}], empty until hello and for an adapter
        # that asks for nothing. Field names only, never a value.
        self.credential_form: list = []
        # What the broker's terms let a platform do (SPEC 3.3), sanitised:
        # always both flags, False until hello and for an adapter that
        # declares none. A platform ACTS on these; see SPEC 3.3.
        self.terms: dict = protocol.sanitize_terms(None)
        # Why this connection was armed, or None. Held HERE, on the
        # connection, so it cannot outlive it: a new Connector is unarmed and
        # a reconnect to real money is a new decision by construction, with no
        # cleanup for a caller to forget. A start() that replaces an earlier
        # process (a restart) clears it the same way. The first start() of a
        # Connector keeps an arming made before it, which is then judged at
        # each order by the terms that process's hello brings (see start()).
        self._armed_for: str | None = None
        # Whether that consent carried the platform's by_hand word (SPEC 3.3).
        # Checked at every mutating call against the terms known THEN, so an
        # arming made before hello, or before a hello that brought
        # orders_by_hand_only, never lets an order through without the word.
        self._armed_by_hand = False
        # Whether a handshake of our protocol has been read from the adapter
        # process this connection is on now. Until one has, the broker's mode
        # and terms are not known, so no arming can cover a mutating call
        # (SPEC 3: hello comes first): not before hello, nor after one that
        # timed out, nor after a start() that opened a new process.
        self._greeted = False
        # Which adapter process this is: start() counts each one, under the
        # write lock. A hello's answer is kept only if no start() came between
        # the hello and its answer (see hello()).
        self._generation = 0
        # The process (its generation) whose reconciliation last completed.
        # A process not reconciled yet is being connected: there, and only
        # there, an answer `reconnect` starts the adapter again (reconcile).
        self._reconciled = 0
        # The (generation, method) a call in several requests began on
        # (subscribe(), unsubscribe()): _send checks it under the write
        # lock, with the write, but only for a write of that same method, so
        # a bye, a hello or another call's request nested inside request()
        # on this thread (a platform that restarts and asks again) is
        # neither judged by it nor takes it away (_chunk, _send).
        self._began = threading.local()
        # what the broker last said about its own health, and whether its
        # catalog needs re-reading (SPEC 5); both maintained by _track
        self.session_state: dict = {"state": "connected", "detail": ""}
        # what this connection currently has streaming, in ask order
        self.subscribed: list[str] = []
        self.last_catalog_change: dict = {}
        self.catalog_stale = False
        self.catalog: dict[str, dict] = {}
        self.account: dict = {}
        self.orders: dict[str, dict] = {}
        # True when the broker has resting orders this view could not list
        # (see reconcile).
        self.orders_partial = False
        self.positions: dict[str, dict] = {}
        # Dedup for the at-least-once trade/close stream (SPEC 7). Bounded so a
        # long session cannot grow it without limit; the window is far larger
        # than any plausible in-flight re-emission.
        # Two separate windows: a fill_id and a close_id share no namespace,
        # so a close whose id happens to equal a past fill's id is never
        # silently dropped.
        self._seen_fills: set = set()
        self._fill_order: collections.deque = collections.deque()
        self._seen_closes: set = set()
        self._close_order: collections.deque = collections.deque()
        # True when an event changed a position in a way the cache cannot
        # compute safely (an add, a reduce, a flip, a partial close). The
        # cache is then deliberately behind the broker until the next
        # authoritative positions.list, which the platform re-reads (SPEC 7).
        self.positions_stale: bool = False

    def _apply_fill(self, data: dict) -> None:
        """Update the optimistic position cache from ONE fill, conservatively.

        Reconstructing a position by folding fill deltas is unreliable: the
        adapter also emits `position.closed` for closes and flips, and a
        reconnect replaces the book from `positions.list` (SPEC 7), so a naive
        aggregator double-counts a flip, doubles a replayed fill, or shows the
        wrong direction. The reliable source of position truth is
        `positions.list`, which the platform re-reads. So the cache does the
        ONE thing it can do safely, and defers everything else:

        - a fill that OPENS a position the cache does not yet know: show it
          at once, so a user sees the position the instant their order fills;
        - a fill on a position the cache already holds (an add, a reduce, a
          flip, or a post-reconnect replay): do NOT recompute; flag stale.

        This can never show a position LARGER or in the WRONG DIRECTION than
        the broker holds; at worst it is briefly behind on size, corrected by
        the next positions.list. Briefly-behind-but-safe beats
        wrong-but-confident.
        """
        pid = data.get("position_id")
        if not pid:
            return
        side = data.get("side")
        if side not in ("buy", "sell"):
            self.positions_stale = True            # cannot trust the direction
            return
        try:
            q = float(data.get("qty") or 0.0)
        except (TypeError, ValueError):
            self.positions_stale = True
            return
        if not (q > 0) or q != q:                  # <=0, NaN: not a real size
            self.positions_stale = True
            return
        if pid in self.positions:
            self.positions_stale = True            # add/reduce/flip/replay
            return
        try:
            px = float(data.get("price") or 0.0)
        except (TypeError, ValueError):
            px = 0.0
        self.positions[pid] = {
            "position_id": pid, "symbol": data.get("symbol"),
            "side": side, "qty": q, "avg_price": px,
            "client_id": data.get("client_id")}

    @staticmethod
    def _usable_key(value) -> bool:
        """Whether `value` can be used as a dict key or a dedup id without
        raising. SPEC's ids are strings; an adapter that sends a list or a
        dict would otherwise crash the reader partway through an event."""
        try:
            hash(value)
            return True
        except TypeError:
            return False

    @staticmethod
    def _dedup(seen: set, order: "collections.deque", key) -> bool:
        """Record an id in a bounded window; False if already seen (skip it)."""
        if key is None:
            return True
        if key in seen:
            return False
        seen.add(key)
        order.append(key)
        if len(order) > 20000:
            seen.discard(order.popleft())
        return True

    def _apply_close(self, data: dict) -> None:
        """A position.closed removes the position, or reduces it on a PARTIAL
        close. Popping unconditionally on a partial close would show the
        account FLAT while it still holds the remainder. If the closed qty is
        less than the cached qty, reduce; otherwise remove.
        """
        pid = data.get("position_id")
        cur = self.positions.get(pid)
        if cur is None:
            return
        try:
            closed = float(data.get("qty") or 0.0)
        except (TypeError, ValueError):
            closed = 0.0
        if closed <= 0 or closed >= cur.get("qty", 0) - 1e-9:
            self.positions.pop(pid, None)
        else:
            cur["qty"] = cur["qty"] - closed
            self.positions_stale = True

    # == lifecycle =======================================================
    @classmethod
    def for_broker(cls, name: str | None = None) -> "Connector":
        """A connector for a REGISTERED broker: the platform's front door.

        Everything after this line is identical for every broker; the name
        picked here is the only thing that changes. That is the contract.
        """
        from . import registry
        cmd, env, _ = registry.resolve(name)
        return cls(cmd, env=env)

    def start(self):
        """Spawn the adapter: a new process, whose handshake nobody has read.

        On a Connector that already had a process (start() after close(), or
        after the adapter died) this is a restart. The earlier process is
        ended first (close(), which says bye only to one still running), its
        reader is waited for, and the arming made for it is cleared: consent
        given while one process was there does not cover the next. (The first
        start() of a Connector is not a restart: an arming made before it is
        kept, and judged at each order by the terms its hello brings.)

        Either way nothing an earlier process said stands. Cleared: the
        handshake and all read from it (mode, terms, broker_info,
        credential_form); the catalog, the account, the positions and the
        resting orders, with their stale flags and the last catalog change;
        the fill and close windows (a new process numbers its fills afresh);
        its event sequence, its streams, its session state and the stray
        lines it printed (a new deque for the new process: the last one is
        not read, so a platform's own Connector that hides the attribute
        still starts). So no mutating call goes out until this process's
        hello has been read, that hello decides the mode and the terms (SPEC
        3, 3.3), and connect() reads the rest from the process itself (SPEC
        7). A line the last process (or a child of it) writes after this has
        begun is not passed on (see _read_loop), what the last process
        answered to a call made before this is not kept by that call either
        (_keep: reconcile, refresh_catalog, subscribe, unsubscribe), and a
        subscribe() or unsubscribe() that began before this writes none of
        its requests to the next process (_chunk).

        One start() at a time. Two threads restarting one Connector at the
        same moment are two restarts, one after the other: each reads,
        under the start lock, the process it replaces, and ends that one, so
        no process is left running that nobody ends.

        What stays, and why: the event history and its count (cursors from
        mark() keep counting, and what happened is still what happened); the
        client_id guard (it records what THIS platform sent under each id,
        and SPEC 2's rule holds across processes); the handlers registered
        with on() and on_start(); and the counts seq_gaps and
        oversize_lines, which tally the Connector's whole life.

        The process started is then handed to each function given to
        on_start(), on this thread, with no lock of the Connector held.
        """
        with self._start_lock:
            self._start()
            proc = self.proc
        for fn in list(getattr(self, "_on_start", None) or ()):
            try:
                fn(proc)
            except Exception:  # noqa: BLE001 - a platform's hook never
                pass           # fails a start

    def on_start(self, fn):
        """Register fn(proc), called with each adapter process this
        Connector starts, once it runs: the first, and every one started
        again, among them the one a connect starts when the adapter answers
        `reconnect` (reconcile). For a platform that took the adapter's
        stderr (stderr=subprocess.PIPE) and drains it itself: each process
        has a pipe of its own, and one nobody reads stops the adapter once
        it is full. Called on the thread that started the process, after
        start() has let go of its locks; what fn raises is not passed on."""
        self._on_start.append(fn)

    def _start(self):
        """start(), under the start lock."""
        # The process this start() replaces, read under the lock that every
        # start() holds: read before it, two starts at once both ended one
        # old process and each opened another, and the first one opened was
        # left running with nobody to end it, even after close().
        with self._lock:
            old = self.proc
        if old is not None:
            self.close()
            # Its reader passes on the last of what it said (up to the end of
            # its pipe). Bounded: a child of the adapter can hold the pipe
            # open after the adapter has ended, and then this reader is left
            # to end by itself, bound to the old pipe (see _read_loop).
            reader = self._reader
            if (reader is not None and reader.is_alive()
                    and reader is not threading.current_thread()):
                reader.join(timeout=2)
        with self._lock:
            # Under the write lock, which request() holds from its checks to
            # its write and arm() while it judges: a call on another thread
            # is judged wholly before this (and written to the old process)
            # or wholly after it, against what is cleared here. And under the
            # reader's lock: the old reader applies no line of the old
            # process once this has begun.
            with self._read_lock:
                self._generation += 1
                generation = self._generation
                self._greeted = False
                self.handshake = {}
                self.broker_info = {}
                self.credential_form = []
                self.terms = protocol.sanitize_terms(None)
                self.catalog = {}
                self.catalog_stale = False
                self.last_catalog_change = {}
                self.account = {}
                self.positions = {}
                self.positions_stale = False
                self.orders = {}
                self.orders_partial = False
                self._seen_fills, self._fill_order = set(), collections.deque()
                self._seen_closes, self._close_order = (
                    set(), collections.deque())
                self._last_seq = 0            # seq starts at 1 per session
                self.subscribed = []
                self.session_state = {"state": "connected", "detail": ""}
                # A new deque, assigned: emptying the last one read the
                # attribute, and a platform's Connector that hides it (one
                # written for a kit from before stray_lines) could not start
                # at all. The new process's reader appends to this one (it
                # reads the attribute as each line comes, under this lock).
                self.stray_lines = collections.deque(maxlen=self.STRAY_LINES)
                if old is not None:
                    self._disarm()
                    # A call still waiting on the old process is never
                    # answered by this one. Its reader answers them as it
                    # ends; this is for a reader that has not ended (a pipe
                    # some child still holds).
                    self._unblock()
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # stderr is INHERITED, not discarded. stdout is the protocol,
                # so stderr is where an adapter says everything else: what it
                # dropped from its own broker_info, why its login failed,
                # which venue code it could not map. Sending that to
                # /dev/null makes every one of those messages unreachable by
                # the person debugging the adapter, which is the opposite of
                # what they are for. No pipe, so a chatty adapter cannot fill
                # a buffer nobody drains. (A caller that asked for something
                # else in __init__ has taken that duty on.)
                stderr=self.stderr, text=True, bufsize=1, env=self.env,
                # SPEC 1: the lines are UTF-8. Bytes that are not ended the
                # reader with UnicodeDecodeError, and the good lines read
                # with them were lost; replaced, they are one line that is
                # not the protocol (stray_lines), and the reader goes on.
                encoding="utf-8", errors="replace")
            self.alive = True
            self._reader = threading.Thread(
                target=self._read_loop, args=(self.proc, generation),
                daemon=True)
            self._reader.start()

    def hello(self, timeout: float = 10.0) -> dict:
        """start + handshake, no reconciliation. Returns the handshake.

        Split out of connect() for one use: a connection picker that wants to
        show a broker's own name and mode without opening a session on it.
        `hello` carries no credentials in either direction (SPEC 8.5), so
        this is the cheapest truthful thing a platform can ask a broker.
        """
        if self.proc is None:
            self.start()
        # The process this hello goes to. If start() opens another before the
        # answer is judged or kept, the answer was the last process's.
        generation = self._generation
        hs = self.request("hello", {
            "protocol": protocol.PROTOCOL_VERSION,
            "client": {"name": "brueconnect", "version": __version__}},
            timeout=timeout)
        ours = (isinstance(hs, dict)
                # JSON's true is not a number, though Python's True == 1.
                # 1.0 is: SPEC 10 calls the version "the hello.protocol
                # number", and JSON has one kind of number.
                and not isinstance(hs.get("protocol"), bool)
                and hs.get("protocol") == protocol.PROTOCOL_VERSION)
        stale = ("the adapter was started again while its hello was being "
                 "answered; that answer was the last process's and is not "
                 "kept")
        # Whose answer this is comes first. An answer of the last process,
        # judged after a start() opened the next one, says nothing of the
        # next one: it is not kept, and if it is not of this protocol it ends
        # nothing either (it once ended the NEW process).
        with self._lock:
            if self._generation != generation:
                raise Disconnected(stale)
            if not ours:
                # SPEC 1: the platform disconnects. A handshake that is not
                # ours is never kept, so no later call can read a mode out of
                # it on terms that were never read, whatever order the caller
                # armed and called in.
                self.alive = False
                proc = self.proc
        if not ours:
            # No bye: the adapter does not speak our protocol. Only the
            # process this hello went to is ended, whatever start() does on
            # another thread meanwhile.
            self._end(proc)
            raise ConnectorError(
                "unsupported", "protocol version mismatch"
                if isinstance(hs, dict)
                else "the adapter's hello did not answer with an object")
        broker_info = protocol.sanitize_broker_info(hs.get("broker_info"))
        credential_form = protocol.sanitize_credential_form(
            hs.get("credential_form"))
        terms = protocol.sanitize_terms(hs.get("terms"))
        with self._lock:
            if self._generation != generation:
                raise Disconnected(stale)
            self.handshake = hs
            self.broker_info = broker_info
            self.credential_form = credential_form
            self.terms = terms
            self._greeted = True
        return hs

    def connect(self) -> dict:
        """start + hello + full reconciliation. Returns the handshake of the
        process that connected: the one started again when the adapter
        answered `reconnect` (reconcile)."""
        self.hello()
        self.reconcile()
        return self.handshake

    def identity(self) -> dict:
        """Everything a connection picker needs, in one shape.

        The broker's own words where it supplied them, the handshake facts
        where it did not. A platform builds its picker from this so it never
        has to know a broker's name in advance, and a broker that fills in
        nothing still gets a usable row.
        """
        hs = self.handshake
        info = dict(self.broker_info)
        # The broker's decoration goes in FIRST and the handshake facts land on
        # top of it. The sanitiser already drops unknown keys, so nothing in
        # broker_info can reach these names today; writing them last means it
        # still cannot if the display allowlist ever grows a colliding field.
        out = dict(info)
        out["display_name"] = (info.get("display_name") or hs.get("broker")
                               or "unnamed broker")
        # "unknown", never "paper". identity() is callable before a handshake
        # and by a platform whose broker answered something unexpected, and
        # the safe default for a field that decides whether real money is at
        # stake is the one that makes a platform ask rather than assume.
        out["mode"] = hs.get("mode") or "unknown"
        out["account_currency"] = hs.get("account_currency")
        out["account_model"] = hs.get("account_model")
        out["adapter"] = hs.get("adapter", {})
        # Absent rather than zero after a hello-only probe: a picker must not
        # be able to render "0 instruments" for a broker it never asked.
        if self.catalog:
            out["symbols"] = len(self.catalog)
        return out

    def _capability(self, part: str) -> dict:
        """One part of the handshake's capabilities ("orders", "data"...):
        {} when the handshake lacks it, or sends null or anything but an
        object for it (adapters in other languages often send null). A part
        that is lacking reads as lacking, never as an AttributeError."""
        caps = self.handshake.get("capabilities")
        got = caps.get(part) if isinstance(caps, dict) else None
        return got if isinstance(got, dict) else {}

    def _keep(self, generation: int, change=None, **values):
        """Keep what was read from the adapter process of `generation`; if a
        start() has replaced it since, keep nothing and raise Disconnected:
        what the last process said does not stand (see start()). `change`,
        when given, is called under the same lock and check, for what is
        changed in place rather than replaced (`subscribed`)."""
        with self._read_lock:
            if self._generation != generation:
                raise Disconnected(
                    "the adapter was started again while its state was being "
                    "read; what the last process said is not kept")
            for name, value in values.items():
                setattr(self, name, value)
            if change is not None:
                change()

    def reconcile(self):
        """SPEC 7: the catalog, the account, the positions and (a broker
        with resting orders) those orders, read from the adapter and
        REPLACING what was known.

        A process's first reconciliation is its connect's: connect() makes
        it after hello, and a platform that asks something between the two
        (which account, say) makes it itself. There an answer `reconnect`
        (SPEC 6) to any of these reads, or an extension `like` it, means the
        adapter learned at connect what changes its handshake, and must be
        started again. This does so by itself, once: that process is ended
        and a new one started (start(): nothing of the last stands, an
        arming made before the connect included), its hello read, and the
        reads made again. A second `reconnect`, from the process started
        for the first, is the adapter's fault: a ConnectorError, `internal`
        and not retryable, in plain words with the adapter's own, and
        nothing is started a third time. Outside a connect (a process that
        has been reconciled) `reconnect` is raised as it is and nothing is
        started: what a platform holds of a live session is its own to end.
        """
        connecting = self._reconciled != self._generation
        try:
            self._reconcile()
            return
        except ConnectorError as e:
            if not connecting or e.effective_code != "reconnect":
                raise
        self.start()
        self.hello()
        try:
            self._reconcile()
        except ConnectorError as e:
            if e.effective_code != "reconnect":
                raise
            raise ConnectorError("internal", self._asked_again(e)) from None

    def _asked_again(self, e: ConnectorError) -> str:
        """Why a connect ended when the process started again for an
        adapter's `reconnect` answered it too (reconcile)."""
        name = (self.broker_info.get("display_name")
                or protocol._clean_text(self.handshake.get("broker"), 40)
                or "this broker").rstrip()
        out = (f"The adapter for {name} asked to be started again a second "
               "time in one connect: it was started again once, as it "
               "asked, and the new process asked the same, so it did not "
               "keep what it learned. It was not started a third time.")
        words = protocol._clean_text(getattr(e, "message", None), 500)
        if words:
            out += f" Its words: {words}"
        return out

    def _reconcile(self):
        """reconcile()'s reads, from the adapter process there is now."""
        generation = self._generation
        cat = self.request("catalog.list")
        self._keep(generation, catalog={
            i["symbol"]: i for i in cat.get("instruments", [])})
        self._keep(generation, account=self.request("account.get"))
        pos = self.request("positions.list")
        # The book is authoritative again; the optimistic cache is truthful.
        self._keep(generation, positions={
            p["position_id"]: p for p in pos.get("positions", [])},
            positions_stale=False)
        # SPEC 7 (1.2): resting orders survive a disconnect exactly as
        # positions do, so a pending-capable broker is asked for them too.
        self._keep(generation, orders={}, orders_partial=False)
        caps_orders = self._capability("orders")
        # can_list defaults to true: a broker that says nothing is assumed to
        # answer orders.list. A broker with no enumerate-orders call at all
        # declares can_list false, so reconciliation is merely incomplete
        # rather than impossible.
        can_list = caps_orders.get("can_list", True)
        if caps_orders.get("pending") and can_list:
            try:
                rows = self.request("orders.list", {"state": "resting"})
                self._keep(generation, orders={
                    o["order_id"]: o for o in rows.get("orders", [])})
            except ConnectorError as e:
                # An adapter that must be started again (SPEC 6) has said
                # so; its view is not merely partial.
                if e.effective_code == "reconnect":
                    raise
                self._keep(generation, orders_partial=True)
        elif caps_orders.get("pending") and not can_list:
            # The platform must know its order view is incomplete rather than
            # believing an empty dict means "no resting orders".
            self._keep(generation, orders_partial=True)
        # This process has been connected: a `reconnect` from it now is not
        # a connect's (reconcile).
        self._keep(generation, _reconciled=generation)

    def close(self):
        try:
            if self.alive:
                self.request("bye", timeout=5)
        except Exception:
            pass
        self._end(self.proc)
        self.alive = False

    @staticmethod
    def _end(proc):
        """End one adapter process, that one and no other: terminate, then
        kill. Bounded (3 s each)."""
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    # == requests ========================================================
    # == arming (SPEC 3: mode is surfaced, real money is armed) ==========
    #
    # ONE definition of "is this real money", ONE enforcement point, ONE way
    # to consent. It lives here because every platform (including one nobody
    # has written yet) reaches a broker through this class, and three
    # surfaces each writing their own version is how a product ends up with
    # three answers to "how do you gate live trading". A surface may still be
    # NARROWER than this (offer paper only, demand its own verification
    # first); it must not re-implement the check.
    @staticmethod
    def mode_is_paper(mode) -> bool:
        """The ONE place the mode string is judged.

        A platform that has to make this call about a broker it has not
        connected (a picker deciding whether to warn, say) calls this with the
        remembered mode rather than writing `== "paper"` somewhere else. One
        comparison, so there is one answer.
        """
        return mode == "paper"

    @property
    def is_paper(self) -> bool:
        """True only if the broker SAID paper. Unknown is not paper."""
        return self.mode_is_paper(self.handshake.get("mode"))

    @property
    def armed(self) -> bool:
        return self._armed_for is not None

    @property
    def armed_for(self) -> str | None:
        """The recorded reason, for a platform to display or journal."""
        return self._armed_for

    def arm(self, reason: str, *, by_hand: bool = False) -> str:
        """Consent to send mutating calls on a broker that is not paper.

        `reason` is required and is not decoration: it is what a platform
        shows, logs and journals as the record of the decision, and an empty
        one means nobody actually decided.

        `by_hand` is the platform's statement that every mutating call it
        sends on this connection follows the user's own action for that one
        order: a button pressed on a ticket, or a confirmation that shows
        that order and nothing else. A broker whose terms set
        `orders_by_hand_only` (SPEC 3.3) cannot be armed without it on a
        connection that is not paper, so a strategy runner, a schedule or an
        assistant that arms the usual way is refused here, before any order
        exists, and is told why. The word is also checked at every mutating
        call (request), against the terms known then: arming before hello,
        or before a hello that brings the flag, is not a way round it.
        """
        reason = (reason or "").strip()
        if not reason:
            raise ConnectorError("unsupported",
                                 "arm() needs a reason; it is the record of "
                                 "the decision")
        # The judgement and the record are one step, under the write lock
        # that start() takes to replace the process: a restart on another
        # thread lands wholly before (the arming is then judged against the
        # new process, whose hello is unread) or wholly after (and clears
        # it). Never between, where an arming judged against the last
        # process outlived the restart that should have cleared it.
        with self._lock:
            if (not self.is_paper and self.terms.get("orders_by_hand_only")
                    and by_hand is not True):
                raise NotArmed(self.by_hand_refusal())
            self._armed_for = reason
            self._armed_by_hand = by_hand is True
        return reason

    def by_hand_refusal(self) -> str:
        """Why this connection is not armed for anything but the user's own
        orders: its broker's terms, in one sentence with its summary."""
        # The broker's own name as text: a third-party handshake can send
        # anything in `broker`, and a refusal must never fail to be said. A
        # name cut at 40 keeps no space before its possessive.
        name = (self.broker_info.get("display_name")
                or protocol._clean_text(self.handshake.get("broker"), 40)
                or "this broker").rstrip()
        out = (f"Not armed: under {_possessive(name)} terms each order on a "
               "live account follows a person's own action for it (SPEC "
               "3.3, orders_by_hand_only), so a strategy, a schedule or an "
               "assistant cannot trade it.")
        summary = str(self.terms.get("summary") or "").strip()
        if summary:
            if not summary.endswith((".", "!", "?")):
                summary += "."
            out += f" The terms: {summary}"
        return out

    def disarm(self):
        with self._lock:
            self._disarm()

    def _disarm(self):
        """Clear the arming. Under the write lock (disarm(), start())."""
        self._armed_for = None
        self._armed_by_hand = False

    def _idempotency_guard(self, method: str, params: dict):
        """Refuse a client_id reused with DIFFERENT parameters.

        SPEC 2 requires idempotency on client_id, and SPEC 6 reserves
        duplicate_client_id for a reuse carrying different parameters. Many
        venues do not enforce that: four of the eight simulated venues answer
        a conflicting replay with the ORIGINAL order and HTTP 200. Leaving the
        duty with the broker means the platform believes it is protected when
        it is not, so the check lives here, once, for every adapter. An
        adapter is still free to enforce it as well.
        """
        cid = (params or {}).get("client_id")
        if not cid:
            return
        sig = protocol.order_signature(method, params)
        prev = self._client_ids.get(cid)
        if prev is None:
            self._client_ids[cid] = sig
        elif prev != sig:
            raise ConnectorError(
                "duplicate_client_id",
                f"client_id {cid!r} was already used with different "
                f"parameters; a replay must be identical", False)

    def request(self, method: str, params: dict | None = None,
                timeout: float = 10.0) -> dict:
        # Only the protocol's own names go out (SPEC 1, 4). Every rule below
        # knows a mutating method by its exact name, and an adapter finds its
        # handler from the name, so another spelling of one (order_place,
        # flatten.all) would reach the handler with none of the rules
        # applied. Refused here, before anything is written, whatever the
        # connection's mode or arming.
        if not isinstance(method, str) or method not in protocol.METHODS:
            raise ConnectorError(
                "unsupported", f"{protocol._clean_text(repr(method), 60)} is "
                "not a method of this protocol (SPEC 4); nothing was sent")
        return self._send(method, params, timeout)

    def probe_unknown(self, name: str, timeout: float = 10.0) -> dict:
        """Send one of UNKNOWN_PROBES, a name that is not a method of this
        protocol, with no parameters, and return the adapter's answer: SPEC
        1 has it refuse the name, `unsupported`, without running anything.
        For the conformance suite (s12), which cannot ask that through
        request(). The names are the kit's own, never the caller's, so this
        is no way round request()'s rule."""
        if name not in self.UNKNOWN_PROBES:
            raise ConnectorError(
                "unsupported", "probe_unknown sends only the kit's own names "
                "(Connector.UNKNOWN_PROBES); nothing was sent")
        return self._send(name, None, timeout)

    def _send(self, method: str, params: dict | None,
              timeout: float) -> dict:
        """request() past its name rule: the arming rules, the write and the
        wait. Only request() and probe_unknown() call it."""
        # The checks and the write are one step, under the write lock that
        # start() also takes to open a new process. So a restart on another
        # thread falls wholly before the checks (which then judge the new
        # process, whose handshake is unread) or after the write (which went
        # to the old one), never between a check passed for one process and a
        # write to the next.
        began = getattr(self._began, "pending", None)
        with self._lock:
            if not self.alive or self.proc is None or self.proc.stdin is None:
                raise Disconnected("adapter not running")
            # A call in several requests that began on an earlier process
            # (subscribe(), unsubscribe(): _chunk) writes nothing to this
            # one: judged here, under the lock start() takes to open a
            # process, so a restart falls wholly before this check or after
            # the write. The mark is read, not cleared, and judges only a
            # write of the call's OWN method: a bye, a hello or another
            # subscribe sent nested inside request() on this thread leaves
            # it for the call's own chunk (a bye from close() that took the
            # mark would let the chunk's write go unchecked).
            if (began is not None and began[1] == method
                    and began[0] != self._generation):
                raise Disconnected(
                    "the adapter was started again since this call began; "
                    "nothing more of it was sent")
            if method in self.MUTATING:
                self._idempotency_guard(method, params or {})
            if (method in self.MUTATING and not self.is_paper
                    and not self.armed):
                mode = self.handshake.get("mode") or "unknown"
                raise NotArmed(
                    f"{method} refused: this broker reports mode {mode!r} and "
                    f"has not been armed. Call arm(reason) first.")
            # Armed, but no handshake read yet: what the arming was given
            # under (the broker's mode and terms) is not known.
            if method in self.MUTATING and not self._greeted:
                raise ConnectorError(
                    "unsupported", f"{method} refused: no handshake has been "
                    "read on this connection (SPEC 3: hello comes first), so "
                    "the broker's terms are not known.")
            # Armed, but not with the by_hand word, on a connection whose
            # terms (as known NOW) keep live orders by hand: the consent was
            # given before the broker said so, so it does not cover this order.
            if (method in self.MUTATING and not self.is_paper
                    and self.terms.get("orders_by_hand_only")
                    and not self._armed_by_hand):
                raise NotArmed(f"{method} refused. " + self.by_hand_refusal())
            self._rid += 1
            rid = f"r{self._rid}"
            slot = _Pending()
            self._pending[rid] = slot
            # The write stays under the lock: a text-mode pipe writer is not
            # thread-safe, and two threads placing orders concurrently could
            # interleave partial lines and corrupt the framing for both.
            try:
                self.proc.stdin.write(protocol.req(rid, method, params) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError) as e:
                self.alive = False
                self._pending.pop(rid, None)
                raise Disconnected(f"adapter disconnected ({e})") from e
        if not slot.done.wait(timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")
        msg = slot.msg
        if "error" in msg:
            e = msg["error"]
            if not isinstance(e, dict):
                # SPEC 4: an error is an object. One that is not (a string,
                # null, a list) is the adapter's fault; the caller gets a
                # ConnectorError with its text, not an AttributeError from
                # e.get.
                text = protocol._clean_text(
                    e if isinstance(e, str) else repr(e), 200)
                raise ConnectorError("internal", text or "the adapter's "
                                     "error is not of this protocol")
            raise ConnectorError(e.get("code", "internal"),
                                 e.get("message", ""), e.get("retryable", False),
                                 e.get("like"))
        result = msg.get("result", {})
        if method != "hello" and not isinstance(result, dict):
            # SPEC 4: a result is an object. One that is not would raise
            # AttributeError in a caller that reads it (reconcile); a
            # protocol error here instead. hello()
            # is left to judge its own handshake: a non-object there is "the
            # adapter's hello did not answer with an object", said by hello.
            raise ConnectorError("internal", "the adapter's answer is not "
                                 "of this protocol: its result is not an "
                                 "object")
        return result

    # == events ==========================================================
    def on(self, name: str, fn):
        """Register fn(data) for an event name ('*' matches all)."""
        self._handlers[name].append(fn)

    def mark(self) -> int:
        """Cursor into the event history; pass to wait_event(since=...) to
        ignore anything that happened before this point. Needed whenever an
        id can repeat (netting adapters reuse one position id per symbol)."""
        with self._event_cv:
            return self._events_total

    def wait_event(self, name: str, predicate=None, timeout: float = 10.0,
                   since: int = 0):
        """Block until an event named `name` (matching predicate) arrives,
        scanning history from `since` first so events can never be missed
        by races."""
        deadline = threading.TIMEOUT_MAX if timeout is None else timeout
        seen = since
        with self._event_cv:
            end = time.monotonic() + deadline
            while True:
                dropped = self._events_total - len(self.events)
                if seen < dropped:
                    # The cursor's events were evicted from the history
                    # window before this waiter scanned them. Skipping them
                    # silently could hide the very event being waited on;
                    # fail loudly so the caller re-reconciles instead.
                    raise ConnectorError(
                        "event_window_overrun",
                        f"{dropped - seen} events fell out of the history "
                        "window before they were scanned; re-reconcile",
                        retryable=True)
                while seen < self._events_total:
                    n, d = self.events[seen - dropped]
                    seen += 1
                    if n == name and (predicate is None or predicate(d)):
                        return d
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no {name} event within {timeout}s")
                self._event_cv.wait(remaining)

    def _unblock(self):
        """Answer every call still waiting: its adapter process is gone.
        Called under the write lock and the reader's lock."""
        for rid in list(self._pending):
            slot = self._pending.pop(rid, None)
            if slot is not None:
                slot.msg = {"type": "res", "id": rid, "error": {
                    "code": "internal", "message": "adapter disconnected",
                    "retryable": True}}
                slot.done.set()

    def _read_loop(self, proc=None, generation=None):
        # The reader of ONE adapter process. A Connector started again has
        # another process and another reader: this one reads its own pipe
        # only, passes on nothing once a start() has begun to replace its
        # process (the generation start() counts first, under the reader's
        # lock, before it clears anything), and its end does not mark the
        # new process dead. Each line is applied under the reader's lock with
        # that check, so no line of the old pipe (an adapter's child may hold
        # it open and write into it) lands on what start() cleared, nor
        # answers a call made to the new process.
        proc = self.proc if proc is None else proc
        generation = self._generation if generation is None else generation
        assert proc and proc.stdout
        # Why the pipe could no longer be read, when it was not its end.
        failed = None
        while True:
            # readline(MAX) instead of iterating the pipe: `for line in
            # stdout` buffers until a newline arrives, so one line with no
            # newline grows without limit and the PLATFORM dies on memory,
            # not the adapter. That is not only a broken-adapter case: an
            # adapter can front a remote venue, so the bytes can come from
            # off-box. An over-long line is drained and dropped rather than
            # truncated, because half a JSON object is not a message.
            try:
                line = proc.stdout.readline(MAX_LINE_CHARS)
            except Exception as e:  # noqa: BLE001 - said, never silent
                failed = f"its output could not be read ({type(e).__name__})"
                break
            if not line:
                break                       # EOF: the pipe is gone
            if self._generation != generation:
                break                       # started again since
            if len(line) >= MAX_LINE_CHARS and not line.endswith("\n"):
                self.oversize_lines += 1
                try:
                    while True:
                        tail = proc.stdout.readline(MAX_LINE_CHARS)
                        if not tail or tail.endswith("\n"):
                            break
                except Exception as e:  # noqa: BLE001 - said, never silent
                    failed = ("its output could not be read "
                              f"({type(e).__name__})")
                    break
                if not tail:
                    break
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = protocol.parse_line(line)
            except Exception:  # noqa: BLE001 - not JSON, or nested past
                msg = None     # what json reads (RecursionError): stray
            handlers, data = (), None
            with self._read_lock:
                if self._generation != generation:
                    break                   # started again since
                if msg is None:
                    # Not the protocol (a plain print() left in the
                    # adapter): kept with the stray lines where those are
                    # kept -- the deque of this process, which start()
                    # assigned (the check above is under the same lock),
                    # never the last one's. A platform that hides
                    # stray_lines (a kit from before them) just drops it;
                    # nothing of it degrades the connection's state.
                    try:
                        self.stray_lines.append(line[:self.STRAY_CHARS])
                    except Exception:  # noqa: BLE001 - dropped on such a kit
                        pass
                else:
                    try:
                        handlers, data = self._apply(msg)
                    except Exception as e:  # noqa: BLE001 - reader goes on
                        # A line of the protocol's form that cannot be
                        # applied: an event whose data is null, a list or a
                        # number, or whose fields _track cannot use. Were it
                        # to end this reader, every later call would time
                        # out, `alive` would stay True, and nothing would say
                        # why. It is a protocol error, kept with the stray
                        # lines and said in the connection's state; nothing
                        # of it is passed on.
                        self._protocol_error(line, msg, e)
                        handlers = ()
            # Outside the locks: a handler may call request().
            for fn in handlers:
                try:
                    fn(data)
                except Exception:
                    pass
        # The pipe is gone. Unblock every waiter, if this is still the
        # connection's process; under the write lock and the reader's, so a
        # start() that opens another is either wholly before this (and this
        # is not the connection's process any more) or after it. A pipe
        # that could not be read is said too: no reader ends in silence.
        with self._lock, self._read_lock:
            if self._generation == generation:
                self.alive = False
                self._unblock()
                if failed:
                    self.session_state = {
                        "state": "disconnected",
                        "detail": "the adapter is no longer heard: "
                                  + failed}
        with self._event_cv:
            self._event_cv.notify_all()

    class _NotOurs(Exception):
        """An event of the protocol's form whose data is not an object."""

    def _apply(self, msg: dict):
        """One parsed line, applied (under the reader's lock): an answer
        handed to its caller, an event tracked and kept. Returns the
        handlers to run for it and its data. Raises for a line that cannot
        be applied (_read_loop: a protocol error)."""
        if msg["type"] == "res":
            slot = self._pending.pop(msg["id"], None)
            if slot:
                slot.msg = msg
                slot.done.set()
            return (), None
        if msg["type"] != "event":
            return (), None
        # Counted as read whatever it holds: a line that is there is no gap.
        seq = msg["seq"]
        if self._last_seq and seq != self._last_seq + 1:
            self.seq_gaps += 1
        self._last_seq = seq
        name, data = msg["event"], msg.get("data", {})
        if not isinstance(data, dict):
            # SPEC 5: an event's data is an object.
            raise self._NotOurs("its data is not an object")
        self._track(name, data)
        with self._event_cv:
            self.events.append((name, data))
            self._events_total += 1
            self._event_cv.notify_all()
        return (self._handlers.get(name, [])
                + self._handlers.get("*", [])), data

    def _protocol_error(self, line: str, msg, e: Exception):
        """A line the reader could not apply: kept in stray_lines, and said
        in the connection's state (under the reader's lock). It raises
        nothing: a platform's Connector that hides stray_lines (written for
        a kit from before it) would otherwise end the reader here."""
        kept = True
        try:
            self.stray_lines.append(line[:self.STRAY_CHARS])
        except Exception:  # noqa: BLE001 - a kit that hides them; said below
            kept = False
        try:
            what = ("an event " + protocol._clean_text(msg.get("event"), 60)
                    if isinstance(msg, dict) and msg.get("type") == "event"
                    else "a line")
        except Exception:  # noqa: BLE001
            what = "a line"
        why = (str(e) if isinstance(e, self._NotOurs)
               else f"it could not be applied ({type(e).__name__})")
        detail = (f"the adapter sent {what} that is not of this protocol "
                  f"({why}); it was skipped")
        if kept:
            detail += ", and its line is kept with the stray lines"
        self.session_state = {"state": "degraded", "detail": detail}

    def _track(self, name: str, data: dict):
        """Keep the reconciled snapshots current from events."""
        if name == "account.updated":
            self.account = data
        elif name in ("order.filled", "order.partial"):
            # Validated before anything is recorded or changed: an event
            # whose fill_id or position_id cannot be used records no
            # fill_id, so the adapter's corrected re-send (the same
            # fill_id) is still applied, not dropped as a duplicate.
            if not (self._usable_key(data.get("fill_id"))
                    and self._usable_key(data.get("position_id"))):
                raise self._NotOurs("an id in it cannot be used")
            # The stream is at-least-once (SPEC 7): a fill re-emitted with the
            # SAME fill_id is applied once. Dedup before touching anything.
            if not self._dedup(self._seen_fills, self._fill_order,
                               data.get("fill_id")):
                return
            self._apply_fill(data)
        elif name == "position.closed":
            if not (self._usable_key(data.get("close_id"))
                    and self._usable_key(data.get("position_id"))):
                raise self._NotOurs("an id in it cannot be used")
            if not self._dedup(self._seen_closes, self._close_order,
                               data.get("close_id")):
                return
            self._apply_close(data)
        elif name == "catalog.changed":
            self._apply_catalog_change(data)
        elif name == "session.state":
            # Kept so a platform can SHOW it. A broker that reports itself
            # degraded (a capped stream, a reconnecting feed) is telling the
            # user something they cannot infer from a price that stopped
            # moving, and dropping it is the silent degradation SPEC 3 forbids
            # the platform to commit, not just the broker.
            self.session_state = {"state": data.get("state", "connected"),
                                  "detail": data.get("detail", "")}

    def _apply_catalog_change(self, data: dict):
        """Keep `catalog` true to the broker mid-session (SPEC 5).

        A catalog read once at connect and never again leaves a suspended
        symbol tradable and repriced terms stale: the platform keeps offering
        an instrument the broker has stopped dealing and sizes orders off
        numbers the broker no longer honours.

        Inline `instruments` are applied directly. Without them the SPEC says
        the platform MUST re-issue `catalog.list`, and it must not do that on
        this thread: this runs on the reader loop, and a request here would
        wait for a response only this loop can deliver. So it is flagged and
        refreshed by `refresh_catalog()`, which callers reach through
        `catalog_stale`.
        """
        reason = data.get("reason")
        symbols = [s for s in (data.get("symbols") or []) if s]
        specs = data.get("instruments")
        if isinstance(specs, list) and specs:
            # Validated before the catalog is touched, so one instrument
            # whose symbol cannot be a key leaves the catalog as it was,
            # not half-changed. A spec that is not an object, or carries no
            # symbol, is skipped; a symbol that is not a string fails the
            # whole change.
            clean = []
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                sym = spec.get("symbol")
                if not sym:
                    continue
                if not isinstance(sym, str):
                    raise self._NotOurs(
                        "an instrument's symbol is not a string")
                clean.append((sym, spec))
            for sym, spec in clean:
                self.catalog[sym] = spec
        elif reason in ("delisted", "expired"):
            for sym in symbols:
                self.catalog.pop(sym, None)
        elif symbols:
            self.catalog_stale = True
        self.last_catalog_change = {"reason": reason, "symbols": symbols}

    # == market data =====================================================
    def quote_limits(self) -> tuple[int, int]:
        """(concurrent symbols, symbols per request); 0 means no stated limit."""
        data = self._capability("data")
        try:
            return (int(data.get("max_quote_symbols") or 0),
                    int(data.get("max_symbols_per_request") or 0))
        except (TypeError, ValueError):
            return (0, 0)

    def subscribe(self, symbols: list[str]) -> dict:
        """Stream these symbols, within whatever the broker said it can serve.

        The ONE place the paging and capping is written. Every caller wants
        the same three things (send in batches the broker accepts, stop at the
        cap, find out what did not fit), and three callers writing it three
        ways is how one of them ends up silently dropping symbols.

        Returns {"subscribed", "refused", "cap"}. `refused` is not an error:
        it is the honest answer to "I asked for 40 and this broker serves 30",
        and the caller decides what to do about the other ten. Nothing here
        chooses for it, because only the caller knows which symbols its
        strategy actually needs.

        A call belongs to the process it began on. Each of its requests is
        written only while that is still the connection's process (_chunk),
        and what the process answered is kept only then (_keep): across a
        restart this raises Disconnected, nothing more of the call is sent
        to the next process, and `subscribed` stays as start() left it, so
        the next process is asked for the stream itself (SPEC 3.3: a restart
        keeps nothing the last process said, its streams included).
        """
        # The process and what it streams, read together: start() changes
        # both under the reader's lock.
        with self._read_lock:
            generation = self._generation
            subscribed = list(self.subscribed)
        want, seen = [], set(subscribed)
        for s in symbols:                     # de-duplicated, order preserved
            if s and s not in seen:
                seen.add(s)
                want.append(s)
        cap, per_call = self.quote_limits()
        room = len(want) if not cap else max(0, cap - len(subscribed))
        take, refused = want[:room], want[room:]
        step = per_call or len(take) or 1
        for i in range(0, len(take), step):
            chunk = take[i:i + step]
            self._chunk(generation, "quotes.subscribe", chunk)
            self._keep(generation,
                       change=lambda chunk=chunk: self.subscribed.extend(chunk))
        return {"subscribed": list(take), "refused": refused, "cap": cap}

    def unsubscribe(self, symbols: list[str]) -> dict:
        """Stop these streams. As subscribe(): across a restart this raises
        Disconnected, sends the next process nothing, and writes nothing
        over what it was asked for. Each request's streams are taken off
        `subscribed` as the adapter answers it, so a later one that fails
        leaves those stopped and said so."""
        with self._read_lock:
            generation = self._generation
            drop = [s for s in symbols if s in self.subscribed]
        _, per_call = self.quote_limits()
        step = per_call or len(drop) or 1
        for i in range(0, len(drop), step):
            chunk = drop[i:i + step]
            self._chunk(generation, "quotes.unsubscribe", chunk)

            def taken_off(gone=frozenset(chunk)):
                self.subscribed = [s for s in self.subscribed
                                   if s not in gone]

            self._keep(generation, change=taken_off)
        return {"unsubscribed": drop}

    def _chunk(self, generation: int, method: str, symbols: list) -> dict:
        """One request of a call that began on the process of `generation`
        (subscribe(), unsubscribe()), through request(). It is written only
        if that is still the connection's process, which _send checks under
        the write lock, with the write, so a restart between two of a call's
        requests, or between its read of `subscribed` and its first write,
        lets nothing of the rest of the call reach the next process.
        Disconnected otherwise, with nothing written. The previous mark is
        saved and put back, not cleared, so a nested _chunk (a platform
        re-subscribing inside request() on this thread) does not drop the
        outer call's mark."""
        prev = getattr(self._began, "pending", None)
        self._began.pending = (generation, method)
        try:
            return self.request(method, {"symbols": symbols})
        finally:
            self._began.pending = prev

    def refresh_catalog(self) -> dict:
        """Re-read the catalog. Safe to call from any thread but the reader."""
        generation = self._generation
        cat = self.request("catalog.list")
        self._keep(generation, catalog={
            i["symbol"]: i for i in cat.get("instruments", [])},
            catalog_stale=False)
        return self.catalog
