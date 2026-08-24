# StrategyEngine: market entries, protective brackets, trailing stops and
# fills, bar by bar. Order identity is by reference (pending orders hold the
# OpenEntry object itself), so Python object identity (`is`) is the test
# wherever an order is matched.
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import List, Optional


def _f64_min(a, b):
    # NaN-ignoring min: a NaN operand loses to the other one. Python's
    # builtin min() keeps a leading NaN, so the rule is spelled out.
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a <= b else b


def _f64_max(a, b):
    # NaN-ignoring max, same NaN handling as _f64_min.
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a >= b else b


def _total_cmp_key(x):
    # IEEE 754 total-order sort key: the raw bits with the sign-magnitude
    # flip, a total order that places NaN above +inf and -NaN below -inf.
    # Plain Python sort would misplace NaN.
    bits = struct.unpack("<q", struct.pack("<d", x))[0]
    if bits < 0:
        bits ^= 0x7FFFFFFFFFFFFFFF
    return bits


@dataclass
class OpenEntry:
    id: str
    direction: str  # "long" | "short"
    price: float
    qty: float
    bar_index: int
    time: float


class PendingOrder:
    # Tagged union: variants are nested dataclasses, matched with isinstance.
    @dataclass
    class Entry:
        id: str
        direction: str
        qty: float

    @dataclass
    class CloseEntry:
        entry_ref: OpenEntry
        exit_id: str
        qty: float


@dataclass
class PendingStopLimit:
    exit_id: str
    entry_ref: OpenEntry
    stop_price: Optional[float]
    limit_price: Optional[float]
    # Trailing stop state. best_price tracks the most favorable price seen
    # since the order was armed (highest for longs, lowest for shorts) using
    # COMPLETED bars only: each bar the trigger check runs against the best
    # from prior bars, then best is updated with this bar's extreme. Checking
    # against the same bar's own high would make any bar with range > trail
    # self-trigger, which invents an intrabar path we cannot know.
    trail_points: Optional[float]
    trail_percent: Optional[float]
    best_price: float
    qty: float


@dataclass
class DeferredExit:
    exit_id: str
    stop_price: Optional[float]
    limit_price: Optional[float]
    profit_points: Optional[float]
    loss_points: Optional[float]
    trail_points: Optional[float]
    trail_percent: Optional[float]
    qty: float


@dataclass
class ExitSpec:
    """Everything an exit() call can specify. Bundled so the engine call site
    stays readable as exit features grow."""
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    profit_points: Optional[float] = None
    loss_points: Optional[float] = None
    trail_points: Optional[float] = None
    trail_percent: Optional[float] = None


@dataclass
class StrategyTrade:
    entry_bar: int
    exit_bar: int
    direction: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_percent: float
    bars_held: int
    entry_id: str
    exit_id: str


class TradeCommand:
    # trade_entry / trade_exit markers, in emission order.
    @dataclass
    class Entry:
        bar_index: int
        direction: str
        price: float
        qty: float
        id: str

    @dataclass
    class Exit:
        bar_index: int
        price: float
        qty: float
        pnl: float
        id: str
        from_entry: str


@dataclass
class StrategyConfig:
    capital: float = 10000.0
    commission: float = 0.0
    commission_type: str = "percent"  # "percent" | "fixed"
    slippage: float = 0.0
    pyramiding: float = 0.0
    default_qty: float = 1.0
    # How an order's qty number is interpreted, resolved at FILL time:
    # "fixed" = units (the default), "percent_of_equity" =
    # percent of current equity converted to units at the fill price,
    # "cash" = account-currency amount converted to units at the fill
    # price. Unrecognized values behave as "fixed" so a position is never
    # silently resized.
    qty_type: str = "fixed"
    currency: str = "USD"


