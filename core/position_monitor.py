"""
position_monitor.py — TraderBot v2
"""
import MetaTrader5 as mt5
import logging
import time as _time
from config import MAGIC_NUMBER, MAX_ROUNDS
from core.order_manager import build_pair, send_pair, cancel_order, lot_for_round

log = logging.getLogger("monitor_v2")

HISTORY_SETTLE_SEC      = 3
SL_PRICE_TOLERANCE_PIPS = 1

# After activation, wait this many seconds before checking if position closed.
# Prevents false triggers when market order takes time to appear in positions_get.
ACTIVATION_GRACE_SEC    = 5


class SourceState:
    IDLE      = "idle"
    PENDING   = "pending"
    ACTIVE    = "active"
    EXHAUSTED = "exhausted"

    def __init__(self, name, price, pip_size, symbol, base_lot, dist_pips, log_fn=None):
        self.name          = name
        self.price         = price
        self.pip_size      = pip_size
        self.symbol        = symbol
        self.base_lot      = base_lot
        self.dist_pips     = dist_pips
        self._log          = log_fn or (lambda msg, level="INFO": log.info(msg))

        self.state          = self.IDLE
        self.round          = 0
        self.direction      = None
        self.buy_ticket     = None
        self.sell_ticket    = None
        self.active_ticket  = None
        self.active_entry   = None
        self.active_sl      = None
        self.active_tp      = None
        self.active_is_buy  = None
        self.registered_at  = 0
        self.last_prev_t    = 0
        self._closed_at     = 0.0
        self._close_handled = False
        self._activated_at  = 0.0   # timestamp when position activated
        self._last_bid      = 0.0
        self._confirmed_open = False  # True once we've seen the position in positions_get

    # ── Public API ────────────────────────────────────────────────

    def place_initial_pair(self):
        self.round = 1
        lot    = lot_for_round(1, self.base_lot)
        orders = build_pair(self.price, self.pip_size, self.dist_pips,
                            self.symbol, round_num=1, lot=lot)
        results = send_pair(orders, self.symbol)

        self.buy_ticket  = None
        self.sell_ticket = None
        for r in results:
            if r["ok"]:
                if r["order"]["type"] == "BUY_STOP":
                    self.buy_ticket = r["ticket"]
                else:
                    self.sell_ticket = r["ticket"]

        if self.buy_ticket or self.sell_ticket:
            self.state = self.PENDING
            self._log(
                f"📌  [{self.name[:20]}] R1 pair placed | "
                f"BUY#{self.buy_ticket} SELL#{self.sell_ticket} | "
                f"dist={self.dist_pips}pips lot={lot:.2f}", "NEW"
            )
        else:
            self._log(f"❌  [{self.name[:20]}] failed to place initial pair", "ERROR")

    def check(self, candle: dict):
        bid = candle.get("BID", 0.0)
        if bid:
            self._last_bid = bid

        if self.state in (self.IDLE, self.EXHAUSTED):
            return
        if self.state == self.PENDING:
            self._check_activation()
        elif self.state == self.ACTIVE:
            self._check_position_closed()

    # ── Activation ────────────────────────────────────────────────

    def _check_activation(self):
        pending_tickets = {
            o.ticket for o in (mt5.orders_get(symbol=self.symbol) or [])
            if o.magic == MAGIC_NUMBER
        }

        buy_still   = self.buy_ticket  in pending_tickets if self.buy_ticket  else False
        sell_still  = self.sell_ticket in pending_tickets if self.sell_ticket else False
        buy_filled  = self.buy_ticket  is not None and not buy_still
        sell_filled = self.sell_ticket is not None and not sell_still

        if not buy_filled and not sell_filled:
            return

        if buy_filled and sell_filled:
            self._log(f"⚠️  [{self.name[:20]}] both orders gone — resetting", "WARN")
            self.state = self.IDLE
            self.round = 0
            return

        if buy_filled:
            if sell_still and self.sell_ticket:
                cancel_order(self.sell_ticket)
                self.sell_ticket = None
            self._on_activated("BUY")
        else:
            if buy_still and self.buy_ticket:
                cancel_order(self.buy_ticket)
                self.buy_ticket = None
            self._on_activated("SELL")

    def _on_activated(self, direction: str):
        self.direction       = direction
        self.active_is_buy   = (direction == "BUY")
        self._activated_at   = _time.time()
        self._confirmed_open = False
        self._closed_at      = 0.0
        self._close_handled  = False

        # Try to find the position immediately
        self._try_find_position()

        icon = "🟢" if self.active_is_buy else "🔴"
        self._log(
            f"{icon}  [{self.name[:20]}] R{self.round} {direction} ACTIVATED | "
            f"pos#{self.active_ticket} | entry={self.active_entry} "
            f"sl={self.active_sl} tp={self.active_tp}", "NEW"
        )
        self.state = self.ACTIVE

    def _try_find_position(self):
        """Try to locate the open position in MT5. May need retries for market orders."""
        positions = mt5.positions_get(symbol=self.symbol) or []
        bot_pos   = [p for p in positions if p.magic == MAGIC_NUMBER]
        matched   = [p for p in bot_pos if (p.type == 0) == self.active_is_buy]

        if matched:
            pos = sorted(matched, key=lambda p: p.time, reverse=True)[0]
            self.active_ticket   = pos.ticket
            self.active_entry    = pos.price_open
            self.active_sl       = pos.sl
            self.active_tp       = pos.tp
            self._confirmed_open = True
        # If not found yet, leave ticket=None — will retry in _check_position_closed

    # ── Position close detection ──────────────────────────────────

    def _check_position_closed(self):
        if self._close_handled:
            return

        now = _time.time()

        # Always within grace period — just try to find the position
        in_grace = (now - self._activated_at) < ACTIVATION_GRACE_SEC

        positions    = mt5.positions_get(symbol=self.symbol) or []
        bot_pos      = [p for p in positions if p.magic == MAGIC_NUMBER]
        open_tickets = {p.ticket for p in bot_pos}

        # If we don't have a ticket yet, try to find the position
        if self.active_ticket is None or not self._confirmed_open:
            matched = [p for p in bot_pos if (p.type == 0) == self.active_is_buy]
            if matched:
                pos = sorted(matched, key=lambda p: p.time, reverse=True)[0]
                self.active_ticket   = pos.ticket
                self.active_entry    = pos.price_open
                self.active_sl       = pos.sl
                self.active_tp       = pos.tp
                self._confirmed_open = True
                self._log(
                    f"🔍  [{self.name[:20]}] R{self.round} position found: "
                    f"pos#{self.active_ticket} sl={self.active_sl}", "INFO"
                )
            else:
                if in_grace:
                    # Still in grace period — position may not have appeared yet
                    return
                else:
                    # Grace period over and still no position found
                    # Position likely filled and closed almost instantly (slippage)
                    self._log(
                        f"⚠️  [{self.name[:20]}] R{self.round} position never found "
                        f"after {ACTIVATION_GRACE_SEC}s — treating as SL", "WARN"
                    )
                    self._close_handled = True
                    self._do_reentry()
                    return

        # We have a confirmed ticket — check if it's still open
        if self.active_ticket in open_tickets:
            # Position still open — refresh SL/TP
            pos_list = [p for p in positions if p.ticket == self.active_ticket]
            if pos_list:
                self.active_sl = pos_list[0].sl
                self.active_tp = pos_list[0].tp
            self._closed_at     = 0.0
            self._close_handled = False
            return

        # Position gone — but only act after grace period
        if in_grace:
            return

        # Start settle timer
        if self._closed_at == 0.0:
            self._closed_at = now
            return

        if now - self._closed_at < HISTORY_SETTLE_SEC:
            return

        # Determine SL vs TP
        self._close_handled = True
        closed_by_sl = self._detect_sl_hit()

        if closed_by_sl:
            self._do_reentry()
        else:
            self._log(
                f"🏁  [{self.name[:20]}] R{self.round} {self.direction} "
                f"closed by TP — sequence done", "NEW"
            )
            self.state = self.EXHAUSTED

    def _detect_sl_hit(self) -> bool:
        """
        Returns True = SL hit, False = TP hit.
        Priority: deal history → price comparison → default SL.
        """
        sl     = self.active_sl
        tp     = self.active_tp
        is_buy = self.active_is_buy
        tol    = self.pip_size * SL_PRICE_TOLERANCE_PIPS

        # ── Deal history ──────────────────────────────────────────
        try:
            from_time = int(_time.time()) - 7200
            deals     = mt5.history_deals_get(from_time, int(_time.time()) + 10)

            if deals and len(deals) > 0:
                # Match by position_id
                closing = [d for d in deals
                           if d.position_id == self.active_ticket
                           and d.entry == mt5.DEAL_ENTRY_OUT]
                if closing:
                    d = closing[-1]
                    if d.reason == mt5.DEAL_REASON_SL:
                        self._log(f"📉  [{self.name[:20]}] R{self.round} SL (deal history)", "WARN")
                        return True
                    elif d.reason == mt5.DEAL_REASON_TP:
                        self._log(f"🎯  [{self.name[:20]}] R{self.round} TP (deal history)", "NEW")
                        return False
                    else:
                        self._log(f"🏁  [{self.name[:20]}] R{self.round} manual close", "INFO")
                        return False

                # Match by price proximity to SL
                if sl and sl > 0:
                    sl_deals = [d for d in deals
                                if d.entry == mt5.DEAL_ENTRY_OUT
                                and d.magic == MAGIC_NUMBER
                                and abs(d.price - sl) <= tol]
                    if sl_deals:
                        self._log(f"📉  [{self.name[:20]}] R{self.round} SL (price≈sl)", "WARN")
                        return True
        except Exception as e:
            log.warning("Deal history error: %s", e)

        # ── Price comparison ──────────────────────────────────────
        cur = self._last_bid
        if cur > 0 and sl and sl > 0:
            if is_buy:
                if cur <= sl - tol:
                    self._log(f"📉  [{self.name[:20]}] R{self.round} SL by price: bid={cur:.5f} < sl={sl:.5f}", "WARN")
                    return True
                if tp and tp > 0 and cur >= tp - tol:
                    self._log(f"🎯  [{self.name[:20]}] R{self.round} TP by price: bid={cur:.5f} ≥ tp={tp:.5f}", "NEW")
                    return False
            else:
                if cur >= sl + tol:
                    self._log(f"📉  [{self.name[:20]}] R{self.round} SL by price: bid={cur:.5f} > sl={sl:.5f}", "WARN")
                    return True
                if tp and tp > 0 and cur <= tp + tol:
                    self._log(f"🎯  [{self.name[:20]}] R{self.round} TP by price: bid={cur:.5f} ≤ tp={tp:.5f}", "NEW")
                    return False

        # ── Unknown — but only default to SL if we had a confirmed position ──
        # If sl/tp are None (position was never confirmed), don't blindly re-enter
        if sl is None or sl == 0:
            self._log(
                f"⚠️  [{self.name[:20]}] R{self.round} no SL data — skipping re-entry to protect account",
                "WARN"
            )
            return False   # treat as TP (stop the sequence)

        self._log(
            f"📉  [{self.name[:20]}] R{self.round} reason unknown — assuming SL "
            f"(bid={cur:.5f} sl={sl} tp={tp})", "WARN"
        )
        return True

    # ── Re-entry ──────────────────────────────────────────────────

    def _do_reentry(self):
        """
        After SL hit: place SAME direction (lot×1.20) + OPPOSITE direction (base lot).
        When one activates, the other is cancelled — same as initial pair.
        """
        self.round     += 1
        self._closed_at = 0.0

        if self.round > MAX_ROUNDS:
            self._log(f"🛑  [{self.name[:20]}] {MAX_ROUNDS} rounds exhausted — stopping", "WARN")
            self.state = self.EXHAUSTED
            return

        is_buy         = self.active_is_buy
        martingale_lot = lot_for_round(self.round, self.base_lot)
        base_lot       = lot_for_round(1, self.base_lot)

        all_orders = build_pair(
            self.price, self.pip_size, self.dist_pips,
            self.symbol, round_num=self.round, lot=martingale_lot
        )

        # Counter-side always gets base lot
        for o in all_orders:
            if is_buy  and o["type"] == "SELL_STOP":
                o["lot"] = base_lot
            elif not is_buy and o["type"] == "BUY_STOP":
                o["lot"] = base_lot

        results = send_pair(all_orders, self.symbol)

        self.buy_ticket  = None
        self.sell_ticket = None
        for r in results:
            if r["ok"]:
                if r["order"]["type"] == "BUY_STOP":
                    self.buy_ticket = r["ticket"]
                else:
                    self.sell_ticket = r["ticket"]
            else:
                self._log(
                    f"❌  [{self.name[:20]}] R{self.round} {r['order']['type']} FAILED "
                    f"({r.get('reason','unknown')})", "ERROR"
                )

        if self.buy_ticket or self.sell_ticket:
            self.active_ticket   = None
            self.active_entry    = None
            self.active_sl       = None
            self.active_tp       = None
            self._confirmed_open = False
            self._close_handled  = False
            self.state           = self.PENDING

            buy_lot  = martingale_lot if is_buy  else base_lot
            sell_lot = base_lot       if is_buy  else martingale_lot
            self._log(
                f"🔁  [{self.name[:20]}] SL hit → R{self.round} pair re-placed | "
                f"BUY#{self.buy_ticket} lot={buy_lot:.2f} | "
                f"SELL#{self.sell_ticket} lot={sell_lot:.2f}", "NEW"
            )
        else:
            self._log(
                f"❌  [{self.name[:20]}] R{self.round} both re-entries FAILED — stopping", "ERROR"
            )
            self.state = self.EXHAUSTED

    # ── Reset ─────────────────────────────────────────────────────

    def reset(self):
        for ticket in [self.buy_ticket, self.sell_ticket]:
            if ticket:
                cancel_order(ticket)
        self.buy_ticket      = None
        self.sell_ticket     = None
        self.active_ticket   = None
        self.active_entry    = None
        self.active_sl       = None
        self.active_tp       = None
        self.direction       = None
        self.active_is_buy   = None
        self.round           = 0
        self.state           = self.IDLE
        self._closed_at      = 0.0
        self._close_handled  = False
        self._confirmed_open = False
        self._log(f"🔄  [{self.name[:20]}] state reset to IDLE")

    @property
    def summary(self) -> dict:
        return {
            "name":      self.name,
            "price":     self.price,
            "state":     self.state,
            "round":     self.round,
            "direction": self.direction,
            "lot":       lot_for_round(self.round, self.base_lot) if self.round > 0 else self.base_lot,
        }