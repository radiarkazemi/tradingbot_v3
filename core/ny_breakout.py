"""
ny_breakout.py — TraderBot v2
4H New York session breakout entry.

Spec (user's own words): if there was no entry until the New York
session's first 4H candle has closed, then on the SECOND 4H candle,
watch for live price crossing above the first candle's high (→ BUY)
or below the first candle's low (→ SELL), and enter at the exact
crossing moment.

This is intentionally NOT a SourceState subclass — unlike every other
entry mechanism in this bot, it doesn't produce a symmetric
BUY-STOP+SELL-STOP pair around a fixed price. It only knows which
direction to trade once price actually crosses the breakout level, so
it's a single directional trigger with its own small state machine.

NY session anchor: 8:00 AM New York local time (the conventional ICT
"New York session open"), computed via the standard-library zoneinfo
module so DST is handled automatically. This is mapped to the
broker's server time using the live offset between the MT5 terminal
clock (mt5.symbol_info_tick().time) and UTC, detected at runtime —
no broker-specific UTC offset is hardcoded, since brokers differ.

  first 4H candle  = [8:00 AM NY, 12:00 PM NY)   → track its high/low
  second 4H candle = [12:00 PM NY, 4:00 PM NY)   → watch for breakout

Gating: this only fires if no entry has happened for this symbol
since the NY session open — checked via an optional reference to the
running MTFFVGWatcher (if the person has one active) by inspecting
its FVGEntryState objects, plus the base/AMD SourceState dict from
the main WatcherThread, if provided.
"""
import MetaTrader5 as mt5
import threading
import time as _time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import MAGIC_NUMBER
from core.order_manager import _filling_mode, _round_price, get_pip_size

log = logging.getLogger("ny_breakout")

NY_TZ = ZoneInfo("America/New_York")
SESSION_OPEN_HOUR  = 8   # 8:00 AM NY — first 4H candle starts here
FIRST_CANDLE_HOURS = 4   # first 4H candle: [8:00, 12:00) NY
SECOND_CANDLE_HOURS = 4  # second 4H candle: [12:00, 16:00) NY


class NYBreakoutSignals:
    def __init__(self):
        self._log_cbs   = []
        self._state_cbs = []

    def on_log(self, fn):   self._log_cbs.append(fn)
    def on_state(self, fn): self._state_cbs.append(fn)

    def emit_log(self, msg, level="INFO"):
        for fn in self._log_cbs: fn(msg, level)

    def emit_state(self, state: dict):
        for fn in self._state_cbs: fn(state)


def _server_time_now(symbol: str) -> datetime:
    """
    Best-effort broker server time as a naive datetime, derived from
    the live tick timestamp (mt5.symbol_info_tick().time is already
    in broker server time, per the MT5 API). Falls back to UTC now
    if no tick is available yet.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick and tick.time:
        return datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_offset_seconds(symbol: str) -> float:
    """
    Detected offset (seconds) between broker server time and true UTC,
    using the gap between the tick's reported time and the local
    machine's UTC clock at the moment of the call. This is necessarily
    a one-tick-interval approximation — fine for placing a session
    anchor that only needs to land on the right side of an hour
    boundary, not for sub-second precision.
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick or not tick.time:
        return 0.0
    return tick.time - _time.time()


def _ny_session_anchor_in_server_time(symbol: str) -> datetime:
    """
    Returns today's (in NY local time) 8:00 AM NY session-open instant,
    expressed as a naive broker-server-time datetime.
    """
    offset_sec = _utc_offset_seconds(symbol)
    now_utc_aware = datetime.now(timezone.utc)
    now_ny         = now_utc_aware.astimezone(NY_TZ)

    session_open_ny = now_ny.replace(hour=SESSION_OPEN_HOUR, minute=0,
                                     second=0, microsecond=0)
    # If we're before today's session open (NY time), the relevant
    # session is still "today's", we just haven't reached it yet —
    # that's fine, the watcher simply waits.
    session_open_utc = session_open_ny.astimezone(timezone.utc)
    session_open_server = session_open_utc.replace(tzinfo=None) + \
                           timedelta(seconds=offset_sec)
    return session_open_server


