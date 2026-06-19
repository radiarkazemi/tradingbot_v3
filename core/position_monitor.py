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
                 start_balance=0.0, log_fn=None, stop_fn=None,
                 risk_free_enabled=False):
        self.name          = name
        self.price         = price
        self.pip_size      = pip_size
        self.symbol        = symbol
        self.base_lot      = base_lot
        self.dist_pips     = dist_pips
        self.start_balance = start_balance
        self._log          = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_fn      = stop_fn
        self._risk_free_enabled = risk_free_enabled

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
        self.buy_r_frozen    = 0.0
        self.sell_r_frozen   = 0.0

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

        # ── Cumulative loss tracking (for TP sizing) ──────────────
        # Tracks total dollar loss across all closed losing positions
        # in this martingale cycle. Reset to 0 on a full reset (win
        # or risk-free close). Used by _balance_target_tp_pips to
        # set TP based on actual loss accumulated, not a theoretical
        # estimate — the TP at each round must cover the real losses
        # already taken, plus the current SL risk, times the target RR.
        self.cumulative_loss = 0.0
        self._pip_value_per_base_lot = 0.0  # calibrated from real SL closes

        # Original chart-object name before any auto-relocate rename —
        # used by watcher.py to remove the original chart-object-backed
        # source when this one auto-relocates, preventing duplicate
        # position placement if the chart line ends up near the new FVG.
        self.original_chart_name = name

        # ── Auto-relocate (TP win / risk-free → fresh FVG) ──────────
        # When _relocate_to_fresh_fvg() renames this instance to a
        # synthetic key (so the watcher's chart-object move-detection
        # never matches and reverts it), it stashes the new name here.
        # watcher.py must read this once per cycle, re-key its
        # self._sources dict entry (old name → new name), and clear
        # this back to None — that re-keying can't happen from inside
        # SourceState itself since it has no reference to that dict.
        self.pending_rename     = None
        self._is_auto_relocated = False
        self._relocated_at         = 0.0
        self._relocated_from_price = None

        # ── Tick-based touch detection (timeframe-immune) ─────────
        # Switching the MT5 chart's timeframe view changes what the EA
        # reports as CANDLE_T/PREV_T (different bar boundaries), which
        # broke touch-detection dedup and caused duplicate order pairs.
        # Touch detection now uses live tick price crossing instead of
        # any EA candle field, so it is completely unaffected by the
        # chart's displayed timeframe.
        self._prev_tick_price = None   # last seen mid price, for crossing detection

        self._log(
            f"⚙️  [{self.name[:20]}] risk_free_enabled = {self._risk_free_enabled}",
            "INFO"
        )

    # ── Fixed price properties (never drift) ──────────────────────
    @property
    def _dist(self):
        return self.dist_pips * self.pip_size

    def _current_spread(self) -> float:
        """
        Current bid-ask spread in price units, fetched live from MT5.
        Used to compensate order entry prices so the EFFECTIVE fill
        price (what MT5 actually executes at) matches the intended
        level exactly, regardless of current spread width.

        BUY_STOP fills at ask: to get a fill at `intended`, place the
        stop at `intended − spread` so that when bid reaches
        (intended − spread), ask = intended and the fill is exact.

        SELL_STOP fills at bid: to get a fill at `intended`, place the
        stop at `intended + spread` so that when ask reaches
        (intended + spread), bid = intended and the fill is exact.

        Returns 0.0 on failure (no compensation applied, safe fallback).
        """
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if tick and tick.ask > 0 and tick.bid > 0:
                return round(tick.ask - tick.bid, 5)
        except Exception:
            pass
        return 0.0

    @property
    def _buy_entry(self):
        """BUY_STOP entry price, spread-compensated so the effective
        MT5 fill lands exactly at price + dist regardless of spread."""
        raw   = _round_price(self.price + self._dist, self.symbol)
        spread = self._current_spread()
        return _round_price(raw - spread, self.symbol)

    @property
    def _sell_entry(self):
        """SELL_STOP entry price, spread-compensated so the effective
        MT5 fill lands exactly at price − dist regardless of spread."""
        raw   = _round_price(self.price - self._dist, self.symbol)
        spread = self._current_spread()
        return _round_price(raw + spread, self.symbol)

    @property
    def _buy_sl_price(self):
        """
        SL of BUY = EXACTLY the SELL side's real entry price
        (_sell_entry, including its spread compensation) — not the
        theoretical pre-spread line position. This is what makes BUY's
        SL land exactly where SELL actually fills, with zero gap,
        matching the bot's whole "zero spread" design intent: if the
        SELL position is sitting at 4134.97, BUY's SL must be 4134.97,
        not some epsilon above or below it.
        """
        return self._sell_entry

    @property
    def _sell_sl_price(self):
        """SL of SELL = EXACTLY the BUY side's real entry price
        (_buy_entry). See _buy_sl_price — same reasoning, mirrored."""
        return self._buy_entry

    def _reanchor_buy(self, close_price: float):
        """
        No-op in the base class. self.price is the fixed main-line
        anchor and must NEVER move — every new recovery order keeps
        the same configured distance from it (set on the GUI), and
        BUY/SELL SL mirroring (_buy_sl_price == _sell_entry and vice
        versa) is already exact by construction since both sides
        derive from this one shared, unmoving anchor. There is no
        slippage gap to correct here: SL price and the opposite
        side's entry price are literally the same computed value,
        not two independently-sourced numbers that could drift apart.
        (Earlier in this project's history this method moved
        self.price to the real SL fill price — that broke things: it
        shifted the shared anchor, which could land a brand-new
        recovery order's ENTRY exactly on top of the opposite side's
        already-open position. Kept as a hook only so subclasses with
        genuinely independent edges, like FVGEntryState, can still
        override it meaningfully.)
        """
        pass

    def _reanchor_sell(self, close_price: float):
        """No-op in the base class. See _reanchor_buy."""
        pass

    def _dollar_per_pip(self, lot: float) -> float:
        """
        Dollar profit per 1 pip per given lot size, in account currency.
        Primary: mt5.order_calc_profit() — broker's own engine.
        Fallback: derive from trade_tick_value / trade_tick_size.
        Emergency fallback: use self._pip_value_cache if we already
        calibrated from a real closed position (see _calibrate_pip_value).
        Returns 0.0 only if everything fails — caller must guard.
        """
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            price = (tick.bid + tick.ask) / 2.0 if tick else self.price
            profit = mt5.order_calc_profit(
                mt5.ORDER_TYPE_BUY, self.symbol, lot, price, price + self.pip_size
            )
            if profit is not None and profit > 0:
                return float(profit)
        except Exception:
            pass

        try:
            info = mt5.symbol_info(self.symbol)
            if info and info.trade_tick_size > 0:
                return (info.trade_tick_value / info.trade_tick_size) * self.pip_size * lot
        except Exception:
            pass

        # Last resort: use cached value from a real closed SL, normalised to lot
        if getattr(self, '_pip_value_per_base_lot', 0.0) > 0:
            return self._pip_value_per_base_lot * lot
        return 0.0

    def _calibrate_pip_value(self, closed_dollar_loss: float, closed_lot: float):
        """
        Back-calculate dollar-per-pip-per-base-lot from a real SL close.
        SL distance = 2 × dist_pips (the mirror geometry — entry to SL
        is exactly dist from the line, same as the opposite entry, so
        the full SL move = 2 × dist_pips in price terms).
        Stored in _pip_value_per_base_lot so _dollar_per_pip can use it
        as a reliable fallback even when order_calc_profit returns None.
        """
        sl_pips = self.dist_pips * 2
        if sl_pips > 0 and closed_lot > 0 and closed_dollar_loss > 0:
            self._pip_value_per_base_lot = (
                closed_dollar_loss / sl_pips / closed_lot * self.base_lot
            )
            self._log(
                f"📐  [{self.name[:20]}] pip value calibrated from real SL: "
                f"${self._pip_value_per_base_lot:.4f}/pip/base_lot "
                f"(loss=${closed_dollar_loss:.2f} lot={closed_lot:.2f})", "INFO"
            )

    @property
    def _tp_pips(self) -> float:
        """
        TP distance in pips, sized to cover ALL losses accumulated so
        far in this martingale cycle plus an equal profit on top —
        minimum 1:2 RR (cover loss + win the same again), targeting
        1:3 (cover loss + win 2× the total loss).

        Formula: tp_pips = (cumulative_loss + current_sl_risk) × RR
                           / dollar_per_pip(current_lot)

        Where:
          cumulative_loss   = sum of all real dollar losses closed so
                              far in this cycle (tracked in self.cumulative_loss)
          current_sl_risk   = what this current position would lose if
                              its own SL is hit next
          RR                = 3 (1:3 target; degrades to 2 if 3 would
                              put tp_pips above a reasonable ceiling)
          dollar_per_pip    = real broker-side pip value at current lot

        Hard floor: dist_pips × 3 (the original 1:3 of the first round,
        ensuring the very first round always has a sane minimum target).
        Hard ceiling: none — we let the RR math determine the distance,
        since capping it is exactly what caused the TP to never move.
        """
        current_lot = max(self.buy_lot, self.sell_lot, self.base_lot)
        dpp = self._dollar_per_pip(current_lot)

        # Minimum floor = dist_pips × 3, always achievable on round 1
        floor_pips = self.dist_pips * 3

        if dpp <= 0:
            self._log(
                f"⚠️  [{self.name[:20]}] _dollar_per_pip returned 0 "
                f"(order_calc_profit unavailable?) — using floor {floor_pips}p",
                "WARN"
            )
            return floor_pips

        # Current round's SL risk in dollars
        sl_pips = self.dist_pips * 2
        current_sl_risk = dpp * sl_pips  # if this position's SL is hit next

        total_at_risk = self.cumulative_loss + current_sl_risk

        # Try 1:3 first; if it puts TP unreasonably far (>200p) fall
        # back to 1:2. This preserves the martingale's ability to
        # actually recover even on deep runs.
        for rr in (3, 2):
            tp_pips = (total_at_risk * rr) / dpp
            if tp_pips <= 200.0:
                return max(tp_pips, floor_pips)

        # Even 1:2 is > 200 pips — use 200p ceiling but never below floor
        return max(200.0, floor_pips)

    @property
    def _buy_tp_price(self):
        tp_dist = self._tp_pips * self.pip_size
        return _round_price(self._buy_entry + tp_dist, self.symbol)

    @property
    def _sell_tp_price(self):
        tp_dist = self._tp_pips * self.pip_size
        return _round_price(self._sell_entry - tp_dist, self.symbol)

    @property
    def _buy_r_distance(self) -> float:
        """
        1R for the BUY side = the fixed structural distance from
        BUY's intended entry (price + dist) to BUY's SL (price - dist)
        = 2 × dist_pips, in price units.

        IMPORTANT: this must NOT be computed from _buy_entry/_buy_sl_price
        directly — _buy_entry now includes live spread compensation
        (see _buy_entry), which fluctuates tick-to-tick. Using it here
        would make R a moving target instead of the fixed risk size
        the position was actually opened with, and could silently
        shrink the 2R trigger threshold or report a value that
        doesn't match what really happened. R is always exactly
        2 × self._dist for the base class's symmetric mirror geometry.
        """
        return abs(2 * self._dist)

    @property
    def _sell_r_distance(self) -> float:
        """1R for the SELL side. See _buy_r_distance — same reasoning,
        same fixed value (2 × self._dist) for the base class."""
        return abs(2 * self._dist)

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

        # ── Staleness check: re-relocate if price ran away and never
        # came back to touch this auto-relocated line ────────────────
        # Only applies to sources that got here via auto-relocate
        # (TP win / risk-free close → fresh FVG) — never to manual
        # lines you drew yourself, which should wait indefinitely for
        # your line regardless of how far price has wandered.
        if self._is_auto_relocated and self._relocated_from_price is not None:
            mid_now = (bid + ask) / 2
            dist_from_line_pips = abs(mid_now - self._relocated_from_price) / self.pip_size
            age_sec = _time.time() - self._relocated_at

            # Distance trigger: price ran far enough away that this
            # FVG context is stale — a closer, fresher gap almost
            # certainly exists now. Threshold scales with the
            # configured order distance so it adapts to symbol/timeframe
            # instead of a hardcoded pip count.
            distance_trigger = dist_from_line_pips >= max(self.dist_pips * 4, 20.0)

            # Time trigger: backstop for a dead/ranging market that
            # never moves far OR close enough to either resolve —
            # 30 minutes is long enough to not fight normal consolidation.
            time_trigger = age_sec >= 1800

            if distance_trigger or time_trigger:
                reason = (f"price ran {dist_from_line_pips:.1f}p away"
                         if distance_trigger else
                         f"{age_sec/60:.0f}min with no touch")
                self._log(
                    f"♻️  [{self.name[:20]}] stale relocation ({reason}) — "
                    f"finding a fresher FVG", "INFO"
                )
                self._relocate_to_fresh_fvg()
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

        # ── Both legs placed: normal success path ──────────────────
        if self.buy_ticket and self.sell_ticket:
            self.state = self.PENDING
            self._log(
                f"📌  [{self.name[:20]}] R1 pair placed | "
                f"BUY#{self.buy_ticket}@{self._buy_entry:.5f} "
                f"sl={self._buy_sl_price:.5f} lot={self.buy_lot:.2f} | "
                f"SELL#{self.sell_ticket}@{self._sell_entry:.5f} "
                f"sl={self._sell_sl_price:.5f} lot={self.sell_lot:.2f}", "NEW"
            )
            return

        # ── Neither leg placed: clean failure, stay IDLE ───────────
        if not self.buy_ticket and not self.sell_ticket:
            self._log(f"❌  [{self.name[:20]}] failed to place initial pair "
                      f"(both legs failed)", "ERROR")
            return

        # ── Exactly ONE leg placed: retry the missing leg a few times
        # before giving up. A single-leg "pair" defeats the whole
        # martingale design (no opposite hedge to activate on
        # recovery), so this must not be silently treated as success.
        missing_side = "SELL_STOP" if self.buy_ticket else "BUY_STOP"
        self._log(
            f"⚠️  [{self.name[:20]}] {missing_side} failed to place — "
            f"retrying ({'BUY' if self.buy_ticket else 'SELL'} leg already "
            f"placed)", "WARN"
        )

        MAX_RETRIES = 3
        got_missing_leg = False
        for attempt in range(1, MAX_RETRIES + 1):
            _time.sleep(1.0)
            if missing_side == "SELL_STOP":
                missing_order = {"type": "SELL_STOP", "entry": self._sell_entry,
                                 "sl": self._sell_sl_price, "tp": self._sell_tp_price,
                                 "lot": self.sell_lot, "source": self.price, "round": 1}
            else:
                missing_order = {"type": "BUY_STOP", "entry": self._buy_entry,
                                 "sl": self._buy_sl_price, "tp": self._buy_tp_price,
                                 "lot": self.buy_lot, "source": self.price, "round": 1}

            retry_results = send_pair([missing_order], self.symbol)
            ok = [r for r in retry_results if r["ok"]]
            if ok:
                if missing_side == "SELL_STOP":
                    self.sell_ticket = ok[0]["ticket"]
                    self.sell_sl     = self._sell_sl_price
                else:
                    self.buy_ticket = ok[0]["ticket"]
                    self.buy_sl     = self._buy_sl_price
                got_missing_leg = True
                self._log(
                    f"✅  [{self.name[:20]}] {missing_side} retry succeeded "
                    f"(attempt {attempt}/{MAX_RETRIES})", "NEW"
                )
                break
            self._log(
                f"⚠️  [{self.name[:20]}] {missing_side} retry "
                f"{attempt}/{MAX_RETRIES} failed", "WARN"
            )

        if got_missing_leg:
            self.state = self.PENDING
            self._log(
                f"📌  [{self.name[:20]}] R1 pair placed (after retry) | "
                f"BUY#{self.buy_ticket}@{self._buy_entry:.5f} "
                f"sl={self._buy_sl_price:.5f} lot={self.buy_lot:.2f} | "
                f"SELL#{self.sell_ticket}@{self._sell_entry:.5f} "
                f"sl={self._sell_sl_price:.5f} lot={self.sell_lot:.2f}", "NEW"
            )
            return

        # ── Retries exhausted: cancel the lone leg and reset cleanly,
        # rather than running with only half a pair (no hedge at all).
        self._log(
            f"❌  [{self.name[:20]}] {missing_side} could not be placed "
            f"after {MAX_RETRIES} retries — cancelling lone leg and "
            f"resetting to IDLE", "ERROR"
        )
        if self.buy_ticket:
            cancel_order(self.buy_ticket)
        if self.sell_ticket:
            cancel_order(self.sell_ticket)
        self.reset()

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
                self.buy_r_frozen   = abs(pos.price_open - pos.sl)
                self.buy_lot        = pos.volume
                self._buy_confirmed = True
            self.buy_ticket = None

        if sell_filled:
            if sell_pos:
                pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
                self.sell_pos_ticket = pos.ticket
                self.sell_sl         = pos.sl
                self.sell_r_frozen   = abs(pos.price_open - pos.sl)
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
                self.buy_r_frozen   = abs(pos.price_open - pos.sl)
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
                self.sell_r_frozen   = abs(pos.price_open - pos.sl)
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
                self.buy_r_frozen   = abs(pos.price_open - pos.sl)
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
                self.sell_r_frozen   = abs(pos.price_open - pos.sl)
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

        # ── Keep each open position's TP synced to the balance-target
        # gap as it shrinks/grows (lot changes, balance moves from a
        # sibling line, etc.) — see _resync_open_tp.
        self._resync_open_tp(buy_pos, sell_pos)
        self._resync_open_sl(buy_pos, sell_pos)

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
            close_price, close_reason = self._get_close_info(closed_ticket)
            was_risk_free = self.risk_free_applied.get("buy", False)
            self._log(
                f"📉  [{self.name[:20]}] BUY pos#{closed_ticket} closed"
                + (f" @ {close_price:.5f}" if close_price else "")
                + (f" ({close_reason})" if close_reason else "")
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
                self._relocate_to_fresh_fvg()
            elif close_reason == "tp":
                # Round WON outright via take-profit — the martingale
                # cycle is complete and successful. Do NOT chain into
                # another recovery order at a bigger lot; that would
                # treat a win exactly like a loss and risk a fresh
                # loss eating into profit that's already locked in.
                self._log(
                    f"🏆  [{self.name[:20]}] BUY hit TP — round won, "
                    f"resetting to IDLE, waiting for a fresh entry", "NEW"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
            else:
                # SL hit (or unknown) — accumulate real loss and
                # calibrate pip value from the real close, then recover.
                real_loss = self._get_real_loss(closed_ticket)
                if real_loss > 0:
                    self.cumulative_loss += real_loss
                    self._calibrate_pip_value(real_loss, self.buy_lot)
                self._place_new_buy_stop(anchor_price=close_price)

        if (self.sell_pos_ticket
                and self.sell_pos_ticket not in open_tickets
                and self._sell_confirmed):
            closed_ticket = self.sell_pos_ticket
            close_price, close_reason = self._get_close_info(closed_ticket)
            was_risk_free = self.risk_free_applied.get("sell", False)
            self._log(
                f"📉  [{self.name[:20]}] SELL pos#{closed_ticket} closed"
                + (f" @ {close_price:.5f}" if close_price else "")
                + (f" ({close_reason})" if close_reason else "")
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
                self._relocate_to_fresh_fvg()
            elif close_reason == "tp":
                self._log(
                    f"🏆  [{self.name[:20]}] SELL hit TP — round won, "
                    f"resetting to IDLE, waiting for a fresh entry", "NEW"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
            else:
                real_loss = self._get_real_loss(closed_ticket)
                if real_loss > 0:
                    self.cumulative_loss += real_loss
                    self._calibrate_pip_value(real_loss, self.sell_lot)
                self._place_new_sell_stop(anchor_price=close_price)

    def _resync_open_sl(self, buy_pos: list, sell_pos: list):
        """
        Re-send TRADE_ACTION_SLTP for any open position whose live SL
        no longer matches the opposite side's REAL current entry price
        (_sell_entry / _buy_entry, both spread-compensated and live).
        This is what makes BUY's SL track exactly where SELL is
        actually sitting right now, and vice versa — the "zero gap"
        mirror the bot is designed around. Spread moves tick to tick,
        so this can re-send fairly often; that's accepted as the cost
        of an exact, always-current mirror rather than a stale one.

        Skipped for a side once risk-free has been applied to it —
        from that point its SL represents the locked-in +1R profit
        level, owned exclusively by _check_risk_free.
        """
        if buy_pos and not self.risk_free_applied.get("buy", False):
            pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
            target_sl = self._buy_sl_price
            if abs(pos.sl - target_sl) > self.pip_size * 0.9:
                self._move_position_sl(pos.ticket, target_sl)

        if sell_pos and not self.risk_free_applied.get("sell", False):
            pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
            target_sl = self._sell_sl_price
            if abs(pos.sl - target_sl) > self.pip_size * 0.9:
                self._move_position_sl(pos.ticket, target_sl)

    def _resync_open_tp(self, buy_pos: list, sell_pos: list):
        """
        Re-send TRADE_ACTION_SLTP for any open position whose live TP
        no longer matches the freshly-computed balance-target TP (see
        _balance_target_tp_pips). The gap to the GUI's balance-TP%
        target shrinks every round as lot size grows, and can also
        shift from balance changes elsewhere (a sibling line/round
        winning or losing) — so a TP set once at entry time can go
        stale. This keeps it live-adjusted on every poll while the
        position is open.

        Skipped for a side once risk-free has been applied to it —
        at that point its SL/TP no longer represent the balance-
        target goal, they represent the locked-in +1R profit level,
        which _check_risk_free owns exclusively from then on.
        """
        if buy_pos and not self.risk_free_applied.get("buy", False):
            pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
            target_tp = self._buy_tp_price
            # Only bother re-sending if the change is more than a
            # rounding/float-noise difference, to avoid spamming
            # order_send every single scan for a no-op change.
            if abs(pos.tp - target_tp) > self.pip_size * 0.9:
                self._move_position_tp(pos.ticket, target_tp)

        if sell_pos and not self.risk_free_applied.get("sell", False):
            pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
            target_tp = self._sell_tp_price
            if abs(pos.tp - target_tp) > self.pip_size * 0.9:
                self._move_position_tp(pos.ticket, target_tp)

    def _move_position_tp(self, ticket: int, new_tp: float) -> bool:
        """Modify an open position's TP via TRADE_ACTION_SLTP, keeping
        its current SL untouched."""
        try:
            pos = next((p for p in (mt5.positions_get(symbol=self.symbol) or [])
                       if p.ticket == ticket), None)
            if not pos:
                return False
            res = mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   self.symbol,
                "position": ticket,
                "sl":       pos.sl,
                "tp":       new_tp,
                "magic":    MAGIC_NUMBER,
            })
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                self._log(
                    f"🎯  [{self.name[:20]}] #{ticket} TP adjusted to "
                    f"{new_tp:.5f} (balance-target gap update)", "INFO"
                )
                return True
            self._log(
                f"⚠️  [{self.name[:20]}] TP resync failed for #{ticket}: "
                f"{getattr(res, 'comment', 'unknown error')}", "WARN"
            )
            return False
        except Exception as e:
            log.warning("TP resync error: %s", e)
            return False

    def _check_risk_free(self, buy_pos: list, sell_pos: list):
        """
        Once an open position's floating profit reaches 2R, move its
        SL to lock in enough profit to cover ALL cumulative losses
        taken so far this martingale cycle PLUS the current
        position's own risk — not just +1R of the current round
        alone. A flat +1R badly under-covers deep martingale runs:
        by the time lot has doubled 6-7 times, cumulative_loss can be
        many multiples of any single round's R, so locking only that
        round's R leaves the cycle's real, larger loss uncovered.

        2R TRIGGER (when to act) still uses the position's own frozen
        R (self.buy_r_frozen/sell_r_frozen) — that's the right basis
        for "has this round itself moved favorably enough to act."

        LOCK-IN AMOUNT (how far to move SL) uses:
            total_at_risk_dollars = cumulative_loss + (R in dollars
                                    at the position's own lot)
        converted to a price distance via the REAL dollar-per-pip at
        the position's actual lot (mt5.order_calc_profit-based, same
        helper the TP formula uses), so the dollar amount actually
        locked in matches the real cumulative loss regardless of lot
        size at this round.

        R is read from self.buy_r_frozen/sell_r_frozen — frozen ONCE
        at position-confirmation time (see _check_legs), not
        recomputed from the live, continuously-resynced pos.sl field.
        """
        if not self._risk_free_enabled:
            return
        if buy_pos and not self.risk_free_applied.get("buy", False):
            pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
            r = self.buy_r_frozen
            if r > 0:
                profit_dist = pos.price_current - pos.price_open
                if profit_dist >= 2 * r:
                    lock_dist = self._risk_free_lock_distance(r, pos.volume)
                    new_sl = _round_price(pos.price_open + lock_dist, self.symbol)
                    if self._move_position_sl(pos.ticket, new_sl):
                        self.risk_free_applied["buy"] = True
                        self._log(
                            f"🛡️  [{self.name[:20]}] BUY risk-free | "
                            f"profit={profit_dist:.5f} ≥ 2R={2*r:.5f} | "
                            f"SL moved to {new_sl:.5f} "
                            f"(covers cumulative_loss=${self.cumulative_loss:.2f} "
                            f"+ this round's risk)", "NEW"
                        )

        if sell_pos and not self.risk_free_applied.get("sell", False):
            pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
            r = self.sell_r_frozen
            if r > 0:
                profit_dist = pos.price_open - pos.price_current
                if profit_dist >= 2 * r:
                    lock_dist = self._risk_free_lock_distance(r, pos.volume)
                    new_sl = _round_price(pos.price_open - lock_dist, self.symbol)
                    if self._move_position_sl(pos.ticket, new_sl):
                        self.risk_free_applied["sell"] = True
                        self._log(
                            f"🛡️  [{self.name[:20]}] SELL risk-free | "
                            f"profit={profit_dist:.5f} ≥ 2R={2*r:.5f} | "
                            f"SL moved to {new_sl:.5f} "
                            f"(covers cumulative_loss=${self.cumulative_loss:.2f} "
                            f"+ this round's risk)", "NEW"
                        )

    def _risk_free_lock_distance(self, r_price: float, lot: float) -> float:
        """
        Price distance (always positive) the risk-free SL should sit
        beyond entry, sized so the locked-in dollar profit covers
        cumulative_loss (all real losses taken so far this cycle)
        PLUS this round's own risk in dollars — not just a flat +1R.

        total_at_risk_$ = cumulative_loss + (r_price_in_pips × $/pip)
        lock_distance    = total_at_risk_$ / $/pip   (back to price units)

        Falls back to the plain +1R distance if dollar-per-pip can't
        be determined, so a lock is always produced.
        """
        dpp = self._dollar_per_pip(lot)
        if dpp <= 0:
            return r_price  # fallback: plain +1R in price terms

        r_pips = r_price / self.pip_size
        this_round_risk_dollars = r_pips * dpp
        total_at_risk_dollars   = self.cumulative_loss + this_round_risk_dollars

        lock_pips = total_at_risk_dollars / dpp
        return lock_pips * self.pip_size

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

    def _get_close_info(self, position_ticket: int):
        """
        Fetch the exact execution price AND the reason (SL/TP/manual/
        other) of the deal that closed this position. Returns
        (price, reason) where reason is one of "tp", "sl", "manual",
        "other", or None if the deal can't be found.

        Distinguishing TP from SL matters a lot here: a TP hit means
        that round was WON outright — the position should reset to
        IDLE, not chain into another martingale recovery order at a
        bigger lot. Only an SL hit (a loss) should trigger the normal
        double-up recovery cycle. Treating every close the same way
        (as this used to) meant a winning TP close would immediately
        re-arm a new pending stop anyway, which could go on to lose
        and eat into profit that was already locked in.
        """
        try:
            deals = mt5.history_deals_get(position=position_ticket)
            if not deals:
                return None, None
            closing = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            if not closing:
                return None, None
            closing.sort(key=lambda d: d.time, reverse=True)
            deal = closing[0]
            price = float(deal.price)
            d_reason = getattr(deal, "reason", None)
            if d_reason == mt5.DEAL_REASON_TP:
                reason = "tp"
            elif d_reason == mt5.DEAL_REASON_SL:
                reason = "sl"
            elif d_reason in (mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_MOBILE,
                              mt5.DEAL_REASON_WEB, mt5.DEAL_REASON_EXPERT):
                reason = "manual"
            else:
                reason = "other"
            return price, reason
        except Exception as e:
            log.warning("Could not fetch close info for #%s: %s",
                       position_ticket, e)
            return None, None

    def _get_close_price(self, position_ticket: int):
        """Back-compat wrapper — price only. See _get_close_info."""
        price, _ = self._get_close_info(position_ticket)
        return price

    def _get_real_loss(self, position_ticket: int) -> float:
        """
        Return the absolute dollar loss of a closed position from MT5
        deal history. Returns 0.0 if the close was profitable or if
        the deal can't be found — so it's safe to always add the
        return value to cumulative_loss.
        """
        try:
            deals = mt5.history_deals_get(position=position_ticket)
            if not deals:
                return 0.0
            closing = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            if not closing:
                return 0.0
            closing.sort(key=lambda d: d.time, reverse=True)
            profit = float(closing[0].profit)
            return abs(profit) if profit < 0 else 0.0
        except Exception:
            return 0.0

    # ── New stop placement ────────────────────────────────────────

    def _max_affordable_lot(self, lot_step: float = 0.01) -> float:
        """
        Largest lot size (rounded down to lot_step) that passes
        _can_afford's margin check right now. Returns 0.0 if even the
        minimum lot isn't affordable.
        """
        try:
            acct = mt5.account_info()
            tick = mt5.symbol_info_tick(self.symbol)
            if not acct or not tick:
                return 0.0
            # Margin scales ~linearly with lot for a fixed price/symbol,
            # so compute margin-per-lot from a 1.0-lot probe and divide.
            probe_margin = mt5.order_calc_margin(
                mt5.ORDER_TYPE_BUY, self.symbol, 1.0, tick.ask
            )
            if not probe_margin or probe_margin <= 0:
                return 0.0
            free_margin   = acct.margin_free
            equity        = acct.equity
            safety_margin = equity * 0.05
            usable_margin = free_margin - safety_margin
            if usable_margin <= 0:
                return 0.0
            max_lot = usable_margin / probe_margin
            # Round down to the nearest lot_step, floor at 0.
            steps = int(max_lot / lot_step)
            return max(steps * lot_step, 0.0)
        except Exception as e:
            log.warning("Max affordable lot calc error: %s", e)
            return 0.0

    def _has_bounce_confluence(self, is_buy: bool, current_price: float) -> bool:
        """
        Structural gate for deep martingale rounds: before continuing
        to double into a losing position, check whether real market
        structure (OB+FVG confluence) actually supports a bounce in
        the needed direction near the current price.

        This is a reasoned heuristic, NOT a calibrated probability —
        there's no backtested statistic backing a specific win rate
        here. It simply asks: does a genuine, currently-unmitigated
        OB+FVG confluence zone exist within a reasonable distance of
        price, in the direction (BULL for a BUY recovery, BEAR for a
        SELL recovery) that would actually support the bounce this
        round needs? If yes, the cycle continues; if no, the round
        is treated as unsupported and the cycle resets instead of
        blindly doubling again.

        Only called once lot/margin thresholds are met — see the
        call sites in _place_new_buy_stop/_place_new_sell_stop.
        """
        try:
            from core.ob_detector import detect_order_blocks
            from core.ob_fvg_confluence import find_confluences
            from core.mtf_fvg import _scan_fvgs, TIMEFRAME_SPECS

            obs = detect_order_blocks(
                self.symbol, lookback=200, min_impulse_pips=3.0, swing_lookback=5
            )
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return True  # can't check — fail open, don't block on a data gap
            bid, ask = tick.bid, tick.ask

            spec = TIMEFRAME_SPECS["1M"]
            fvgs = _scan_fvgs(
                self.symbol, "1M", spec["default_lookback"],
                min_gap_pips=1.5, pip_size=self.pip_size, bid=bid, ask=ask
            )

            zones = find_confluences(obs, fvgs, self.pip_size)
            if not zones:
                return False

            wanted_kind = "BULL" if is_buy else "BEAR"
            # Reasonable proximity: within 3x the configured order
            # distance — close enough to plausibly matter for this
            # round's bounce, not just any zone anywhere on the chart.
            max_dist = self.dist_pips * 3 * self.pip_size

            for z in zones:
                if z.kind != wanted_kind or z.mitigated:
                    continue
                zone_mid = (z.combined_top + z.combined_bottom) / 2
                if abs(zone_mid - current_price) <= max_dist:
                    self._log(
                        f"✅  [{self.name[:20]}] bounce confluence found: "
                        f"{z.summary()}", "INFO"
                    )
                    return True

            return False
        except Exception as e:
            log.warning("Bounce confluence check error: %s", e)
            return True  # fail open on any error — don't block on a bug here

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
            # 5% cushion — enough to avoid landing right at a literal
            # margin call after this fill, without blocking trades the
            # account can genuinely afford. The previous 20% buffer
            # was blocking real, affordable rounds (e.g. needing
            # $2122 against $2445 free — actually affordable — got
            # blocked because the 20%-of-equity cushion demanded $706
            # left over, not because the trade itself was unaffordable).
            safety_margin = equity * 0.05
            if free_margin - margin < safety_margin:
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} MARGIN PROTECTION | "
                    f"lot={lot:.2f} needs ${margin:.2f} margin | "
                    f"free=${free_margin:.2f} equity=${equity:.2f} | "
                    f"cannot place safely — resetting to IDLE", "WARN"
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
            # Full doubled lot isn't affordable — try the largest lot
            # that IS affordable instead of abandoning the cycle.
            # The TP/SL formulas read self.buy_lot live, so reducing
            # it here still produces a correctly-sized TP that covers
            # cumulative_loss + this reduced round's real risk — it's
            # mathematically sound, just a smaller step than a full
            # doubling would have been.
            reduced_lot = self._max_affordable_lot()
            if reduced_lot >= 0.01:
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} reduced lot "
                    f"{self.buy_lot:.2f} → {reduced_lot:.2f} (margin-limited) — "
                    f"keeping the cycle alive toward the original target", "WARN"
                )
                self.buy_lot = reduced_lot
            else:
                # Even the minimum lot isn't affordable — nothing left
                # to try. Reset cleanly rather than leaving the source
                # dangling with no position and no pending order.
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} MARGIN PROTECTION | "
                    f"not even minimum lot is affordable — resetting to IDLE", "WARN"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
                return

        # Re-anchor from the EXACT price the previous BUY position's
        # SL closed at (rather than the original fixed line/zone
        # price), eliminating any slippage-induced gap. Delegated to
        # _reanchor_buy() since subclasses (e.g. FVGEntryState) derive
        # entry/SL/TP from different fields than self.price and need
        # their own re-anchoring logic.
        if anchor_price is not None:
            self._reanchor_buy(anchor_price)

        # ── Structural gate for deep rounds ─────────────────────────
        # Once lot has grown large (≥0.64) OR free margin is getting
        # tight, don't just blindly double again — require real OB+FVG
        # confluence supporting a bounce. If neither condition is met,
        # this is a no-op (cheap checks only, no detector calls).
        deep_round = self.buy_lot >= 0.64
        tight_margin = False
        if not deep_round:
            try:
                acct = mt5.account_info()
                if acct and acct.equity > 0:
                    tight_margin = (acct.margin_free / acct.equity) < 0.30
            except Exception:
                pass

        if deep_round or tight_margin:
            mid_now = (self._last_bid + self._last_ask) / 2 if self._last_ask else self.price
            if not self._has_bounce_confluence(is_buy=True, current_price=mid_now):
                self._log(
                    f"🚫  [{self.name[:20]}] R{self.round} no bounce confluence "
                    f"found (lot={self.buy_lot:.2f}) — cutting losses, "
                    f"resetting instead of doubling again", "WARN"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
                return

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
            reduced_lot = self._max_affordable_lot()
            if reduced_lot >= 0.01:
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} reduced lot "
                    f"{self.sell_lot:.2f} → {reduced_lot:.2f} (margin-limited) — "
                    f"keeping the cycle alive toward the original target", "WARN"
                )
                self.sell_lot = reduced_lot
            else:
                self._log(
                    f"🛡️  [{self.name[:20]}] R{self.round} MARGIN PROTECTION | "
                    f"not even minimum lot is affordable — resetting to IDLE", "WARN"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
                return

        # Re-anchor from the EXACT price the previous SELL position's
        # SL closed at — see matching comment in _place_new_buy_stop.
        if anchor_price is not None:
            self._reanchor_sell(anchor_price)

        deep_round = self.sell_lot >= 0.64
        tight_margin = False
        if not deep_round:
            try:
                acct = mt5.account_info()
                if acct and acct.equity > 0:
                    tight_margin = (acct.margin_free / acct.equity) < 0.30
            except Exception:
                pass

        if deep_round or tight_margin:
            mid_now = (self._last_bid + self._last_ask) / 2 if self._last_ask else self.price
            if not self._has_bounce_confluence(is_buy=False, current_price=mid_now):
                self._log(
                    f"🚫  [{self.name[:20]}] R{self.round} no bounce confluence "
                    f"found (lot={self.sell_lot:.2f}) — cutting losses, "
                    f"resetting instead of doubling again", "WARN"
                )
                self.needs_full_reset = True
                self.reset()
                self._relocate_to_fresh_fvg()
                return

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
        self.buy_r_frozen    = 0.0
        self.sell_r_frozen   = 0.0
        self.round           = 0
        self.state           = self.IDLE
        self._buy_confirmed  = False
        self._sell_confirmed = False
        self.risk_free_applied = {"buy": False, "sell": False}
        self.cumulative_loss   = 0.0
        self._pip_value_per_base_lot = 0.0
        self._log(f"🔄  [{self.name[:20]}] state reset to IDLE")
        try:
            from core.resume import clear_session
            clear_session(self.symbol)
        except Exception:
            pass

    def _relocate_to_fresh_fvg(self):
        """
        After a fully-automatic reset (TP win or risk-free close), re-
        anchor this source's main line to a freshly detected FVG and
        immediately resume the touch-detection cycle, instead of
        sitting idle waiting for a manual line move.

        IMPORTANT: this does NOT touch the actual MT5 chart object
        that originally created this source (e.g. a horizontal line
        you drew). It only knows how to draw rectangles via the
        command-file bridge (DRAW_RECT, used elsewhere for FVG
        zones) — there's no confirmed command for creating/moving a
        horizontal line, so guessing one risks silently doing nothing
        on your EA. Trying to "move" the real chart object would also
        be wrong anyway: that line is yours, and should keep meaning
        whatever you intend it to mean even after this source's
        martingale cycle finishes.

        Instead, this source RENAMES itself to a synthetic,
        watcher-internal name (no longer tied to any real chart
        object name) and sets self._is_auto_relocated = True. The
        watcher's "did a chart object move?" loop only ever iterates
        names it just read FROM the chart file — a synthetic name
        will never appear there, so it can never be mistaken for a
        manual line move and reset again. watcher.py is responsible
        for re-keying self._sources to this new name after calling
        this (see _consume_relocation()).

        Picks the most recent, currently-untouched FVG on the
        smallest configured timeframe (1M) nearest to current price.
        """
        try:
            from core.mtf_fvg import _scan_fvgs, TIMEFRAME_SPECS
        except Exception as e:
            log.warning("Could not import mtf_fvg for auto-relocate: %s", e)
            return

        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                return
            bid, ask = tick.bid, tick.ask
            mid = (bid + ask) / 2.0

            tf_key = "1M"
            spec   = TIMEFRAME_SPECS[tf_key]
            fvgs = _scan_fvgs(
                self.symbol, tf_key, spec["default_lookback"],
                min_gap_pips=1.0, pip_size=self.pip_size, bid=bid, ask=ask
            )
            if not fvgs:
                self._log(
                    f"ℹ️  [{self.name[:20]}] auto-relocate: no fresh FVG "
                    f"found yet — staying IDLE until one appears", "INFO"
                )
                return

            # Nearest-to-price among the most recently formed gaps.
            fvgs.sort(key=lambda f: abs(((f.top + f.bottom) / 2.0) - mid))
            best = fvgs[0]
            new_price = round((best.top + best.bottom) / 2.0, 5)

            old_name  = self.name
            old_price = self.price

            # Synthetic name, never matched against real chart object
            # names — strip any leading "AUTO_" chain so repeated
            # auto-relocations don't grow an ever-longer prefix.
            base_label = old_name
            if base_label.startswith("AUTO_"):
                base_label = base_label.split("_", 2)[-1]
            self.pending_rename = f"AUTO_{int(mid*100)}_{base_label}"
            self.name   = self.pending_rename
            self.price  = new_price
            self._prev_tick_price = mid
            self.last_prev_t   = 0
            self.registered_at = 0
            self._is_auto_relocated = True
            self._relocated_at = _time.time()
            self._relocated_from_price = new_price

            self._log(
                f"🆕  [{old_name[:20]}] auto-relocated {old_price:.5f} → "
                f"{new_price:.5f} as [{self.name[:25]}] (fresh {best.kind} "
                f"1M FVG {best.bottom:.5f}-{best.top:.5f}, {best.gap_pips}p) "
                f"| waiting for candle touch", "NEW"
            )
        except Exception as e:
            log.warning("Auto-relocate to fresh FVG failed: %s", e)


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