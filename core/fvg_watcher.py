"""
fvg_watcher.py — FVG background scanner
========================================
Completely independent thread — does NOT wait for EA file or chart lines.
Scans MT5 candles directly via Python API as soon as MT5 is connected.
Drawing to chart is optional — if EA is not running, FVGs are still
logged and available for use.
"""
import threading
import time as _time
import logging
import MetaTrader5 as mt5
from core.fvg_detector import detect_fvgs, draw_fvgs_on_chart, clear_fvgs_on_chart

log = logging.getLogger("fvg_watcher")

TIMEFRAME_NAMES = {
    mt5.TIMEFRAME_M1:  "M1",
    mt5.TIMEFRAME_M5:  "M5",
    mt5.TIMEFRAME_M15: "M15",
    mt5.TIMEFRAME_M30: "M30",
    mt5.TIMEFRAME_H1:  "H1",
    mt5.TIMEFRAME_H4:  "H4",
    mt5.TIMEFRAME_D1:  "D1",
}


class FVGWatcher(threading.Thread):
    """
    Standalone daemon thread that:
    1. Scans MT5 candles for FVG patterns every scan_interval seconds
    2. Logs found FVGs (always works, no EA needed)
    3. Tries to draw rectangles on chart via command file (optional, skips silently if EA not running)
    4. Exposes latest FVGs via self.latest_fvgs for other modules to read
    """

    def __init__(self, symbol: str, timeframe=None,
                 min_gap_pips: float = 3.0,
                 lookback: int = 200,
                 scan_interval: float = 5.0,
                 max_draw: int = 30,
                 draw_on_chart: bool = True,
                 log_fn=None):
        super().__init__(daemon=True)
        self.symbol        = symbol
        self.timeframe     = timeframe or mt5.TIMEFRAME_M1
        self.min_gap_pips  = min_gap_pips
        self.lookback      = lookback
        self.scan_interval = scan_interval
        self.max_draw      = max_draw
        self.draw_on_chart = draw_on_chart
        self._log          = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop         = threading.Event()

        # Public: other modules can read this
        self.latest_fvgs   = []
        self._last_count   = -1
        self._lock         = threading.Lock()

    def stop(self):
        self._stop.set()

    def get_fvgs(self):
        """Thread-safe access to latest detected FVGs."""
        with self._lock:
            return list(self.latest_fvgs)

    def update_settings(self, min_gap_pips: float = None,
                        lookback: int = None, max_draw: int = None,
                        draw_on_chart: bool = None):
        """Hot-update settings without restarting."""
        if min_gap_pips  is not None: self.min_gap_pips  = min_gap_pips
        if lookback      is not None: self.lookback      = lookback
        if max_draw      is not None: self.max_draw      = max_draw
        if draw_on_chart is not None: self.draw_on_chart = draw_on_chart
        self._last_count = -1  # force re-log on next scan

    def run(self):
        tf_name = TIMEFRAME_NAMES.get(self.timeframe, str(self.timeframe))
        self._log(
            f"📐  FVG Watcher started | {self.symbol} {tf_name} | "
            f"min={self.min_gap_pips}pips | lookback={self.lookback}", "INFO"
        )

        # Small startup delay — let MT5 initialize fully
        self._stop.wait(2.0)

        while not self._stop.is_set():
            try:
                # Scan MT5 directly — no EA needed for this
                fvgs = detect_fvgs(
                    symbol       = self.symbol,
                    timeframe    = self.timeframe,
                    lookback     = self.lookback,
                    min_gap_pips = self.min_gap_pips,
                )

                with self._lock:
                    self.latest_fvgs = fvgs

                # Log only when count changes
                if len(fvgs) != self._last_count:
                    bull = sum(1 for f in fvgs if f.kind == "BULL")
                    bear = sum(1 for f in fvgs if f.kind == "BEAR")
                    self._last_count = len(fvgs)

                    if fvgs:
                        # Log the 3 most significant (largest gap)
                        top3 = sorted(fvgs, key=lambda f: f.gap_pips, reverse=True)[:3]
                        summary = "  ".join(
                            f"{'🟢' if f.kind=='BULL' else '🔴'}{f.gap_pips:.1f}p"
                            for f in top3
                        )
                        self._log(
                            f"📐  FVG: {len(fvgs)} gaps | "
                            f"🟢{bull} bull  🔴{bear} bear | "
                            f"top3: {summary} | min={self.min_gap_pips}pips", "NEW"
                        )
                    else:
                        self._log(
                            f"📐  FVG: 0 gaps found (min={self.min_gap_pips}pips "
                            f"may be too high, try lowering it)", "INFO"
                        )

                # Draw on chart — independent, silently skips if EA not running
                if self.draw_on_chart and fvgs:
                    try:
                        draw_fvgs_on_chart(self.symbol, fvgs,
                                           max_draw=self.max_draw,
                                           timeframe=self.timeframe)
                    except Exception:
                        pass  # EA not running — detection still works

            except Exception as e:
                self._log(f"💥 FVG scan error: {type(e).__name__}: {e}", "ERROR")

            self._stop.wait(self.scan_interval)

        # Cleanup chart on stop
        if self.draw_on_chart:
            try:
                clear_fvgs_on_chart(self.symbol)
            except Exception:
                pass

        self._log("📐  FVG Watcher stopped", "INFO")