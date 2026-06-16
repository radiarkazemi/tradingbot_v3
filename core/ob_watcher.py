"""
ob_watcher.py — Order Block background scanner
===============================================
Completely independent daemon thread — mirrors fvg_watcher.py design.
Does NOT depend on the EA file, chart lines, or position_monitor.

Responsibilities:
  1. Scan MT5 candles for OB patterns every scan_interval seconds
  2. Track mitigation (price re-entering a zone) and flag those OBs
  3. Remove mitigated OBs from chart automatically
  4. Log changes (new OBs found, OBs mitigated)
  5. Expose latest_obs list for other modules to read (thread-safe)

Hot-update via update_settings() — no restart needed.
"""

import threading
import time as _time
import logging
import MetaTrader5 as mt5

from core.ob_detector import (
    detect_order_blocks,
    check_mitigation,
    draw_obs_on_chart,
    clear_obs_on_chart,
    OrderBlock,
)

log = logging.getLogger("ob_watcher")

TIMEFRAME_NAMES = {
    mt5.TIMEFRAME_M1:  "M1",
    mt5.TIMEFRAME_M5:  "M5",
    mt5.TIMEFRAME_M15: "M15",
    mt5.TIMEFRAME_M30: "M30",
    mt5.TIMEFRAME_H1:  "H1",
    mt5.TIMEFRAME_H4:  "H4",
    mt5.TIMEFRAME_D1:  "D1",
}


