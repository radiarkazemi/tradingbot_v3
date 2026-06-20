"""
backtest_broker.py — TraderBot v2

A minimal mock of the MetaTrader5 Python module's API surface, just
enough to run position_monitor.py and order_manager.py UNMODIFIED
against a simulated account instead of a live MT5 terminal.

This is NOT a general-purpose MT5 emulator — it only implements the
specific calls this codebase actually makes (checked against
order_manager.py and position_monitor.py directly). If you add new
mt5.* calls elsewhere, this will need new stubs to match.

Usage: build a MockBroker, then monkeypatch the `mt5` name in every
module that imports MetaTrader5 to point at a thin function-module
shim backed by this broker (see backtest_engine.py — install_mock()).
"""
import time as _time
from types import SimpleNamespace


# ── Constants (mirrors the real MetaTrader5 module's values closely
#    enough for this codebase's purposes — exact numeric values don't
#    matter since nothing here compares them to real MT5 constants,
#    only to each other) ──────────────────────────────────────────
ORDER_TYPE_BUY        = 0
ORDER_TYPE_SELL       = 1
ORDER_TYPE_BUY_STOP   = 4
ORDER_TYPE_SELL_STOP  = 5

TRADE_ACTION_DEAL     = 1
TRADE_ACTION_PENDING  = 5
TRADE_ACTION_SLTP     = 6
TRADE_ACTION_REMOVE   = 8

TRADE_RETCODE_DONE    = 10009

ORDER_TIME_GTC        = 0
ORDER_FILLING_FOK     = 0
ORDER_FILLING_IOC     = 1
ORDER_FILLING_RETURN  = 2

DEAL_ENTRY_IN         = 0
DEAL_ENTRY_OUT        = 1

DEAL_REASON_CLIENT    = 0
DEAL_REASON_MOBILE    = 1
DEAL_REASON_WEB       = 2
DEAL_REASON_EXPERT    = 3
DEAL_REASON_SL        = 4
DEAL_REASON_TP        = 5


# ── Per-symbol synthetic specs (close enough to real broker specs for
#    this codebase's math — NOT real broker contract specs) ────────
SYMBOL_SPECS = {
    "EURUSD": dict(digits=5, point=0.00001, contract_size=100_000,
                   trade_stops_level=0, base_price=1.0850,
                   base_spread=0.00015,          # ~1.5 pips
                   daily_vol_pips=90.0,
                   volume_step=0.01, volume_min=0.01,
                   trade_tick_size=0.00001, trade_tick_value=1.0),
    "XAUUSD": dict(digits=2, point=0.01, contract_size=100,
                   trade_stops_level=0, base_price=2350.00,
                   base_spread=0.30,             # ~3 pips (0.10 each)
                   daily_vol_pips=180.0,
                   volume_step=0.01, volume_min=0.01,
                   trade_tick_size=0.01, trade_tick_value=1.0),
    "NAS100": dict(digits=1, point=0.1, contract_size=1,
                   trade_stops_level=0, base_price=19500.0,
                   base_spread=1.0,
                   daily_vol_pips=250.0,
                   volume_step=0.01, volume_min=0.01,
                   trade_tick_size=0.1, trade_tick_value=1.0),
}

MAGIC_NUMBER = 998877  # must match config.MAGIC_NUMBER for filtering


def _pip_size(spec) -> float:
    if spec["digits"] in (0, 1):
        return 1.0
    if spec["digits"] in (2, 3):
        return spec["point"] * 10
    return spec["point"] * 10