class NYBreakoutWatcher(threading.Thread):
    """
    States:
      WAITING_FIRST   — before/during the first 4H candle, tracking
                         high/low as it forms (final values locked in
                         once the candle closes).
      WAITING_BREAKOUT — in the second 4H candle, watching for a cross
                         of the first candle's high/low.
      DONE            — either an entry was placed this session, or
                         the second 4H candle closed with no breakout
                         (spec doesn't ask for entries past that
                         window, so this watcher simply goes idle
                         until the next day's session).
    """
    WAITING_FIRST    = "waiting_first"
    WAITING_BREAKOUT = "waiting_breakout"
    DONE             = "done"

    def __init__(self, symbol: str, base_lot: float, dist_pips: float = 0.0,
                 other_entry_check=None, log_fn=None):
        """
        other_entry_check: optional callable -> bool, returning True if
        some OTHER entry mechanism has already produced a PENDING/ACTIVE
        position for this symbol since the session opened. If it
        returns True when WAITING_BREAKOUT, this watcher stands down
        for the rest of the session (per spec: only fires "if there
        was no entry until NY session first 4H").
        """
        super().__init__(daemon=True)
        self.symbol            = symbol
        self.base_lot          = base_lot
        self.dist_pips         = dist_pips
        self._other_entry_check = other_entry_check or (lambda: False)
        self.sig               = NYBreakoutSignals()
        if log_fn:
            self.sig.on_log(log_fn)
        self._stop_event       = threading.Event()

        self.state          = self.WAITING_FIRST
        self._session_date  = None     # NY-local date this session belongs to
        self._first_open_t  = None     # server-time datetime, first candle open
        self._first_close_t = None     # server-time datetime, first candle close
        self._second_close_t = None    # server-time datetime, second candle close
        self._first_high    = None
        self._first_low     = None
        self._entry_placed_today = False

    def stop(self):
        self._stop_event.set()

    def _log(self, msg, level="INFO"):
        self.sig.emit_log(msg, level)
        log.info(msg)

    def _maybe_roll_session(self):
        """
        Detect a new NY session day and reset all per-day tracking.
        Called every scan; cheap no-op once the day's anchors are set.
        """
        anchor_server = _ny_session_anchor_in_server_time(self.symbol)
        today_key = anchor_server.date()

        if self._session_date != today_key:
            self._session_date   = today_key
            self._first_open_t   = anchor_server
            self._first_close_t  = anchor_server + timedelta(hours=FIRST_CANDLE_HOURS)
            self._second_close_t = self._first_close_t + timedelta(hours=SECOND_CANDLE_HOURS)
            self._first_high     = None
            self._first_low      = None
            self._entry_placed_today = False
            self.state = self.WAITING_FIRST
            self._log(
                f"📅  [NY-4H] new session anchor | "
                f"1st candle {self._first_open_t.strftime('%H:%M')}-"
                f"{self._first_close_t.strftime('%H:%M')} | "
                f"2nd candle ends {self._second_close_t.strftime('%H:%M')} "
                f"(server time)", "INFO"
            )

    def _update_first_candle_extremes(self, bid: float, ask: float, now_server: datetime):
        """Track running high/low while inside the first 4H candle window."""
        if not (self._first_open_t <= now_server < self._first_close_t):
            return
        mid = (bid + ask) / 2.0
        if self._first_high is None or mid > self._first_high:
            self._first_high = mid
        if self._first_low is None or mid < self._first_low:
            self._first_low = mid

    def run(self):
        self._log("=" * 60)
        self._log("  NY 4H Breakout Watcher started")
        self._log("=" * 60)

        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as e:
                self._log(f"⚠️  [NY-4H] scan error: {e}", "WARN")
            self._stop_event.wait(2)

    def _scan(self):
        self._maybe_roll_session()

        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return
        bid, ask = tick.bid, tick.ask
        now_server = _server_time_now(self.symbol)

        if now_server < self._first_open_t:
            return  # session hasn't started yet today

        # Lock in first-candle high/low while it's forming or just closed.
        if now_server < self._first_close_t:
            self.state = self.WAITING_FIRST
            self._update_first_candle_extremes(bid, ask, now_server)
            return

        # First candle has closed — decide if we should watch for breakout.
        if self.state == self.WAITING_FIRST:
            if self._first_high is None or self._first_low is None:
                self._log(
                    "⚠️  [NY-4H] first candle closed with no tick data captured — "
                    "skipping this session", "WARN"
                )
                self.state = self.DONE
                return
            self.state = self.WAITING_BREAKOUT
            self._log(
                f"🕐  [NY-4H] first candle closed | "
                f"high={self._first_high:.5f} low={self._first_low:.5f} | "
                f"watching for breakout in 2nd candle", "NEW"
            )

        if self.state != self.WAITING_BREAKOUT:
            return

        # Second candle window expired with no breakout — stand down for today.
        if now_server >= self._second_close_t:
            self._log(
                "ℹ️  [NY-4H] 2nd candle ended with no breakout — "
                "no entry today", "INFO"
            )
            self.state = self.DONE
            return

        # If some other entry mechanism already has an active/pending
        # position this session, the spec says this watcher doesn't fire.
        if self._other_entry_check():
            self._log(
                "ℹ️  [NY-4H] another entry already triggered this session — "
                "standing down", "INFO"
            )
            self.state = self.DONE
            return

        mid = (bid + ask) / 2.0
        if mid > self._first_high:
            self._enter("buy", ask)
        elif mid < self._first_low:
            self._enter("sell", bid)

    def _enter(self, direction: str, fill_price: float):
        if self._entry_placed_today:
            return
        pip = get_pip_size(self.symbol)
        filling = _filling_mode(self.symbol)
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

        # SL/TP anchored to the breakout range itself: the opposite
        # side of the first candle's range as the stop (a natural
        # invalidation level for a range breakout), 1:3 RR for the
        # target, consistent with this bot's other entries.
        range_dist = abs(self._first_high - self._first_low)
        if range_dist <= 0:
            range_dist = self.dist_pips * pip if self.dist_pips else 10 * pip

        if direction == "buy":
            sl = _round_price(self._first_low, self.symbol)
            tp = _round_price(fill_price + range_dist * 3, self.symbol)
        else:
            sl = _round_price(self._first_high, self.symbol)
            tp = _round_price(fill_price - range_dist * 3, self.symbol)

        res = mt5.order_send({
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       self.symbol,
            "volume":       self.base_lot,
            "type":         order_type,
            "price":        fill_price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    30,
            "magic":        MAGIC_NUMBER,
            "comment":      "TB2_NY4H",
            "type_filling": filling,
        })

        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            self._entry_placed_today = True
            self.state = self.DONE
            self._log(
                f"🚀  [NY-4H] {direction.upper()} breakout entry @ {fill_price:.5f} | "
                f"sl={sl:.5f} tp={tp:.5f} lot={self.base_lot:.2f} | "
                f"crossed {'high' if direction == 'buy' else 'low'} "
                f"{self._first_high if direction == 'buy' else self._first_low:.5f}",
                "NEW"
            )
        else:
            self._log(
                f"⚠️  [NY-4H] {direction.upper()} entry failed: "
                f"{getattr(res, 'comment', 'unknown error')}", "WARN"
            )