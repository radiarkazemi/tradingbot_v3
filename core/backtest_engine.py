"""
backtest_engine.py — TraderBot v2

Drives the REAL, unmodified core/position_monitor.py SourceState logic
against a simulated price path and a mock MT5 broker (backtest_broker.py)
instead of a live MT5 terminal. No chart, no EA file-bridge, no FVG/OB/
AMD detectors — just the core touch -> pair -> recovery -> risk-free /
hard-stop machinery, stress-tested against synthetic volatility.

IMPORTANT — what this is and isn't:
  - The price path is a synthetic random walk calibrated to a rough
    daily-volatility figure per symbol. It is NOT real historical
    price data. This tests whether the bot's RECOVERY/RISK MACHINERY
    survives volatile/ranging conditions — it says nothing about
    whether trading any particular real line would be profitable.
  - The deep-round OB+FVG confluence gate is bypassed (fail-open),
    since it depends on real chart history this simulation doesn't
    have.
  - Runs a single line/source on one symbol. The bot's recovery logic
    naturally re-touches the same fixed line many times over the
    simulated window as price randomly walks back and forth across it
    — that's the realistic stress case for lot growth, risk-free, and
    the hard stop-loss kill switch.

Symbol is namespaced with a "_BT" suffix internally so this can never
collide with (or delete) a real start_balance_<SYMBOL>.json session
file from live trading.
"""
import math
import random
import time as _time
from types import SimpleNamespace
from datetime import datetime

import config as cfg
import core.position_monitor as pm
import core.order_manager as om
from core.watcher import WatcherSignals
from core.backtest_broker import MockBroker, _pip_size, SYMBOL_SPECS


# ── Mock mt5 module shim ────────────────────────────────────────────

class _MockMT5:
    """
    Function-level shim matching the subset of the real MetaTrader5
    module's API that order_manager.py / position_monitor.py call.
    Delegates everything to a bound MockBroker instance.
    """
    def __init__(self, broker: MockBroker):
        self._b = broker
        # Re-export constants so `mt5.ORDER_TYPE_BUY` etc. still work
        # after position_monitor.mt5 / order_manager.mt5 are pointed
        # at this object.
        import core.backtest_broker as bb
        for name in dir(bb):
            if name.isupper():
                setattr(self, name, getattr(bb, name))

    def symbol_select(self, symbol, enable=True):
        return True

    def symbol_info(self, symbol):
        spec = self._b.spec
        return SimpleNamespace(
            digits=spec["digits"], point=spec["point"],
            trade_stops_level=spec["trade_stops_level"],
            filling_mode=4,  # bit for ORDER_FILLING_RETURN path in _filling_mode()
            volume_step=spec["volume_step"], volume_min=spec["volume_min"],
            trade_tick_size=spec["trade_tick_size"],
            trade_tick_value=spec["trade_tick_value"],
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=self._b.bid, ask=self._b.ask)

    def account_info(self):
        return self._b.get_account_info()

    def positions_get(self, symbol=None):
        return self._b.get_positions()

    def orders_get(self, symbol=None):
        return self._b.get_orders()

    def order_send(self, request):
        return self._b.order_send(request)

    def order_calc_margin(self, action, symbol, lot, price):
        return self._b._margin_for(lot, price)

    def order_calc_profit(self, action, symbol, lot, price_open, price_close):
        is_buy = (action == 0)  # ORDER_TYPE_BUY == 0
        return self._b._profit_for(is_buy, lot, price_open, price_close)

    def history_deals_get(self, position=None):
        return self._b.get_deals(position)


def install_mock(broker: MockBroker):
    """
    Monkeypatch the `mt5` name inside position_monitor.py and
    order_manager.py to point at a shim backed by `broker`. Returns
    the originals needed to restore afterward via uninstall_mock().
    Also disables disk-persistence side effects (_save / resume), the
    chart-dependent confluence/relocate methods, and replaces
    position_monitor's wall-clock (_time.time/_time.sleep) with a
    controllable simulated clock — ACTIVATION_GRACE_SEC and the
    auto-relocate staleness timers are real-time-based, and a 10-day
    backtest finishes in well under a second of actual wall-clock
    time, so without this the bot would be stuck "in grace" for the
    entire simulated run and never notice a single closed position.
    """
    shim = _MockMT5(broker)
    clock = _FakeClock()
    originals = dict(
        pm_mt5=pm.mt5, om_mt5=om.mt5,
        pm_save=pm._save,
        bounce=pm.SourceState._has_bounce_confluence,
        relocate=pm.SourceState._relocate_to_fresh_fvg,
        pm_time=pm._time,
    )
    pm.mt5 = shim
    om.mt5 = shim
    pm._save = lambda state: None  # never touch real resume-session files
    pm.SourceState._has_bounce_confluence = lambda self, is_buy, current_price: True
    pm.SourceState._relocate_to_fresh_fvg = lambda self: None
    pm._time = clock
    return originals, clock


