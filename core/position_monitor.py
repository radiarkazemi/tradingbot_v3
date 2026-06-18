"""
position_monitor.py — TraderBot v2

No round limit. Runs until balance TP is hit or trader stops the bot.

LOGIC:
  Line touched → BUY-STOP @ line+dist  SL=line-dist (=SELL entry)
               + SELL-STOP @ line-dist  SL=line+dist (=BUY entry)

  ONE activates (e.g. BUY):
    → SELL-STOP lot = buy_lot × 2, SL stays exact

  SECOND activates:
    → Both positions open simultaneously

  When one position closes:
    → Place new stop same side at SAME fixed price
    → New lot = other_side_lot × 2
    → No limit on rounds

  Balance TP: balance ≥ start × ratio → close all & stop bot
"""
import MetaTrader5 as mt5
import logging
import time as _time
from config import MAGIC_NUMBER
from core.order_manager import send_pair, cancel_order, _filling_mode, _round_price


def _save(state):
    """Save session state — imported lazily to avoid circular import."""
    try:
        from core.resume import save_session
        save_session(state)
    except Exception:
        pass

log = logging.getLogger("monitor_v2")

ACTIVATION_GRACE_SEC = 5


class SourceState:
    IDLE      = "idle"
    PENDING   = "pending"
    ACTIVE    = "active"
    EXHAUSTED = "exhausted"

    def __init__(self, name, price, pip_size, symbol, base_lot, dist_pips,
                 start_balance=0.0, log_fn=None, stop_fn=None):
        self.name          = name
        self.price         = price
        self.pip_size      = pip_size
        self.symbol        = symbol
        self.base_lot      = base_lot
        self.dist_pips     = dist_pips
        self.start_balance = start_balance
        self._log          = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_fn      = stop_fn

        self.state           = self.IDLE
        self.round           = 0

        self.buy_ticket      = None
        self.sell_ticket     = None
        self.buy_pos_ticket  = None
        self.sell_pos_ticket = None

        self.buy_lot         = base_lot
        self.sell_lot        = base_lot
        self.buy_sl          = None
        self.sell_sl         = None

        self._buy_confirmed  = False
        self._sell_confirmed = False
        self._activated_at   = 0.0
        self._last_bid       = 0.0
        self._last_ask       = 0.0

        self.registered_at   = 0
        self.last_prev_t     = 0

        # ── Risk-free mechanism ───────────────────────────────────
        # Once an open position's floating profit reaches 2R (R =
        # the price distance to its own SL at entry), its SL is moved
        # to lock in +1R profit instead of just breakeven. After that
        # risk-free SL is applied, the position closing for ANY
        # reason (hit the new SL, manually closed, etc.) triggers a
        # full reset of this source back to IDLE — including chart
        # object cleanup, handled by the watcher layer via
        # needs_full_reset. The opposite pending stop order is left
        # completely untouched by this mechanism.
        self.risk_free_applied = {"buy": False, "sell": False}
        self.needs_full_reset  = False   # watcher checks/clears this

        # ── Tick-based touch detection (timeframe-immune) ─────────
        # Switching the MT5 chart's timeframe view changes what the EA
        # reports as CANDLE_T/PREV_T (different bar boundaries), which
        # broke touch-detection dedup and caused duplicate order pairs.
        # Touch detection now uses live tick price crossing instead of
        # any EA candle field, so it is completely unaffected by the
        # chart's displayed timeframe.
        self._prev_tick_price = None   # last seen mid price, for crossing detection

    # ── Fixed price properties (never drift) ──────────────────────
    @property
    def _dist(self):
        return self.dist_pips * self.pip_size

    @property
    def _buy_entry(self):
        return _round_price(self.price + self._dist, self.symbol)

    @property
    def _sell_entry(self):
        return _round_price(self.price - self._dist, self.symbol)

    @property
    def _buy_sl_price(self):   # SL of BUY  = SELL entry (exact mirror)
        return self._sell_entry

    @property
    def _sell_sl_price(self):  # SL of SELL = BUY entry (exact mirror)
        return self._buy_entry

    def _reanchor_buy(self, close_price: float):
        """
        Re-anchor self.price so _buy_entry recalculates to exactly
        close_price (the real SL fill price of the just-closed BUY
        position), eliminating slippage drift. Override in subclasses
        that don't derive entry/SL from self.price directly (e.g.
        FVGEntryState, which uses fvg_top/fvg_bottom instead).
        """
        self.price = close_price - self._dist

    def _reanchor_sell(self, close_price: float):
        """Re-anchor self.price so _sell_entry recalculates to exactly
        close_price. See _reanchor_buy."""
        self.price = close_price + self._dist

    @property
    def _dynamic_tp_pips(self) -> float:
        """
        TP distance in pips, growing as the martingale lot grows.

        base_tp_pips = dist_pips × 3  (same 1:3 RR convention used by
        MTF FVG entries elsewhere in the bot)

        Scales by the ratio of the CURRENT lot (whichever side is
        about to be placed/modified — callers read this once per side)
        to base_lot, so each time the lot doubles on a martingale round,
        the TP distance grows proportionally. A round-1 0.01 lot trade
        and a round-4 0.08 lot trade are not equally easy to recover
        from at the same pip distance — the larger the lot, the more
        room is given for the position to capture a real trending move
        before locking in profit, rather than scalping a now much
        bigger position for the same small price move.
        """
        base_tp_pips = self.dist_pips * 3
        if self.base_lot <= 0:
            return base_tp_pips
        # Use whichever lot is larger right now (covers the moment
        # right after one side activates and the other side's lot has
        # just been doubled, before both are equal again).
        current_lot = max(self.buy_lot, self.sell_lot, self.base_lot)
        ratio       = current_lot / self.base_lot
        return base_tp_pips * ratio

    @property
    def _buy_tp_price(self):
        """
        Dynamic TP that grows with lot size — see _dynamic_tp_pips.
        Base class previously had no TP at all (0.0, let it run with
        trend indefinitely); this keeps that "ride the trend" spirit
        but gives a concrete, ever-widening target as the position
        grows, rather than no target at all.
        """
        tp_dist = self._dynamic_tp_pips * self.pip_size
        return _round_price(self._buy_entry + tp_dist, self.symbol)

    @property
    def _sell_tp_price(self):
        """Dynamic TP that grows with lot size — see _dynamic_tp_pips."""
        tp_dist = self._dynamic_tp_pips * self.pip_size
        return _round_price(self._sell_entry - tp_dist, self.symbol)

    @property
    def _buy_r_distance(self) -> float:
        """1R for the BUY side = distance from BUY entry to BUY's SL."""
        return abs(self._buy_entry - self._buy_sl_price)

    @property
    def _sell_r_distance(self) -> float:
        """1R for the SELL side = distance from SELL entry to SELL's SL."""
        return abs(self._sell_entry - self._sell_sl_price)

    # ── Public API ────────────────────────────────────────────────

    def check_touch(self, bid: float, ask: float) -> bool:
        """
        Tick-based touch detection — timeframe-immune.

        Returns True (and transitions to PENDING via place_initial_pair)
        if price has touched self.price since the last tick we saw.

        Detects a touch two ways:
          1. Direct straddle: bid <= price <= ask right now
          2. Crossing: the mid price moved from one side of self.price
             to the other between the previous tick and this one
             (catches fast moves where price jumps over the line
             between two ticks without ever exactly straddling it)

        This does not depend on any EA-reported candle data, so it
        is unaffected by the user switching the MT5 chart's displayed
        timeframe mid-session.
        """
        if self.state != self.IDLE:
            return False
        if bid <= 0 or ask <= 0:
            return False

        mid = (bid + ask) / 2
        src = self.price

        touched = False
        desc    = ""

        # Direct straddle this tick
        if bid <= src <= ask:
            touched = True
            desc    = f"bid/ask straddle bid={bid:.5f} ask={ask:.5f}"

        # Crossing since last tick
        elif self._prev_tick_price is not None:
            prev = self._prev_tick_price
            if (prev < src <= mid) or (mid <= src < prev):
                touched = True
                desc    = f"crossed {prev:.5f}→{mid:.5f}"

        self._prev_tick_price = mid

        if touched:
            self._log(
                f"🎯  [{self.name[:20]}] touched @ {src:.5f} ({desc}) | "
                f"dist={self.dist_pips}pips | placing orders", "NEW"
            )
            self.place_initial_pair()
            return True

        return False

    def place_initial_pair(self):
        self.round    = 1
        self.buy_lot  = self.base_lot
        self.sell_lot = self.base_lot

        orders = [
            {"type": "BUY_STOP",  "entry": self._buy_entry,  "sl": self._buy_sl_price,
             "tp": self._buy_tp_price, "lot": self.buy_lot,  "source": self.price, "round": 1},
            {"type": "SELL_STOP", "entry": self._sell_entry, "sl": self._sell_sl_price,
             "tp": self._sell_tp_price, "lot": self.sell_lot, "source": self.price, "round": 1},
        ]
        results = send_pair(orders, self.symbol)

        self.buy_ticket  = None
        self.sell_ticket = None
        for r in results:
            if r["ok"]:
                if r["order"]["type"] == "BUY_STOP":
                    self.buy_ticket = r["ticket"]
                    self.buy_sl     = self._buy_sl_price
                else:
                    self.sell_ticket = r["ticket"]
                    self.sell_sl     = self._sell_sl_price

        if self.buy_ticket or self.sell_ticket:
            self.state = self.PENDING
            self._log(
                f"📌  [{self.name[:20]}] R1 pair placed | "
                f"BUY#{self.buy_ticket}@{self._buy_entry:.5f} "
                f"sl={self._buy_sl_price:.5f} lot={self.buy_lot:.2f} | "
                f"SELL#{self.sell_ticket}@{self._sell_entry:.5f} "
                f"sl={self._sell_sl_price:.5f} lot={self.sell_lot:.2f}", "NEW"
            )
        else:
            self._log(f"❌  [{self.name[:20]}] failed to place initial pair", "ERROR")

    def check(self, candle: dict):
        bid = candle.get("BID", 0.0)
        if bid:
            self._last_bid = bid
        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            self._last_bid = tick.bid
            self._last_ask = tick.ask

        if self.state in (self.IDLE, self.EXHAUSTED):
            return

        self._check_balance_tp()

        if self.state == self.PENDING:
            self._check_activation()
        elif self.state == self.ACTIVE:
            self._check_legs()

    # ── Balance TP ────────────────────────────────────────────────

    def _check_balance_tp(self):
        if self.start_balance <= 0:
            return
        try:
            import config as cfg
            ratio  = getattr(cfg, 'BALANCE_TP_RATIO', 1.10)
            info   = mt5.account_info()
            if not info:
                return
            target = self.start_balance * ratio
            if info.balance >= target:
                self._log(
                    f"🎯  [{self.name[:20]}] Balance TP! "
                    f"{info.balance:.2f} ≥ {target:.2f} — closing all & stopping", "NEW"
                )
                self._close_all_and_stop()
        except Exception as e:
            log.warning("Balance TP check error: %s", e)

    def _close_all_and_stop(self):
        filling = _filling_mode(self.symbol)
        tick    = mt5.symbol_info_tick(self.symbol)

        # Close all open positions
        for p in (mt5.positions_get(symbol=self.symbol) or []):
            if p.magic != MAGIC_NUMBER:
                continue
            is_buy = p.type == 0
            res = mt5.order_send({
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       p.volume,
                "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position":     p.ticket,
                "price":        tick.bid if is_buy else tick.ask,
                "deviation":    30,
                "magic":        MAGIC_NUMBER,
                "comment":      "TB2_BalTP",
                "type_filling": filling,
            })
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                self._log(f"✅  Closed #{p.ticket}", "NEW")
            else:
                self._log(f"⚠️  Failed to close #{p.ticket}", "WARN")

        # Cancel all pending orders
        for o in (mt5.orders_get(symbol=self.symbol) or []):
            if o.magic == MAGIC_NUMBER:
                cancel_order(o.ticket)

        # Delete saved start balance so next session starts fresh
        import os as _os
        _bal_file = f"start_balance_{self.symbol}.json"
        try:
            if _os.path.exists(_bal_file):
                _os.remove(_bal_file)
                self._log(f"🗑️  Cleared saved start balance (session complete)", "INFO")
        except Exception:
            pass

        self.state = self.EXHAUSTED

        # Signal the watcher to stop cleanly.
        # The watcher's _on_balance_tp() sets its stop event and emits
        # sig.emit_stop() so the GUI can stop FVG/OB/Confluence watchers
        # before mt5.shutdown() is called at the end of watcher.run().
        # DO NOT call mt5.shutdown() here — the connection must stay alive
        # until the watcher loop exits naturally.
        if self._stop_fn:
            self._stop_fn()

    # ── Activation ────────────────────────────────────────────────

    def _check_activation(self):
        pending   = {o.ticket for o in (mt5.orders_get(symbol=self.symbol) or [])
                     if o.magic == MAGIC_NUMBER}
        positions = mt5.positions_get(symbol=self.symbol) or []
        bot_pos   = [p for p in positions if p.magic == MAGIC_NUMBER]
        buy_pos   = [p for p in bot_pos if p.type == 0]
        sell_pos  = [p for p in bot_pos if p.type == 1]

        buy_still  = self.buy_ticket  in pending if self.buy_ticket  else False
        sell_still = self.sell_ticket in pending if self.sell_ticket else False
        buy_filled  = self.buy_ticket  is not None and not buy_still
        sell_filled = self.sell_ticket is not None and not sell_still

        if not buy_filled and not sell_filled:
            return

        self._activated_at   = _time.time()
        self._buy_confirmed  = False
        self._sell_confirmed = False

        if buy_filled:
            if buy_pos:
                pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
                self.buy_pos_ticket = pos.ticket
                self.buy_sl         = pos.sl
                self.buy_lot        = pos.volume
                self._buy_confirmed = True
            self.buy_ticket = None

        if sell_filled:
            if sell_pos:
                pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
                self.sell_pos_ticket = pos.ticket
                self.sell_sl         = pos.sl
                self.sell_lot        = pos.volume
                self._sell_confirmed = True
            self.sell_ticket = None

        if buy_filled and not sell_filled:
            new_sell_lot  = round(self.buy_lot * 2, 2)
            self.sell_lot = max(new_sell_lot, 0.01)
            if self.sell_ticket:
                self._modify_order_lot(self.sell_ticket, self.sell_lot,
                                       exact_sl=self._sell_sl_price)
            self._log(
                f"🟢  [{self.name[:20]}] R{self.round} BUY activated | "
                f"pos#{self.buy_pos_ticket} sl={self._buy_sl_price:.5f} | "
                f"SELL#{self.sell_ticket} lot → {self.sell_lot:.2f}", "NEW"
            )
        elif sell_filled and not buy_filled:
            new_buy_lot  = round(self.sell_lot * 2, 2)
            self.buy_lot = max(new_buy_lot, 0.01)
            if self.buy_ticket:
                if self.buy_ticket in pending:
                    self._modify_order_lot(self.buy_ticket, self.buy_lot,
                                           exact_sl=self._buy_sl_price)
                else:
                    self.buy_lot = buy_pos[0].volume if buy_pos else self.buy_lot
            self._log(
                f"🔴  [{self.name[:20]}] R{self.round} SELL activated | "
                f"pos#{self.sell_pos_ticket} sl={self._sell_sl_price:.5f} | "
                f"BUY#{self.buy_ticket} lot → {self.buy_lot:.2f}", "NEW"
            )
        else:
            self._log(
                f"🟢🔴  [{self.name[:20]}] R{self.round} BOTH activated | "
                f"BUY#{self.buy_pos_ticket} SELL#{self.sell_pos_ticket}", "NEW"
            )

        self.state = self.ACTIVE
        _save(self)

    # ── Active leg monitoring ─────────────────────────────────────

    def _check_legs(self):
        now      = _time.time()
        in_grace = (now - self._activated_at) < ACTIVATION_GRACE_SEC

        positions    = mt5.positions_get(symbol=self.symbol) or []
        bot_pos      = [p for p in positions if p.magic == MAGIC_NUMBER]
        open_tickets = {p.ticket for p in bot_pos}
        buy_pos      = [p for p in bot_pos if p.type == 0]
        sell_pos     = [p for p in bot_pos if p.type == 1]

        pending = {o.ticket for o in (mt5.orders_get(symbol=self.symbol) or [])
                   if o.magic == MAGIC_NUMBER}

        # ── Confirm unconfirmed positions ─────────────────────────
        if not self._buy_confirmed and self.buy_pos_ticket is None:
            if buy_pos:
                pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
                self.buy_pos_ticket = pos.ticket
                self.buy_sl         = pos.sl
                self.buy_lot        = pos.volume
                self._buy_confirmed = True
                self._log(
                    f"🔍  [{self.name[:20]}] BUY pos confirmed "
                    f"#{pos.ticket} sl={pos.sl}", "INFO"
                )

        if not self._sell_confirmed and self.sell_pos_ticket is None:
            if sell_pos:
                pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
                self.sell_pos_ticket = pos.ticket
                self.sell_sl         = pos.sl
                self.sell_lot        = pos.volume
                self._sell_confirmed = True
                self._log(
                    f"🔍  [{self.name[:20]}] SELL pos confirmed "
                    f"#{pos.ticket} sl={pos.sl}", "INFO"
                )

        # ── Second activation (pending stop filled) ───────────────
        if (self.buy_ticket and self.buy_ticket not in pending
                and not self._buy_confirmed):
            if buy_pos:
                pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
                self.buy_pos_ticket = pos.ticket
                self.buy_sl         = pos.sl
                self.buy_lot        = pos.volume
                self._buy_confirmed = True
                self.buy_ticket     = None
                new_sell_lot  = round(self.buy_lot * 2, 2)
                self.sell_lot = max(new_sell_lot, 0.01)
                if self.sell_ticket and self.sell_ticket in pending:
                    self._modify_order_lot(self.sell_ticket, self.sell_lot,
                                           exact_sl=self._sell_sl_price)
                self.round += 1
                self._log(
                    f"🟢  [{self.name[:20]}] R{self.round} BUY activated (2nd) | "
                    f"pos#{self.buy_pos_ticket} | SELL lot → {self.sell_lot:.2f}", "NEW"
                )
            else:
                self.buy_ticket = None

        if (self.sell_ticket and self.sell_ticket not in pending
                and not self._sell_confirmed):
            if sell_pos:
                pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
                self.sell_pos_ticket = pos.ticket
                self.sell_sl         = pos.sl
                self.sell_lot        = pos.volume
                self._sell_confirmed = True
                self.sell_ticket     = None
                new_buy_lot  = round(self.sell_lot * 2, 2)
                self.buy_lot = max(new_buy_lot, 0.01)
                if self.buy_ticket and self.buy_ticket in pending:
                    self._modify_order_lot(self.buy_ticket, self.buy_lot,
                                           exact_sl=self._buy_sl_price)
                self.round += 1
                self._log(
                    f"🔴  [{self.name[:20]}] R{self.round} SELL activated (2nd) | "
                    f"pos#{self.sell_pos_ticket} | BUY lot → {self.buy_lot:.2f}", "NEW"
                )
            else:
                self.sell_ticket = None

        if in_grace:
            return

        # ── Risk-free: move SL to lock +1R once floating profit ≥2R ─
        self._check_risk_free(buy_pos, sell_pos)

        # ── Detect closed positions → place new stop ──────────────
        # Pass the EXACT price the closed position's SL executed at,
        # so the new same-side pending order anchors to that real
        # fill price instead of recalculating from the original fixed
        # line/zone anchor. Slippage on the SL fill would otherwise
        # leave a small gap between where the position actually closed
        # and where the new order sits, adding delay before it can
        # trigger again.
        if (self.buy_pos_ticket
                and self.buy_pos_ticket not in open_tickets
                and self._buy_confirmed):
            closed_ticket = self.buy_pos_ticket
            close_price   = self._get_close_price(closed_ticket)
            was_risk_free = self.risk_free_applied.get("buy", False)
            self._log(
                f"📉  [{self.name[:20]}] BUY pos#{closed_ticket} closed"
                + (f" @ {close_price:.5f}" if close_price else "")
                + (" (was risk-free)" if was_risk_free else ""), "WARN"
            )
            self.buy_pos_ticket = None
            self._buy_confirmed = False
            if was_risk_free:
                # Risk-free profit was locked in — this slot is done.
                # Cancel the still-pending opposite stop, mark for a
                # full reset (chart object cleanup is the watcher's
                # job), and do NOT start a new recovery cycle.
                self._log(
                    f"🟢  [{self.name[:20]}] risk-free BUY closed — "
                    f"resetting to IDLE, waiting for a fresh entry", "NEW"
                )
                self.needs_full_reset = True
                self.reset()
            else:
                self._place_new_buy_stop(anchor_price=close_price)

        if (self.sell_pos_ticket
                and self.sell_pos_ticket not in open_tickets
                and self._sell_confirmed):
            closed_ticket = self.sell_pos_ticket
            close_price   = self._get_close_price(closed_ticket)
            was_risk_free = self.risk_free_applied.get("sell", False)
            self._log(
                f"📉  [{self.name[:20]}] SELL pos#{closed_ticket} closed"
                + (f" @ {close_price:.5f}" if close_price else "")
                + (" (was risk-free)" if was_risk_free else ""), "WARN"
            )
            self.sell_pos_ticket = None
            self._sell_confirmed = False
            if was_risk_free:
                self._log(
                    f"🔴  [{self.name[:20]}] risk-free SELL closed — "
                    f"resetting to IDLE, waiting for a fresh entry", "NEW"
                )
                self.needs_full_reset = True
                self.reset()
            else:
                self._place_new_sell_stop(anchor_price=close_price)

    def _check_risk_free(self, buy_pos: list, sell_pos: list):
        """
        Once an open position's floating profit reaches 2R, move its
        SL to lock in +1R instead of the original SL. R is the price
        distance between that side's entry and its own SL at entry
        time (computed once via _buy_r_distance/_sell_r_distance and
        cached in risk_free_applied so this only fires once per
        position). The opposite pending stop order is left untouched.
        """
        if buy_pos and not self.risk_free_applied.get("buy", False):
            pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
            r = self._buy_r_distance
            if r > 0:
                profit_dist = pos.price_current - pos.price_open
                if profit_dist >= 2 * r:
                    new_sl = _round_price(pos.price_open + 1 * r, self.symbol)
                    if self._move_position_sl(pos.ticket, new_sl):
                        self.risk_free_applied["buy"] = True
                        self._log(
                            f"🛡️  [{self.name[:20]}] BUY risk-free | "
                            f"profit={profit_dist:.5f} ≥ 2R={2*r:.5f} | "
                            f"SL moved to {new_sl:.5f} (+1R locked)", "NEW"
                        )

        if sell_pos and not self.risk_free_applied.get("sell", False):
            pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
            r = self._sell_r_distance
            if r > 0:
                profit_dist = pos.price_open - pos.price_current
                if profit_dist >= 2 * r:
                    new_sl = _round_price(pos.price_open - 1 * r, self.symbol)
                    if self._move_position_sl(pos.ticket, new_sl):
                        self.risk_free_applied["sell"] = True
                        self._log(
                            f"🛡️  [{self.name[:20]}] SELL risk-free | "
                            f"profit={profit_dist:.5f} ≥ 2R={2*r:.5f} | "
                            f"SL moved to {new_sl:.5f} (+1R locked)", "NEW"
                        )

    def _move_position_sl(self, ticket: int, new_sl: float) -> bool:
        """Modify an open position's SL via TRADE_ACTION_SLTP."""
        try:
            pos = next((p for p in (mt5.positions_get(symbol=self.symbol) or [])
                       if p.ticket == ticket), None)
            if not pos:
                return False
            res = mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   self.symbol,
                "position": ticket,
                "sl":       new_sl,
                "tp":       pos.tp,
                "magic":    MAGIC_NUMBER,
            })
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            self._log(
                f"⚠️  [{self.name[:20]}] risk-free SL move failed for #{ticket}: "
                f"{getattr(res, 'comment', 'unknown error')}", "WARN"
            )
            return False
        except Exception as e:
            log.warning("Risk-free SL move error: %s", e)
            return False

    def _get_close_price(self, position_ticket: int):
        """
        Fetch the exact execution price of the deal that closed this
        position (DEAL_ENTRY_OUT), so the next pending order can be
        anchored to reality instead of the original fixed line price.
        Returns None if the deal can't be found (falls back to the
        original anchor in that case).
        """
        try:
            deals = mt5.history_deals_get(position=position_ticket)
            if not deals:
                return None
            closing = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            if not closing:
                return None
            # Most recent closing deal for this position
            closing.sort(key=lambda d: d.time, reverse=True)
            return float(closing[0].price)
        except Exception as e:
            log.warning("Could not fetch close price for #%s: %s", position_ticket, e)
            return None

    # ── New stop placement ────────────────────────────────────────

    def _can_afford(self, lot: float, is_buy: bool) -> bool:
        try:
            action = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            tick   = mt5.symbol_info_tick(self.symbol)
            price  = tick.ask if is_buy else tick.bid
            margin = mt5.order_calc_margin(action, self.symbol, lot, price)
            acct   = mt5.account_info()
            if margin is None or acct is None:
                return True
            free_margin   = acct.margin_free
            equity        = acct.equity
            safety_margin = equity * 0.20
            if free_margin - margin < safety_margin:
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} MARGIN PROTECTION | "
                    f"lot={lot:.2f} needs ${margin:.2f} margin | "
                    f"free=${free_margin:.2f} equity=${equity:.2f} | "
                    f"skipping new order — keeping existing position open", "WARN"
                )
                return False
            return True
        except Exception as e:
            log.warning("Margin check error: %s", e)
            return True

    def _place_new_buy_stop(self, anchor_price: float = None):
        self.round  += 1
        new_buy_lot  = round(self.sell_lot * 2, 2)
        self.buy_lot = max(new_buy_lot, 0.01)

        if not self._can_afford(self.buy_lot, is_buy=True):
            return

        # Re-anchor from the EXACT price the previous BUY position's
        # SL closed at (rather than the original fixed line/zone
        # price), eliminating any slippage-induced gap. Delegated to
        # _reanchor_buy() since subclasses (e.g. FVGEntryState) derive
        # entry/SL/TP from different fields than self.price and need
        # their own re-anchoring logic.
        if anchor_price is not None:
            self._reanchor_buy(anchor_price)

        order = {"type": "BUY_STOP", "entry": self._buy_entry,
                 "sl": self._buy_sl_price, "tp": self._buy_tp_price,
                 "lot": self.buy_lot, "source": self.price, "round": self.round}
        results = send_pair([order], self.symbol)
        ok = [r for r in results if r["ok"]]

        if ok:
            self.buy_ticket = ok[0]["ticket"]
            new_sell_lot  = round(self.buy_lot * 2, 2)
            self.sell_lot = max(new_sell_lot, 0.01)
            pending = {o.ticket for o in (mt5.orders_get(symbol=self.symbol) or [])
                       if o.magic == MAGIC_NUMBER}
            if self.sell_ticket and self.sell_ticket in pending:
                self._modify_order_lot(self.sell_ticket, self.sell_lot,
                                       exact_sl=self._sell_sl_price)
            self._log(
                f"🔁  [{self.name[:20]}] R{self.round} new BUY-STOP | "
                f"entry={self._buy_entry:.5f} sl={self._buy_sl_price:.5f} "
                f"lot={self.buy_lot:.2f} | SELL lot → {self.sell_lot:.2f}", "NEW"
            )
            _save(self)

    def _place_new_sell_stop(self, anchor_price: float = None):
        self.round   += 1
        new_sell_lot  = round(self.buy_lot * 2, 2)
        self.sell_lot = max(new_sell_lot, 0.01)

        if not self._can_afford(self.sell_lot, is_buy=False):
            return

        # Re-anchor from the EXACT price the previous SELL position's
        # SL closed at — see matching comment in _place_new_buy_stop.
        if anchor_price is not None:
            self._reanchor_sell(anchor_price)

        order = {"type": "SELL_STOP", "entry": self._sell_entry,
                 "sl": self._sell_sl_price, "tp": self._sell_tp_price,
                 "lot": self.sell_lot, "source": self.price, "round": self.round}
        results = send_pair([order], self.symbol)
        ok = [r for r in results if r["ok"]]

        if ok:
            self.sell_ticket = ok[0]["ticket"]
            new_buy_lot  = round(self.sell_lot * 2, 2)
            self.buy_lot = max(new_buy_lot, 0.01)
            pending = {o.ticket for o in (mt5.orders_get(symbol=self.symbol) or [])
                       if o.magic == MAGIC_NUMBER}
            if self.buy_ticket and self.buy_ticket in pending:
                self._modify_order_lot(self.buy_ticket, self.buy_lot,
                                       exact_sl=self._buy_sl_price)
            self._log(
                f"🔁  [{self.name[:20]}] R{self.round} new SELL-STOP | "
                f"entry={self._sell_entry:.5f} sl={self._sell_sl_price:.5f} "
                f"lot={self.sell_lot:.2f} | BUY lot → {self.buy_lot:.2f}", "NEW"
            )
            _save(self)

    # ── Order lot modification ────────────────────────────────────

    def _modify_order_lot(self, ticket: int, new_lot: float,
                          exact_sl: float = None) -> bool:
        orders = mt5.orders_get(symbol=self.symbol) or []
        target = next((o for o in orders if o.ticket == ticket), None)
        if not target:
            self._log(
                f"ℹ️  [{self.name[:20]}] order #{ticket} already filled", "INFO"
            )
            return False

        cancel_order(ticket)

        is_buy     = target.type == mt5.ORDER_TYPE_BUY_STOP
        order_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
        filling    = _filling_mode(self.symbol)
        use_sl     = exact_sl if exact_sl is not None else target.sl
        entry      = target.price_open

        tick         = mt5.symbol_info_tick(self.symbol)
        bid          = tick.bid if tick else 0.0
        ask          = tick.ask if tick else 0.0
        already_past = (is_buy and ask > 0 and entry <= ask) or \
                       (not is_buy and bid > 0 and entry >= bid)

        if already_past:
            market_price = ask if is_buy else bid
            use_tp = self._buy_tp_price if is_buy else self._sell_tp_price
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       new_lot,
                "type":         mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price":        market_price,
                "sl":           use_sl,
                "tp":           use_tp,
                "deviation":    30,
                "magic":        MAGIC_NUMBER,
                "comment":      (target.comment or "") + "m",
                "type_filling": filling,
            }
            self._log(
                f"⚡  [{self.name[:20]}] {'BUY' if is_buy else 'SELL'} past market — "
                f"MARKET lot={new_lot:.2f} sl={use_sl:.5f}", "WARN"
            )
        else:
            use_tp = self._buy_tp_price if is_buy else self._sell_tp_price
            request = {
                "action":       mt5.TRADE_ACTION_PENDING,
                "symbol":       self.symbol,
                "volume":       new_lot,
                "type":         order_type,
                "price":        entry,
                "sl":           use_sl,
                "tp":           use_tp,
                "deviation":    20,
                "magic":        MAGIC_NUMBER,
                "comment":      (target.comment or "") + "m",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }

        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            if is_buy:
                self.buy_ticket = res.order
            else:
                self.sell_ticket = res.order
            self._log(
                f"✏️  [{self.name[:20]}] {'BUY' if is_buy else 'SELL'}-STOP modified | "
                f"ticket#{res.order} lot={new_lot:.2f} sl={use_sl:.5f} @ {entry:.5f}",
                "INFO"
            )
            return True
        else:
            self._log(
                f"❌  Modify failed: {res.retcode if res else '?'}", "ERROR"
            )
            return False

    # ── Reset ─────────────────────────────────────────────────────

    def reset(self):
        for ticket in [self.buy_ticket, self.sell_ticket]:
            if ticket:
                cancel_order(ticket)
        self.buy_ticket      = None
        self.sell_ticket     = None
        self.buy_pos_ticket  = None
        self.sell_pos_ticket = None
        self.buy_lot         = self.base_lot
        self.sell_lot        = self.base_lot
        self.buy_sl          = None
        self.sell_sl         = None
        self.round           = 0
        self.state           = self.IDLE
        self._buy_confirmed  = False
        self._sell_confirmed = False
        self._log(f"🔄  [{self.name[:20]}] state reset to IDLE")
        try:
            from core.resume import clear_session
            clear_session(self.symbol)
        except Exception:
            pass

    @property
    def summary(self) -> dict:
        return {
            "name":      self.name,
            "price":     self.price,
            "state":     self.state,
            "round":     self.round,
            "direction": f"B:{self.buy_lot:.2f} S:{self.sell_lot:.2f}",
            "lot":       self.buy_lot,
            "buy_lot":   self.buy_lot,
            "sell_lot":  self.sell_lot,
        }