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

    # ── Public API ────────────────────────────────────────────────

    def place_initial_pair(self):
        self.round    = 1
        self.buy_lot  = self.base_lot
        self.sell_lot = self.base_lot

        orders = [
            {"type": "BUY_STOP",  "entry": self._buy_entry,  "sl": self._buy_sl_price,
             "tp": 0.0, "lot": self.buy_lot,  "source": self.price, "round": 1},
            {"type": "SELL_STOP", "entry": self._sell_entry, "sl": self._sell_sl_price,
             "tp": 0.0, "lot": self.sell_lot, "source": self.price, "round": 1},
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
                f"BUY#{self.buy_ticket}@{self._buy_entry:.5f} sl={self._buy_sl_price:.5f} lot={self.buy_lot:.2f} | "
                f"SELL#{self.sell_ticket}@{self._sell_entry:.5f} sl={self._sell_sl_price:.5f} lot={self.sell_lot:.2f}", "NEW"
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
        for o in (mt5.orders_get(symbol=self.symbol) or []):
            if o.magic == MAGIC_NUMBER:
                cancel_order(o.ticket)
        # Delete saved start balance — next session starts fresh
        import os as _os
        _bal_file = f"start_balance_{self.symbol}.json"
        try:
            if _os.path.exists(_bal_file):
                _os.remove(_bal_file)
                self._log(f"🗑️  Cleared saved start balance (session complete)", "INFO")
        except Exception:
            pass
        self.state = self.EXHAUSTED
        if self._stop_fn:
            self._stop_fn()
        # Clear saved start balance so next session starts fresh
        import os as _os
        _bal_file = f"start_balance_{self.symbol}.json"
        try:
            if _os.path.exists(_bal_file):
                _os.remove(_bal_file)
                self._log("💾  Session balance file cleared — next session starts fresh", "INFO")
        except Exception:
            pass

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
                self._log(f"🔍  [{self.name[:20]}] BUY pos confirmed #{pos.ticket} sl={pos.sl}", "INFO")

        if not self._sell_confirmed and self.sell_pos_ticket is None:
            if sell_pos:
                pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
                self.sell_pos_ticket = pos.ticket
                self.sell_sl         = pos.sl
                self.sell_lot        = pos.volume
                self._sell_confirmed = True
                self._log(f"🔍  [{self.name[:20]}] SELL pos confirmed #{pos.ticket} sl={pos.sl}", "INFO")

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

        # ── Detect closed positions → place new stop ──────────────
        if (self.buy_pos_ticket
                and self.buy_pos_ticket not in open_tickets
                and self._buy_confirmed):
            self._log(f"📉  [{self.name[:20]}] BUY pos#{self.buy_pos_ticket} closed", "WARN")
            self.buy_pos_ticket = None
            self._buy_confirmed = False
            self._place_new_buy_stop()

        if (self.sell_pos_ticket
                and self.sell_pos_ticket not in open_tickets
                and self._sell_confirmed):
            self._log(f"📉  [{self.name[:20]}] SELL pos#{self.sell_pos_ticket} closed", "WARN")
            self.sell_pos_ticket = None
            self._sell_confirmed = False
            self._place_new_sell_stop()

    # ── New stop placement ────────────────────────────────────────

    def _can_afford(self, lot: float, is_buy: bool) -> bool:
        """
        Check if the account has enough free margin to place this order.
        Uses MT5's own margin calculator. Returns False if margin would
        drop below 20% of equity (safety buffer).
        """
        try:
            action = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            tick   = mt5.symbol_info_tick(self.symbol)
            price  = tick.ask if is_buy else tick.bid
            margin = mt5.order_calc_margin(action, self.symbol, lot, price)
            acct   = mt5.account_info()
            if margin is None or acct is None:
                return True  # can't check — allow and let MT5 reject if needed
            free_margin   = acct.margin_free
            equity        = acct.equity
            safety_margin = equity * 0.20  # keep 20% equity as buffer
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
            return True  # allow on error

    def _place_new_buy_stop(self):
        self.round  += 1
        new_buy_lot  = round(self.sell_lot * 2, 2)
        self.buy_lot = max(new_buy_lot, 0.01)

        if not self._can_afford(self.buy_lot, is_buy=True):
            return  # keep SELL position running, no new BUY

        order = {"type": "BUY_STOP", "entry": self._buy_entry,
                 "sl": self._buy_sl_price, "tp": 0.0,
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

    def _place_new_sell_stop(self):
        self.round   += 1
        new_sell_lot  = round(self.buy_lot * 2, 2)
        self.sell_lot = max(new_sell_lot, 0.01)

        if not self._can_afford(self.sell_lot, is_buy=False):
            return  # keep BUY position running, no new SELL

        order = {"type": "SELL_STOP", "entry": self._sell_entry,
                 "sl": self._sell_sl_price, "tp": 0.0,
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
            self._log(f"ℹ️  [{self.name[:20]}] order #{ticket} already filled", "INFO")
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
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       self.symbol,
                "volume":       new_lot,
                "type":         mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price":        market_price,
                "sl":           use_sl,
                "tp":           0.0,
                "deviation":    30,
                "magic":        MAGIC_NUMBER,
                "comment":      (target.comment or "") + "m",
                "type_filling": filling,
            }
            self._log(f"⚡  [{self.name[:20]}] {'BUY' if is_buy else 'SELL'} past market — "
                      f"MARKET lot={new_lot:.2f} sl={use_sl:.5f}", "WARN")
        else:
            request = {
                "action":       mt5.TRADE_ACTION_PENDING,
                "symbol":       self.symbol,
                "volume":       new_lot,
                "type":         order_type,
                "price":        entry,
                "sl":           use_sl,
                "tp":           0.0,
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
                f"ticket#{res.order} lot={new_lot:.2f} sl={use_sl:.5f} @ {entry:.5f}", "INFO"
            )
            return True
        else:
            self._log(f"❌  Modify failed: {res.retcode if res else '?'}", "ERROR")
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
        # Clear session file on manual reset
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