def uninstall_mock(originals: dict):
    """Restore everything install_mock() touched."""
    pm.mt5 = originals["pm_mt5"]
    om.mt5 = originals["om_mt5"]
    pm._save = originals["pm_save"]
    pm.SourceState._has_bounce_confluence = originals["bounce"]
    pm.SourceState._relocate_to_fresh_fvg = originals["relocate"]
    pm._time = originals["pm_time"]


class _FakeClock:
    """
    Drop-in replacement for the `time` module's surface that
    position_monitor.py actually uses (.time(), .sleep()). Sleep
    advances the simulated clock instead of blocking — there is no
    reason to actually wait in a backtest. The backtest loop also
    advances `.now` every simulated bar (see BacktestThread.run) so
    ACTIVATION_GRACE_SEC and the auto-relocate staleness timers see
    simulated time elapsing at the same rate as the price path.
    """
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ── Synthetic price path ────────────────────────────────────────────

def generate_price_path(spec: dict, days: int, bars_per_day: int = 1440,
                         seed: int = None):
    """
    Pure random walk (no drift) calibrated so the cumulative move over
    one simulated day has roughly `daily_vol_pips` standard deviation
    — scaled down per-bar via sqrt(time), the standard random-walk
    scaling. Spread fluctuates mildly around the symbol's base spread.
    Yields (bid, ask) tuples, bars_per_day * days total.
    """
    rng = random.Random(seed)
    pip_size = _pip_size(spec)
    daily_sigma_price = spec["daily_vol_pips"] * pip_size
    per_bar_sigma = daily_sigma_price / math.sqrt(bars_per_day)

    price = spec["base_price"]
    base_spread = spec["base_spread"]
    total_bars = days * bars_per_day
    for _ in range(total_bars):
        price += rng.gauss(0.0, per_bar_sigma)
        price = max(price, pip_size)  # never go non-positive
        spread = base_spread * rng.uniform(0.8, 1.6)
        bid = price
        ask = price + spread
        yield bid, ask


# ── Backtest thread (drop-in interface mirroring WatcherThread) ────

import threading