class StrategyEngine:
    def __init__(self, config: StrategyConfig, bar_count: int):
        capital = config.capital
        self.config = config
        self.bar_count = bar_count
        self.open_entries: List[OpenEntry] = []
        self.pending_orders: list = []
        self.pending_stop_limits: List[PendingStopLimit] = []
        # insertion-ordered map by entry id, kept as a list of (key, value)
        # pairs so replacement and iteration order stay explicit
        self.deferred_exits: list = []
        self.closed_trades: List[StrategyTrade] = []
        self.equity_curve: List[float] = [capital] * bar_count
        self.realized_pnl = 0.0
        self.total_commission = 0.0
        self.trade_commands: list = []
        self.current_streak = 0.0
        self.max_consec_wins = 0.0
        self.max_consec_losses = 0.0
        # Per-bar "was any position open" flags, written idempotently by
        # update_equity (finalize re-marks the last bar).
        self.in_market: List[bool] = [False] * bar_count

    def entry(self, id, direction, qty, _bar, _price, _time):
        actual_qty = qty if qty > 0.0 else self.config.default_qty
        if actual_qty <= 0.0:
            return
        same_direction_count = float(
            sum(1 for e in self.open_entries if e.direction == direction)
        )
        if same_direction_count > self.config.pyramiding:
            return
        # Queue closes for opposite entries (reverse at next open)
        opposite = [e for e in self.open_entries if e.direction != direction]
        for opp in opposite:
            self.pending_orders.append(PendingOrder.CloseEntry(
                entry_ref=opp,
                exit_id=f"reverse_{id}",
                qty=0.0,
            ))
        self.pending_orders.append(PendingOrder.Entry(
            id=id,
            direction=direction,
            qty=actual_qty,
        ))

    def exit(self, id, from_entry, qty, bar, price, high, low, time, spec: ExitSpec):
        stop_price = spec.stop_price
        limit_price = spec.limit_price
        profit_points = spec.profit_points
        loss_points = spec.loss_points
        trail_points = spec.trail_points
        trail_percent = spec.trail_percent
        if math.isnan(price) or math.isnan(high) or math.isnan(low):
            return
        if from_entry is not None and from_entry != "":
            targets = [e for e in self.open_entries if e.id == from_entry]
        else:
            targets = list(self.open_entries)

        if not targets:
            fe = from_entry
            if fe is not None:
                if fe != "" and (
                    stop_price is not None
                    or limit_price is not None
                    or profit_points is not None
                    or loss_points is not None
                    or trail_points is not None
                    or trail_percent is not None
                ):
                    has_pending = any(
                        isinstance(o, PendingOrder.Entry) and o.id == fe
                        for o in self.pending_orders
                    )
                    if has_pending:
                        # one deferred exit per entry id: re-registering replaces
                        self.deferred_exits = [(k, v) for (k, v) in self.deferred_exits if k != fe]
                        self.deferred_exits.append((
                            fe,
                            DeferredExit(
                                exit_id=id,
                                stop_price=stop_price,
                                limit_price=limit_price,
                                profit_points=profit_points,
                                loss_points=loss_points,
                                trail_points=trail_points,
                                trail_percent=trail_percent,
                                qty=qty,
                            ),
                        ))
            return

        for entry in targets:
            e_dir = entry.direction
            e_price = entry.price
            e_qty = entry.qty
            if loss_points is not None:
                effective_stop = e_price - loss_points if e_dir == "long" else e_price + loss_points
            else:
                effective_stop = stop_price
            if profit_points is not None:
                effective_limit = e_price + profit_points if e_dir == "long" else e_price - profit_points
            else:
                effective_limit = limit_price
            has_trail = trail_points is not None or trail_percent is not None

            if effective_stop is not None or effective_limit is not None or has_trail:
                exit_price = price
                should_exit = False
                if e_dir == "long":
                    if effective_stop is not None:
                        if low <= effective_stop:
                            exit_price = effective_stop
                            should_exit = True
                    if effective_limit is not None:
                        if high >= effective_limit and not should_exit:
                            exit_price = effective_limit
                            should_exit = True
                else:
                    if effective_stop is not None:
                        if high >= effective_stop:
                            exit_price = effective_stop
                            should_exit = True
                    if effective_limit is not None:
                        if low <= effective_limit and not should_exit:
                            exit_price = effective_limit
                            should_exit = True

                if should_exit:
                    exit_qty = _f64_min(qty, e_qty) if qty > 0.0 else e_qty
                    self.close_entry(entry, bar, exit_price, time, id, exit_qty)
                    self.pending_stop_limits = [
                        o for o in self.pending_stop_limits
                        if not (o.entry_ref is entry and o.exit_id == id)
                    ]
                else:
                    # Strategies call exit() every bar, which replaces the
                    # standing (entryRef, exitId) order. A trailing stop must
                    # survive that replacement or its high-water mark resets
                    # each bar, so carry best_price over from the order being
                    # replaced. First arm seeds best at the current close.
                    prior_best = None
                    for o in self.pending_stop_limits:
                        if o.entry_ref is entry and o.exit_id == id:
                            prior_best = o.best_price
                            break
                    self.pending_stop_limits = [
                        o for o in self.pending_stop_limits
                        if not (o.entry_ref is entry and o.exit_id == id)
                    ]
                    self.pending_stop_limits.append(PendingStopLimit(
                        exit_id=id,
                        entry_ref=entry,
                        stop_price=effective_stop,
                        limit_price=effective_limit,
                        trail_points=trail_points,
                        trail_percent=trail_percent,
                        best_price=prior_best if prior_best is not None else price,
                        qty=qty,
                    ))
            else:
                self.pending_orders.append(PendingOrder.CloseEntry(
                    entry_ref=entry,
                    exit_id=id,
                    qty=qty,
                ))

    def close_all(self, _bar, _price, _time):
        entries = list(self.open_entries)
        for entry in entries:
            self.pending_orders.append(PendingOrder.CloseEntry(
                entry_ref=entry,
                exit_id="close_all",
                qty=0.0,
            ))

    def process_pending_orders(self, bar, fill_price, time):
        if not self.pending_orders:
            return
        if math.isnan(fill_price) or fill_price <= 0.0:
            self.pending_orders.clear()
            return
        to_process = self.pending_orders
        self.pending_orders = []

        for order in to_process:
            if isinstance(order, PendingOrder.CloseEntry):
                entry_ref = order.entry_ref
                exit_id = order.exit_id
                qty = order.qty
                if not any(e is entry_ref for e in self.open_entries):
                    continue
                e_qty = entry_ref.qty
                exit_qty = _f64_min(qty, e_qty) if qty > 0.0 else e_qty
                self.close_entry(entry_ref, bar, fill_price, time, exit_id, exit_qty)
            else:
                id = order.id
                direction = order.direction
                qty = order.qty
                same_dir = float(
                    sum(1 for e in self.open_entries if e.direction == direction)
                )
                if same_dir > self.config.pyramiding:
                    continue
                if direction == "long":
                    fill_with_slippage = fill_price + self.config.slippage
                else:
                    fill_with_slippage = fill_price - self.config.slippage
                # Resolve the order's qty number into units per qty_type.
                # Equity marks open positions at the raw fill price (the
                # bar's open), before slippage, since slippage is a cost
                # of THIS order, not a revaluation of the book.
                # "risk" sizing: the order's qty number is the percent of
                # equity to LOSE if the stop hits, so units = risk cash /
                # stop distance. The stop must already be attached (the
                # deferred exit registered before the fill); with no stop
                # there is no defined risk, so the fill is refused rather
                # than guessed (fail closed, same as busted equity).
                risk_stop_distance = None
                for (k, d) in self.deferred_exits:
                    if k == id:
                        lp = d.loss_points
                        sp = d.stop_price
                        tp = d.trail_points
                        if lp is not None and lp > 0.0:
                            risk_stop_distance = lp
                        elif lp is None and sp is not None:
                            dist = abs(fill_with_slippage - sp)
                            risk_stop_distance = dist if dist > 0.0 else None
                        elif lp is None and sp is None and tp is not None and tp > 0.0:
                            risk_stop_distance = tp
                        break
                if self.config.qty_type == "risk":
                    if risk_stop_distance is None:
                        continue
                    unrealized = 0.0
                    for e in self.open_entries:
                        if e.direction == "long":
                            unrealized += (fill_price - e.price) * e.qty
                        else:
                            unrealized += (e.price - fill_price) * e.qty
                    equity = self.config.capital + self.realized_pnl + unrealized
                    qty = equity * qty / 100.0 / risk_stop_distance
                elif self.config.qty_type == "percent_of_equity":
                    unrealized = 0.0
                    for e in self.open_entries:
                        if e.direction == "long":
                            unrealized += (fill_price - e.price) * e.qty
                        else:
                            unrealized += (e.price - fill_price) * e.qty
                    equity = self.config.capital + self.realized_pnl + unrealized
                    qty = equity * qty / 100.0 / fill_with_slippage
                elif self.config.qty_type == "cash":
                    qty = qty / fill_with_slippage
                # else: "fixed" and anything unrecognized keep qty as units
                # Sizing can produce zero, negative (busted account), or
                # non-finite units; refuse the fill rather than open a
                # nonsense position.
                if not math.isfinite(qty) or qty <= 0.0:
                    continue
                commission = self.calc_commission(fill_with_slippage, qty)
                self.total_commission += commission
                self.realized_pnl -= commission

                entry = OpenEntry(
                    id=id,
                    direction=direction,
                    price=fill_with_slippage,
                    qty=qty,
                    bar_index=bar,
                    time=time,
                )
                self.open_entries.append(entry)

                self.trade_commands.append(TradeCommand.Entry(
                    bar_index=bar,
                    direction=direction,
                    price=fill_with_slippage,
                    qty=qty,
                    id=id,
                ))

                # Activate any deferred stop/limit for this entry id
                pos = None
                for i, (k, _) in enumerate(self.deferred_exits):
                    if k == id:
                        pos = i
                        break
                if pos is not None:
                    _, deferred = self.deferred_exits.pop(pos)
                    if deferred.loss_points is not None:
                        if direction == "long":
                            stop_price = fill_with_slippage - deferred.loss_points
                        else:
                            stop_price = fill_with_slippage + deferred.loss_points
                    else:
                        stop_price = deferred.stop_price
                    if deferred.profit_points is not None:
                        if direction == "long":
                            limit_price = fill_with_slippage + deferred.profit_points
                        else:
                            limit_price = fill_with_slippage - deferred.profit_points
                    else:
                        limit_price = deferred.limit_price
                    self.pending_stop_limits.append(PendingStopLimit(
                        exit_id=deferred.exit_id,
                        entry_ref=entry,
                        stop_price=stop_price,
                        limit_price=limit_price,
                        trail_points=deferred.trail_points,
                        trail_percent=deferred.trail_percent,
                        # Deferred order activates at the fill: the trail
                        # starts tracking from the entry price.
                        best_price=fill_with_slippage,
                        qty=deferred.qty,
                    ))

    def check_pending_stop_limits(self, bar, high, low, time):
        if not self.pending_stop_limits:
            return
        # Iterate a snapshot; entries may close during the walk
        order_snapshot = [
            (o.exit_id, o.entry_ref, o.stop_price, o.limit_price, o.qty,
             o.trail_points, o.trail_percent, o.best_price)
            for o in self.pending_stop_limits
        ]

        for (exit_id, entry_ref, static_stop, limit_price, qty,
             trail_points, trail_percent, best_price) in order_snapshot:
            # Order identity is (entryRef, exitId): exit() dedupes that pair
            # on registration, so it uniquely names one standing order. Field
            # equality would break on NaN prices (Some(NaN) != Some(NaN)).
            if not any(e is entry_ref for e in self.open_entries):
                self.pending_stop_limits = [
                    o for o in self.pending_stop_limits
                    if not (o.entry_ref is entry_ref and o.exit_id == exit_id)
                ]
                continue
            e_dir = entry_ref.direction
            # Trailing component: stop derived from the best price over
            # COMPLETED bars (best_price excludes this bar; see the class
            # comment). Combined with a static stop the tighter one wins.
            if trail_points is not None:
                trail_dist = trail_points
            elif trail_percent is not None:
                trail_dist = abs(best_price) * trail_percent / 100.0
            else:
                trail_dist = None
            if trail_dist is not None:
                dynamic = best_price - trail_dist if e_dir == "long" else best_price + trail_dist
                if static_stop is not None:
                    if e_dir == "long":
                        stop_price = _f64_max(static_stop, dynamic)
                    else:
                        stop_price = _f64_min(static_stop, dynamic)
                else:
                    stop_price = dynamic
            else:
                stop_price = static_stop
            exit_price = 0.0
            should_exit = False
            if e_dir == "long":
                if stop_price is not None:
                    if low <= stop_price:
                        exit_price = stop_price
                        should_exit = True
                if limit_price is not None:
                    if high >= limit_price and not should_exit:
                        exit_price = limit_price
                        should_exit = True
            else:
                if stop_price is not None:
                    if high >= stop_price:
                        exit_price = stop_price
                        should_exit = True
                if limit_price is not None:
                    if low <= limit_price and not should_exit:
                        exit_price = limit_price
                        should_exit = True
            if not should_exit and trail_dist is not None:
                # Advance the high-water mark AFTER the trigger check so this
                # bar's extreme never feeds its own stop.
                if e_dir == "long":
                    new_best = _f64_max(best_price, high)
                else:
                    new_best = _f64_min(best_price, low)
                for o in self.pending_stop_limits:
                    if o.entry_ref is entry_ref and o.exit_id == exit_id:
                        o.best_price = new_best
                        break
            if should_exit:
                # remove this order, matched by identity
                pos = None
                for i, o in enumerate(self.pending_stop_limits):
                    if o.entry_ref is entry_ref and o.exit_id == exit_id:
                        pos = i
                        break
                if pos is not None:
                    self.pending_stop_limits.pop(pos)
                e_qty = entry_ref.qty
                exit_qty = _f64_min(qty, e_qty) if qty > 0.0 else e_qty
                self.close_entry(entry_ref, bar, exit_price, time, exit_id, exit_qty)

    def update_equity(self, bar, current_price):
        unrealized = 0.0
        for e in self.open_entries:
            if e.direction == "long":
                unrealized += (current_price - e.price) * e.qty
            else:
                unrealized += (e.price - current_price) * e.qty
        if bar < len(self.equity_curve):
            self.equity_curve[bar] = self.config.capital + self.realized_pnl + unrealized
            # OR, not assign: finalize re-runs update_equity after force-
            # closing everything, which must not erase the fact the last bar
            # WAS in the market.
            self.in_market[bar] = self.in_market[bar] or bool(self.open_entries)

    def resolve_position(self, member, _bar, current_price):
        """position.* namespace; None for unknown members."""
        if member == "size":
            size = 0.0
            for e in self.open_entries:
                size += e.qty if e.direction == "long" else -e.qty
            return size
        elif member == "avg_price":
            if not self.open_entries:
                return math.nan
            total_cost = 0.0
            total_qty = 0.0
            for e in self.open_entries:
                total_cost += e.price * e.qty
                total_qty += e.qty
            return total_cost / total_qty if total_qty > 0.0 else math.nan
        elif member == "unrealized_pnl":
            pnl = 0.0
            for e in self.open_entries:
                if e.direction == "long":
                    pnl += (current_price - e.price) * e.qty
                else:
                    pnl += (e.price - current_price) * e.qty
            return pnl
        elif member == "entry_bar":
            if not self.open_entries:
                return math.nan
            return float(self.open_entries[0].bar_index)
        elif member == "entry_time":
            if not self.open_entries:
                return math.nan
            return self.open_entries[0].time
        elif member == "entry_price":
            if not self.open_entries:
                return math.nan
            return self.open_entries[0].price
        else:
            return None

    def resolve_strategy(self, member, _bar, current_price):
        """strategy.* namespace"""
        if member == "equity":
            unrealized = 0.0
            for e in self.open_entries:
                if e.direction == "long":
                    unrealized += (current_price - e.price) * e.qty
                else:
                    unrealized += (e.price - current_price) * e.qty
            return self.config.capital + self.realized_pnl + unrealized
        elif member == "initial_capital":
            return self.config.capital
        elif member == "net_profit":
            return self.realized_pnl
        elif member == "gross_profit":
            total = 0.0
            for t in self.closed_trades:
                if t.pnl > 0.0:
                    total += t.pnl
            return total
        elif member == "gross_loss":
            total = 0.0
            for t in self.closed_trades:
                if t.pnl < 0.0:
                    total += t.pnl
            return total
        elif member == "open_trades":
            return float(len(self.open_entries))
        elif member == "closed_trades":
            return float(len(self.closed_trades))
        elif member == "win_rate":
            if not self.closed_trades:
                return 0.0
            wins = float(sum(1 for t in self.closed_trades if t.pnl > 0.0))
            return (wins / float(len(self.closed_trades))) * 100.0
        elif member == "max_drawdown":
            return self.calc_max_drawdown()[0]
        elif member == "commission":
            return self.total_commission
        else:
            return None

    def take_trade_commands(self):
        return list(self.trade_commands)

    def has_open_entries(self):
        return bool(self.open_entries)

    def finalize(self, last_bar, last_price, last_time):
        """End-of-data settlement: fill still-pending orders at the last
        close and close every open entry there, so the trade-command stream
        (and the script-visible closed_trades state) is complete. The
        engine's job ends at deterministic order state; no statistics are
        computed here."""
        self.process_pending_orders(last_bar, last_price, last_time)
        # close every open entry at the last close
        entries = list(self.open_entries)
        for entry in entries:
            self.close_entry(entry, last_bar, last_price, last_time, "close_all", None)
        self.update_equity(last_bar, last_price)

    def close_entry(self, entry, bar, raw_exit_price, time, exit_id, exit_qty):
        e_dir = entry.direction
        e_price = entry.price
        e_qty = entry.qty
        e_bar = entry.bar_index
        e_id = entry.id
        qty = exit_qty if exit_qty is not None else e_qty

        if e_dir == "long":
            exit_price = raw_exit_price - self.config.slippage
        else:
            exit_price = raw_exit_price + self.config.slippage

        if e_dir == "long":
            raw_pnl = (exit_price - e_price) * qty
        else:
            raw_pnl = (e_price - exit_price) * qty

        commission = self.calc_commission(exit_price, qty)
        self.total_commission += commission
        net_pnl = raw_pnl - commission
        self.realized_pnl += net_pnl

        if net_pnl > 0.0:
            self.current_streak = self.current_streak + 1.0 if self.current_streak > 0.0 else 1.0
            if self.current_streak > self.max_consec_wins:
                self.max_consec_wins = self.current_streak
        elif net_pnl < 0.0:
            self.current_streak = self.current_streak - 1.0 if self.current_streak < 0.0 else -1.0
            if abs(self.current_streak) > self.max_consec_losses:
                self.max_consec_losses = abs(self.current_streak)

        pnl_percent = (net_pnl / (e_price * qty)) * 100.0 if e_price != 0.0 else 0.0
        self.closed_trades.append(StrategyTrade(
            entry_bar=e_bar,
            exit_bar=bar,
            direction=e_dir,
            entry_price=e_price,
            exit_price=exit_price,
            qty=qty,
            pnl=net_pnl,
            pnl_percent=pnl_percent,
            bars_held=bar - e_bar,
            entry_id=e_id,
            exit_id=exit_id,
        ))

        self.trade_commands.append(TradeCommand.Exit(
            bar_index=bar,
            price=exit_price,
            qty=qty,
            pnl=net_pnl,
            id=exit_id,
            from_entry=e_id,
        ))

        if qty >= e_qty:
            self.open_entries = [e for e in self.open_entries if e is not entry]
            self.pending_stop_limits = [o for o in self.pending_stop_limits if o.entry_ref is not entry]
        else:
            entry.qty -= qty

    def calc_commission(self, price, qty):
        if self.config.commission_type == "percent":
            return abs(price * qty * self.config.commission)
        else:
            return self.config.commission

    def calc_max_drawdown(self):
        peak = self.config.capital
        max_dd = 0.0
        max_dd_pct = 0.0
        for i in range(self.bar_count):
            equity = self.equity_curve[i]
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = (dd / peak) * 100.0 if peak > 0.0 else 0.0
        return (max_dd, max_dd_pct)