class MockBroker:
    """
    Holds all simulated account/market state and implements order
    matching (pending-order fills, SL/TP closes) that a real broker
    would otherwise do server-side. position_monitor.py only ever
    READS state via positions_get/orders_get/account_info and reacts
    — it never simulates fills itself — so this class has to actually
    perform that matching for the bot's logic to see anything happen.
    """

    def __init__(self, symbol: str, start_balance: float = 1000.0,
                 leverage: int = 500, commission_per_lot: float = 0.0,
                 seed: int = None, spec_key: str = None):
        spec_key = spec_key or symbol
        if spec_key not in SYMBOL_SPECS:
            raise ValueError(
                f"No backtest spec for symbol '{spec_key}'. "
                f"Add one to SYMBOL_SPECS in backtest_broker.py "
                f"(supported: {list(SYMBOL_SPECS.keys())})"
            )
        self.symbol  = symbol          # used for the file-namespacing guard
        self.spec    = SYMBOL_SPECS[spec_key]
        self.leverage = leverage
        self.commission_per_lot = commission_per_lot

        self.balance = start_balance
        self.start_balance = start_balance

        self.bid = self.spec["base_price"]
        self.ask = self.bid + self.spec["base_spread"]

        self._next_ticket = 1000
        self.pending: dict[int, SimpleNamespace] = {}
        self.positions: dict[int, SimpleNamespace] = {}
        self.deals: dict[int, list] = {}   # position ticket -> [deal, ...]
        self.closed_log: list = []         # flat history for reporting

        self.equity_curve: list = []       # (sim_time, equity) samples
        self.max_drawdown_pct = 0.0
        self.peak_equity = start_balance
        self.current_sim_time = 0  # updated every process_tick() call

    # ── Market data ────────────────────────────────────────────────

    def set_price(self, bid: float, ask: float):
        self.bid, self.ask = bid, ask

    def _margin_for(self, lot: float, price: float) -> float:
        return (lot * self.spec["contract_size"] * price) / self.leverage

    def _profit_for(self, is_buy: bool, lot: float,
                     open_price: float, close_price: float) -> float:
        diff = (close_price - open_price) if is_buy else (open_price - close_price)
        return diff * self.spec["contract_size"] * lot

    # ── Floating P&L / equity bookkeeping ───────────────────────────

    def _mark_to_market(self):
        for pos in self.positions.values():
            is_buy = pos.type == ORDER_TYPE_BUY
            pos.price_current = self.bid if is_buy else self.ask
            pos.profit = self._profit_for(is_buy, pos.volume,
                                           pos.price_open, pos.price_current)

    def equity(self) -> float:
        return self.balance + sum(p.profit for p in self.positions.values())

    def margin_used(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            price = self.ask if pos.type == ORDER_TYPE_BUY else self.bid
            total += self._margin_for(pos.volume, price)
        return total

    def record_equity_sample(self, sim_time):
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            dd = (self.peak_equity - eq) / self.peak_equity * 100.0
            self.max_drawdown_pct = max(self.max_drawdown_pct, dd)
        self.equity_curve.append((sim_time, eq))

    # ── Order matching engine (runs every simulated tick) ──────────

    def process_tick(self, sim_time):
        self.current_sim_time = sim_time
        self._mark_to_market()
        self._check_pending_fills(sim_time)
        self._check_sl_tp_closes(sim_time)
        self._mark_to_market()
        self.record_equity_sample(sim_time)

    def _check_pending_fills(self, sim_time):
        triggered = []
        for ticket, o in self.pending.items():
            if o.type == ORDER_TYPE_BUY_STOP and self.ask >= o.price_open:
                triggered.append(ticket)
            elif o.type == ORDER_TYPE_SELL_STOP and self.bid <= o.price_open:
                triggered.append(ticket)
        for ticket in triggered:
            o = self.pending.pop(ticket)
            is_buy = o.type == ORDER_TYPE_BUY_STOP
            pos = SimpleNamespace(
                ticket=ticket, symbol=self.symbol, magic=o.magic,
                type=ORDER_TYPE_BUY if is_buy else ORDER_TYPE_SELL,
                volume=o.volume_current, price_open=o.price_open,
                price_current=o.price_open, sl=o.sl, tp=o.tp,
                profit=0.0, time=sim_time,
            )
            self.positions[ticket] = pos
            self.deals[ticket] = [SimpleNamespace(
                entry=DEAL_ENTRY_IN, price=o.price_open, profit=0.0,
                reason=DEAL_REASON_CLIENT, commission=0.0, time=sim_time,
            )]

    def _check_sl_tp_closes(self, sim_time):
        to_close = []  # (ticket, price, reason)
        for ticket, pos in self.positions.items():
            is_buy = pos.type == ORDER_TYPE_BUY
            if is_buy:
                if pos.sl and self.bid <= pos.sl:
                    to_close.append((ticket, pos.sl, DEAL_REASON_SL))
                elif pos.tp and self.bid >= pos.tp:
                    to_close.append((ticket, pos.tp, DEAL_REASON_TP))
            else:
                if pos.sl and self.ask >= pos.sl:
                    to_close.append((ticket, pos.sl, DEAL_REASON_SL))
                elif pos.tp and self.ask <= pos.tp:
                    to_close.append((ticket, pos.tp, DEAL_REASON_TP))
        for ticket, price, reason in to_close:
            self._close_position(ticket, price, reason, sim_time)

    def _close_position(self, ticket, price, reason, sim_time, comment=""):
        pos = self.positions.pop(ticket, None)
        if pos is None:
            return
        is_buy = pos.type == ORDER_TYPE_BUY
        profit = self._profit_for(is_buy, pos.volume, pos.price_open, price)
        commission = self.commission_per_lot * pos.volume
        self.balance += profit - commission
        self.deals.setdefault(ticket, []).append(SimpleNamespace(
            entry=DEAL_ENTRY_OUT, price=price, profit=profit,
            reason=reason, commission=commission, time=sim_time,
        ))
        self.closed_log.append(dict(
            ticket=ticket, side="BUY" if is_buy else "SELL",
            volume=pos.volume, open_price=pos.price_open,
            close_price=price, profit=profit, commission=commission,
            reason={DEAL_REASON_SL: "sl", DEAL_REASON_TP: "tp"}.get(reason, "other"),
            time=sim_time,
        ))

    def close_all_at_market(self, sim_time, comment="manual"):
        for ticket in list(self.positions.keys()):
            pos = self.positions[ticket]
            price = self.bid if pos.type == ORDER_TYPE_BUY else self.ask
            self._close_position(ticket, price, DEAL_REASON_CLIENT, sim_time, comment)
        self.pending.clear()

    # ── order_send() dispatch ────────────────────────────────────────

    def order_send(self, request: dict):
        action = request.get("action")
        try:
            if action == TRADE_ACTION_PENDING:
                return self._send_pending(request)
            elif action == TRADE_ACTION_DEAL:
                return self._send_market(request)
            elif action == TRADE_ACTION_SLTP:
                return self._send_sltp(request)
            elif action == TRADE_ACTION_REMOVE:
                return self._send_remove(request)
        except Exception:
            pass
        return SimpleNamespace(retcode=-1, order=None, comment="unsupported action")

    def _new_ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _send_pending(self, request):
        ticket = self._new_ticket()
        o = SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], magic=request.get("magic", 0),
            type=request["type"], price_open=request["price"],
            sl=request.get("sl", 0.0), tp=request.get("tp", 0.0),
            volume_current=request["volume"], time=_time.time(),
            comment=request.get("comment", ""),
        )
        self.pending[ticket] = o
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="ok")

    def _send_market(self, request):
        # If a `position` ticket is present, this is a close/reduce
        # of an EXISTING position (full close, e.g. hard stop-loss /
        # balance TP / risk-free, or a partial close) — not a new
        # position. Without this check, a close-all request would
        # have silently opened a brand-new phantom position instead
        # of actually closing the real one.
        pos_ticket = request.get("position")
        if pos_ticket is not None and pos_ticket in self.positions:
            return self._close_or_reduce_position(request)

        ticket = self._new_ticket()
        is_buy = request["type"] == ORDER_TYPE_BUY
        price  = request["price"]
        pos = SimpleNamespace(
            ticket=ticket, symbol=request["symbol"], magic=request.get("magic", 0),
            type=request["type"], volume=request["volume"],
            price_open=price, price_current=price,
            sl=request.get("sl", 0.0), tp=request.get("tp", 0.0),
            profit=0.0, time=_time.time(),
        )
        self.positions[ticket] = pos
        self.deals[ticket] = [SimpleNamespace(
            entry=DEAL_ENTRY_IN, price=price, profit=0.0,
            reason=DEAL_REASON_CLIENT, commission=0.0, time=_time.time(),
        )]
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="ok")

    def _close_or_reduce_position(self, request):
        """
        Closes (fully or partially) an existing position. If the
        requested volume covers the whole position, it's a full close
        (same accounting as _close_position). If it's less, this is a
        partial exit: realizes profit on just that slice, reduces the
        position's remaining volume, and leaves it open.
        """
        ticket = request["position"]
        pos = self.positions[ticket]
        close_vol = request["volume"]
        price = request["price"]
        is_buy = pos.type == ORDER_TYPE_BUY
        sim_time = self.current_sim_time

        if close_vol >= pos.volume - 1e-9:
            self._close_position(ticket, price, DEAL_REASON_CLIENT, sim_time)
            return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="closed")

        profit = self._profit_for(is_buy, close_vol, pos.price_open, price)
        commission = self.commission_per_lot * close_vol
        self.balance += profit - commission
        pos.volume = round(pos.volume - close_vol, 2)
        self.deals.setdefault(ticket, []).append(SimpleNamespace(
            entry=DEAL_ENTRY_OUT, price=price, profit=profit,
            reason=DEAL_REASON_CLIENT, commission=commission, time=sim_time,
        ))
        self.closed_log.append(dict(
            ticket=ticket, side="BUY" if is_buy else "SELL",
            volume=close_vol, open_price=pos.price_open, close_price=price,
            profit=profit, commission=commission, reason="partial",
            time=sim_time,
        ))
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="partial")

    def _send_sltp(self, request):
        ticket = request.get("position")
        pos = self.positions.get(ticket)
        if pos is None:
            return SimpleNamespace(retcode=-1, order=None, comment="no position")
        pos.sl = request.get("sl", pos.sl)
        pos.tp = request.get("tp", pos.tp)
        return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="ok")

    def _send_remove(self, request):
        ticket = request.get("order")
        if ticket in self.pending:
            del self.pending[ticket]
            return SimpleNamespace(retcode=TRADE_RETCODE_DONE, order=ticket, comment="ok")
        return SimpleNamespace(retcode=-1, order=None, comment="not found")

    # ── Read-side queries (positions_get / orders_get / account_info) ─

    def get_positions(self):
        return list(self.positions.values())

    def get_orders(self):
        return list(self.pending.values())

    def get_account_info(self):
        eq = self.equity()
        used = self.margin_used()
        return SimpleNamespace(
            balance=self.balance, equity=eq,
            margin=used, margin_free=eq - used,
        )

    def get_deals(self, position_ticket):
        return self.deals.get(position_ticket, [])