class BacktestThread(threading.Thread):

    def __init__(self, symbol: str, lot_size: float, dist_pips: float,
                 risk_free_enabled: bool = False, days: int = 10,
                 start_balance: float = 1000.0, leverage: int = 500,
                 bars_per_day: int = 1440, seed: int = None):
        super().__init__(daemon=True)
        self.real_symbol       = symbol
        self.bt_symbol         = f"{symbol}_BT"   # namespaced — see module docstring
        self.lot_size          = lot_size
        self.dist_pips         = dist_pips
        self.risk_free_enabled = risk_free_enabled
        self.days              = days
        self.start_balance     = start_balance
        self.leverage          = leverage
        self.bars_per_day      = bars_per_day
        self.seed              = seed

        self.sig          = WatcherSignals()
        self._stop_event  = threading.Event()
        self.broker        = None

    def stop(self):
        self._stop_event.set()

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.sig.emit_log(f"{ts}  {msg}", level)
        print(f"{ts}  {msg}")

    def _on_balance_tp(self):
        self._stop_event.set()
        self.sig.emit_stop()

    def run(self):
        print("=" * 70)
        print("  TraderBot v2 — BACKTEST MODE (simulated, no live MT5)")
        print("=" * 70)
        self.log(f"Symbol: {self.real_symbol}  days={self.days}  "
                 f"base_lot={self.lot_size}  dist_pips={self.dist_pips}  "
                 f"risk_free={self.risk_free_enabled}")
        self.log("⚠️  Synthetic random-walk price path — NOT real "
                 "historical data. Tests recovery/risk machinery only.")

        if self.real_symbol not in SYMBOL_SPECS:
            self.log(
                f"❌  No backtest spec for '{self.real_symbol}'. "
                f"Supported: {list(SYMBOL_SPECS.keys())}", "ERROR"
            )
            return

        self.broker = MockBroker(
            symbol=self.bt_symbol, spec_key=self.real_symbol,
            start_balance=self.start_balance, leverage=self.leverage,
            commission_per_lot=getattr(cfg, "COMMISSION_PER_LOT", 0.0),
        )
        spec = self.broker.spec
        pip_size = _pip_size(spec)

        originals, clock = install_mock(self.broker)
        seconds_per_bar = 86400.0 / self.bars_per_day  # 1 trading day = 86400s
        try:
            state = pm.SourceState(
                name="BACKTEST_LINE", price=self.broker.bid, pip_size=pip_size,
                symbol=self.bt_symbol, base_lot=self.lot_size,
                dist_pips=self.dist_pips, start_balance=self.start_balance,
                log_fn=self.log, stop_fn=self._on_balance_tp,
                risk_free_enabled=self.risk_free_enabled,
            )

            total_bars = self.days * self.bars_per_day
            day_marker = self.bars_per_day
            t0 = _time.time()

            for i, (bid, ask) in enumerate(generate_price_path(
                    spec, self.days, self.bars_per_day, self.seed)):
                if self._stop_event.is_set():
                    self.log("🛑  Backtest stopped early by user.", "WARN")
                    break

                clock.now = i * seconds_per_bar  # advance simulated wall-clock
                sim_time = i  # abstract simulated tick index
                self.broker.set_price(bid, ask)
                self.broker.process_tick(sim_time)

                if state.state == state.IDLE:
                    state.check_touch(bid, ask)
                elif state.state in (state.PENDING, state.ACTIVE):
                    state.check({"BID": bid})

                if state.state == state.EXHAUSTED:
                    self.log("🏁  Bot reached EXHAUSTED state "
                             "(balance TP or hard stop-loss fired) — "
                             "stopping simulation early.", "NEW")
                    break

                if i + 1 >= day_marker:
                    day_num = day_marker // self.bars_per_day
                    eq = self.broker.equity()
                    self.log(f"📅  Day {day_num}/{self.days} complete | "
                             f"balance=${self.broker.balance:.2f} "
                             f"equity=${eq:.2f} | "
                             f"trades closed={len(self.broker.closed_log)}")
                    day_marker += self.bars_per_day

            elapsed = _time.time() - t0
            self._print_report(elapsed)

        finally:
            uninstall_mock(originals)

    # ── Results ─────────────────────────────────────────────────────

    def _save_chart(self):
        """
        Saves a PNG with the equity curve (marked against the hard
        stop-loss floor and Balance TP target) plus a per-trade lot
        size bar chart, color-coded win/loss. Returns the file path,
        or None if matplotlib isn't installed (logs a warning instead
        of crashing the backtest over an optional visualization).
        """
        try:
            import matplotlib
            matplotlib.use("Agg")  # no display needed
            import matplotlib.pyplot as plt
        except ImportError:
            self.log("⚠️  matplotlib not installed — skipping chart "
                     "(pip install matplotlib to enable it)", "WARN")
            return None

        b = self.broker
        if not b.equity_curve:
            return None

        try:
            times    = [t for t, _ in b.equity_curve]
            equities = [e for _, e in b.equity_curve]

            hard_floor = self.start_balance * getattr(cfg, "HARD_STOP_LOSS_RATIO", 0.80)
            tp_target  = self.start_balance * getattr(cfg, "BALANCE_TP_RATIO", 1.10)

            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(12, 8), gridspec_kw={"height_ratios": [3, 1]})

            ax1.plot(times, equities, color="#2979FF", linewidth=1.2, label="Equity")
            ax1.axhline(self.start_balance, color="gray", linestyle="--",
                        linewidth=0.8, label="Start balance")
            ax1.axhline(hard_floor, color="#FF4560", linestyle="--",
                        linewidth=0.8, label="Hard stop-loss floor")
            ax1.axhline(tp_target, color="#00D97E", linestyle="--",
                        linewidth=0.8, label="Balance TP target")

            for t in b.closed_log:
                idx = t["time"]
                if 0 <= idx < len(b.equity_curve):
                    eq_at_close = b.equity_curve[idx][1]
                    is_win = (t["profit"] - t["commission"]) > 0
                    ax1.scatter(idx, eq_at_close,
                                color="#00D97E" if is_win else "#FF4560",
                                s=25, zorder=3,
                                edgecolors="black", linewidths=0.4)

            ax1.set_ylabel("Equity ($)")
            ax1.set_title(f"{self.real_symbol} backtest — {self.days} simulated days "
                          f"(seed={self.seed})")
            ax1.legend(loc="best", fontsize=8)
            ax1.grid(alpha=0.3)

            lots   = [t["volume"] for t in b.closed_log]
            colors = ["#00D97E" if (t["profit"] - t["commission"]) > 0 else "#FF4560"
                      for t in b.closed_log]
            ax2.bar(range(1, len(lots) + 1), lots, color=colors)
            ax2.set_xlabel("Trade # (closed, in order)")
            ax2.set_ylabel("Lot size")
            ax2.grid(alpha=0.3)

            plt.tight_layout()

            import os as _os
            _os.makedirs("backtest_results", exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = _os.path.join("backtest_results", f"{self.real_symbol}_{ts}.png")
            plt.savefig(path, dpi=120)
            plt.close(fig)
            return path
        except Exception as e:
            self.log(f"⚠️  Chart generation failed: {e}", "WARN")
            return None

    def _print_report(self, elapsed_sec: float):
        b = self.broker
        closed = b.closed_log
        wins   = [t for t in closed if t["profit"] - t["commission"] > 0]
        losses = [t for t in closed if t["profit"] - t["commission"] <= 0]
        sl_hits = [t for t in closed if t["reason"] == "sl"]
        tp_hits = [t for t in closed if t["reason"] == "tp"]

        net_profit  = b.balance - b.start_balance
        net_pct     = (net_profit / b.start_balance * 100.0) if b.start_balance else 0.0
        max_lot     = max([t["volume"] for t in closed], default=0.0)
        total_comm  = sum(t["commission"] for t in closed)

        lines = [
            "",
            "=" * 70,
            "  BACKTEST RESULTS",
            "=" * 70,
            f"  Symbol (real):        {self.real_symbol}",
            f"  Simulated days:       {self.days}  ({self.bars_per_day} bars/day)",
            f"  Wall-clock runtime:   {elapsed_sec:.1f}s",
            "-" * 70,
            f"  Start balance:        ${b.start_balance:,.2f}",
            f"  End balance:          ${b.balance:,.2f}",
            f"  Net profit:           ${net_profit:+,.2f}  ({net_pct:+.2f}%)",
            f"  Max drawdown:         -{b.max_drawdown_pct:.2f}% (vs equity peak)",
            f"  Total commission:     ${total_comm:,.2f}",
            "-" * 70,
            f"  Positions closed:     {len(closed)}",
            f"    Wins:               {len(wins)}",
            f"    Losses:             {len(losses)}",
            f"    Closed by TP:       {len(tp_hits)}",
            f"    Closed by SL:       {len(sl_hits)}",
            f"  Max lot size reached: {max_lot:.2f}",
            f"  Final state:          {self._final_state_label()}",
            "=" * 70,
            "  Reminder: synthetic random-walk price path, not real",
            "  market history. This validates the recovery/risk",
            "  machinery (lot growth, risk-free, margin gates, hard",
            "  stop-loss) under generic volatility — not whether this",
            "  line/strategy would be profitable on real price action.",
            "=" * 70,
            "",
        ]
        report = "\n".join(lines)
        print(report)
        for ln in lines:
            if ln.strip():
                self.sig.emit_log(ln, "NEW")

        chart_path = self._save_chart()
        if chart_path:
            msg = f"📊  Chart saved: {chart_path}"
            print(msg)
            self.sig.emit_log(msg, "NEW")

    def _final_state_label(self) -> str:
        hard_floor = self.start_balance * getattr(cfg, "HARD_STOP_LOSS_RATIO", 0.80)
        tp_target  = self.start_balance * getattr(cfg, "BALANCE_TP_RATIO", 1.10)
        if self.broker.balance <= hard_floor * 1.001:
            return "HARD STOP-LOSS triggered"
        if self.broker.balance >= tp_target * 0.999:
            return "BALANCE TP reached"
        return "Ran full simulated period without TP/hard-stop"