class OBWatcher(threading.Thread):
    """
    Standalone daemon thread that detects and tracks Order Blocks.

    Lifecycle:
      start() → runs until stop() is called
      update_settings() → hot-updates params mid-run
      get_obs() → thread-safe snapshot of latest active (non-mitigated) OBs
    """

    def __init__(
        self,
        symbol:           str,
        timeframe=None,
        min_impulse_pips: float = 3.0,
        lookback:         int   = 200,
        swing_lookback:   int   = 5,
        scan_interval:    float = 5.0,
        max_draw:         int   = 30,
        draw_on_chart:    bool  = True,
        log_fn=None,
    ):
        super().__init__(daemon=True)
        self.symbol           = symbol
        self.timeframe        = timeframe or mt5.TIMEFRAME_M1
        self.min_impulse_pips = min_impulse_pips
        self.lookback         = lookback
        self.swing_lookback   = swing_lookback
        self.scan_interval    = scan_interval
        self.max_draw         = max_draw
        self.draw_on_chart    = draw_on_chart
        self._log             = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_event      = threading.Event()

        # Public state — read from outside via get_obs()
        self.latest_obs: list[OrderBlock] = []
        self._lock          = threading.Lock()

        # Internal tracking
        self._last_count    = -1
        self._known_names   = set()   # names of OBs we've already logged
        self._mitigated_log = set()   # names we've already logged as mitigated

    # ── Public API ────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()

    def get_obs(self) -> list:
        """Thread-safe snapshot of active (non-mitigated) OBs."""
        with self._lock:
            return [ob for ob in self.latest_obs if not ob.mitigated]

    def get_all_obs(self) -> list:
        """Thread-safe snapshot including mitigated OBs."""
        with self._lock:
            return list(self.latest_obs)

    def update_settings(
        self,
        min_impulse_pips: float = None,
        lookback:         int   = None,
        swing_lookback:   int   = None,
        max_draw:         int   = None,
        draw_on_chart:    bool  = None,
    ):
        """Hot-update settings without restarting the thread."""
        if min_impulse_pips is not None: self.min_impulse_pips = min_impulse_pips
        if lookback         is not None: self.lookback         = lookback
        if swing_lookback   is not None: self.swing_lookback   = swing_lookback
        if max_draw         is not None: self.max_draw         = max_draw
        if draw_on_chart    is not None: self.draw_on_chart    = draw_on_chart
        # Force re-log and redraw on next cycle
        self._last_count  = -1
        self._known_names = set()

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        tf_name = TIMEFRAME_NAMES.get(self.timeframe, str(self.timeframe))
        self._log(
            f"🟦  OB Watcher started | {self.symbol} {tf_name} | "
            f"min_impulse={self.min_impulse_pips}pips | "
            f"lookback={self.lookback} | swing={self.swing_lookback}", "INFO"
        )

        # Small startup delay — let MT5 fully initialize
        self._stop_event.wait(2.5)

        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                self._log(f"💥 OB scan error: {type(e).__name__}: {e}", "ERROR")

            self._stop_event.wait(self.scan_interval)

        # Cleanup chart on stop
        if self.draw_on_chart:
            try:
                clear_obs_on_chart(self.symbol)
            except Exception:
                pass

        self._log("🟦  OB Watcher stopped", "INFO")

    # ── Single scan cycle ─────────────────────────────────────────

    def _cycle(self):
        # 1. Detect fresh OBs from candle data
        fresh_obs = detect_order_blocks(
            symbol           = self.symbol,
            timeframe        = self.timeframe,
            lookback         = self.lookback,
            min_impulse_pips = self.min_impulse_pips,
            swing_lookback   = self.swing_lookback,
        )

        # 2. Merge with existing knowledge (preserve mitigation state)
        merged = self._merge(fresh_obs)

        # 3. Check mitigation against current price
        merged = check_mitigation(merged, self.symbol)

        # 4. Update shared state
        with self._lock:
            self.latest_obs = merged

        # 5. Log new OBs
        self._log_new(merged)

        # 6. Log newly mitigated OBs
        self._log_mitigated(merged)

        # 7. Draw on chart (non-mitigated only)
        if self.draw_on_chart:
            try:
                draw_obs_on_chart(
                    self.symbol, merged,
                    max_draw  = self.max_draw,
                    timeframe = self.timeframe,
                )
            except Exception:
                pass  # EA not running — detection still works fine

    # ── Merge helper ──────────────────────────────────────────────

    def _merge(self, fresh: list) -> list:
        """
        Merge freshly detected OBs with previously known ones.
        Preserves mitigation state for OBs we already knew about.
        New OBs are appended. OBs that disappeared (scrolled off lookback)
        are dropped.
        """
        prev_by_name = {ob.name: ob for ob in self.latest_obs}
        result       = []
        for ob in fresh:
            if ob.name in prev_by_name:
                # Carry over mitigation state
                ob.mitigated = prev_by_name[ob.name].mitigated
            result.append(ob)
        return result

    # ── Logging helpers ───────────────────────────────────────────

    def _log_new(self, obs: list):
        new_obs = [ob for ob in obs if ob.name not in self._known_names]
        if not new_obs:
            return

        for ob in new_obs:
            self._known_names.add(ob.name)

        bull = sum(1 for ob in obs if ob.kind == "BULL" and not ob.mitigated)
        bear = sum(1 for ob in obs if ob.kind == "BEAR" and not ob.mitigated)
        total_active = bull + bear

        if total_active != self._last_count:
            self._last_count = total_active

            # Log up to 3 newest non-mitigated OBs as a summary
            active = [ob for ob in obs if not ob.mitigated][:3]
            summary = "  ".join(
                f"{'🟦' if ob.kind == 'BULL' else '🟣'}"
                f"{ob.gap_str} [{ob.method}]"
                for ob in active
            ) if active else "—"

            self._log(
                f"🟦  OB: {total_active} active | "
                f"🟦{bull} bull  🟣{bear} bear | "
                f"top: {self._top3_str(obs)} | "
                f"min={self.min_impulse_pips}pips", "NEW"
            )

    def _log_mitigated(self, obs: list):
        for ob in obs:
            if ob.mitigated and ob.name not in self._mitigated_log:
                self._mitigated_log.add(ob.name)
                icon = "🟦" if ob.kind == "BULL" else "🟣"
                self._log(
                    f"{icon}  OB mitigated: {ob.kind} [{ob.method}] "
                    f"zone {ob.bottom:.5f}–{ob.top:.5f} "
                    f"(impulse={ob.impulse_pips:.1f}pips) — removed from chart", "WARN"
                )

    def _top3_str(self, obs: list) -> str:
        active = [ob for ob in obs if not ob.mitigated]
        top3   = sorted(active, key=lambda o: o.impulse_pips, reverse=True)[:3]
        if not top3:
            return "—"
        return "  ".join(
            f"{'🟦' if ob.kind == 'BULL' else '🟣'}{ob.impulse_pips:.1f}p [{ob.method}]"
            for ob in top3
        )


# ── Patch OrderBlock with gap_str property (needed by watcher log) ─────────────
def _gap_str(self):
    return f"{self.impulse_pips:.1f}p"

OrderBlock.gap_str = property(_gap_str)