"""
fvg_entry_state.py — FVG-Anchored Entry Source
================================================
Subclasses SourceState to implement edge-anchored entries instead of
the standard center-line + symmetric-distance entries.

LOGIC (per user spec):
  Given a 5M FVG with top/bottom edges:

  BUY-STOP  entry = top    − dist
            SL    = entry  − 2×dist   (= top − 3×dist)
            TP    = entry  + (FVG height × 3)     [1:3 RR based on FVG size]

  SELL-STOP entry = bottom + dist
            SL    = entry  + 2×dist   (= bottom + 3×dist)
            TP    = entry  − (FVG height × 3)

  dist = dist_pips × pip_size (same config value used by the
  line-based entries elsewhere in the bot)

  Everything else — activation, lot doubling (sell_lot = buy_lot × 2
  on activation, doubling again on each SL hit), margin protection,
  balance TP, no round limit — is inherited unchanged from SourceState.

Trigger:
  Same touch logic as line-based entries, just using the FVG's
  price range [bottom, top] as the "line" zone instead of a single
  price. A touch fires when tick price enters [bottom, top].
"""

from core.position_monitor import SourceState
from core.order_manager import _round_price


class FVGEntryState(SourceState):
    """
    SourceState variant anchored to an FVG's top/bottom edges instead
    of a single center price. Adds a 1:3 RR take-profit based on the
    FVG's own height.
    """

    def __init__(self, name, fvg_top, fvg_bottom, pip_size, symbol,
                 base_lot, dist_pips, start_balance=0.0,
                 log_fn=None, stop_fn=None):
        # self.price is kept as the FVG midpoint purely for logging/
        # summary display and for the SourceState.reset()/resume.py
        # save_session() code paths that reference it generically.
        # All actual entry/SL/TP math below uses fvg_top/fvg_bottom
        # directly and ignores self.price.
        mid = round((fvg_top + fvg_bottom) / 2, 5)
        super().__init__(
            name=name, price=mid, pip_size=pip_size, symbol=symbol,
            base_lot=base_lot, dist_pips=dist_pips,
            start_balance=start_balance, log_fn=log_fn, stop_fn=stop_fn,
        )
        self.fvg_top    = fvg_top
        self.fvg_bottom = fvg_bottom

    # ── FVG geometry ──────────────────────────────────────────────

    @property
    def _fvg_height(self) -> float:
        return self.fvg_top - self.fvg_bottom

    # ── Entry / SL overrides (edge-anchored, not center+symmetric) ─

    @property
    def _buy_entry(self):
        return _round_price(self.fvg_top - self._dist, self.symbol)

    @property
    def _sell_entry(self):
        return _round_price(self.fvg_bottom + self._dist, self.symbol)

    @property
    def _buy_sl_price(self):
        # Self-mirrored around BUY's own entry, NOT tied to the zone
        # bottom: SL = entry - 2*dist = top - 3*dist
        return _round_price(self._buy_entry - 2 * self._dist, self.symbol)

    @property
    def _sell_sl_price(self):
        # Self-mirrored around SELL's own entry: SL = entry + 2*dist
        # = bottom + 3*dist
        return _round_price(self._sell_entry + 2 * self._dist, self.symbol)

    # ── Re-anchoring on SL close (zero-spread fix) ──────────────────
    # BUY and SELL entries here are independent edges (top vs bottom),
    # not mirrored around one shared center like the base class. So
    # re-anchoring on a BUY close only shifts fvg_top (which is all
    # _buy_entry depends on); fvg_bottom / the SELL side is untouched.
    # This also means the FVG's height changes slightly if the close
    # price drifted from the original edge, which correctly carries
    # through to a slightly adjusted TP (still height x 3) rather than
    # silently keeping a stale height.

    def _reanchor_buy(self, close_price: float):
        self.fvg_top = close_price + self._dist

    def _reanchor_sell(self, close_price: float):
        self.fvg_bottom = close_price - self._dist

    # ── Take profit: 1:3 RR based on FVG height ────────────────────

    @property
    def _buy_tp_price(self):
        return _round_price(self._buy_entry + self._fvg_height * 3, self.symbol)

    @property
    def _sell_tp_price(self):
        return _round_price(self._sell_entry - self._fvg_height * 3, self.symbol)

    # ── Touch detection: zone-based instead of single-price ────────

    def check_touch(self, bid: float, ask: float) -> bool:
        """
        Touch fires when tick price enters the FVG's own price range
        [fvg_bottom, fvg_top] — the rectangle drawn on chart — rather
        than a single line price. Crossing detection (for fast moves
        that jump over the zone between ticks) checks whether the
        zone overlaps the bid-ask span seen between the previous and
        current tick.
        """
        if self.state != self.IDLE:
            return False
        if bid <= 0 or ask <= 0:
            return False

        mid = (bid + ask) / 2
        top, bottom = self.fvg_top, self.fvg_bottom

        touched = False
        desc    = ""

        # Direct: current tick's bid/ask range overlaps the FVG zone
        if bid <= top and ask >= bottom:
            touched = True
            desc    = f"price in FVG zone bid={bid:.5f} ask={ask:.5f}"

        # Crossing: mid price moved from outside the zone to inside
        # (or through it) since the last tick we saw.
        elif self._prev_tick_price is not None:
            prev = self._prev_tick_price
            prev_inside = bottom <= prev <= top
            cur_inside  = bottom <= mid  <= top
            crossed_through = (prev < bottom and mid > top) or (prev > top and mid < bottom)
            if (not prev_inside and cur_inside) or crossed_through:
                touched = True
                desc    = f"crossed into FVG zone {prev:.5f}→{mid:.5f}"

        self._prev_tick_price = mid

        if touched:
            height_pips = round(self._fvg_height / self.pip_size, 1)
            self._log(
                f"🎯  [{self.name[:20]}] FVG zone touched "
                f"[{bottom:.5f}-{top:.5f}] ({height_pips}pips) ({desc}) | "
                f"dist={self.dist_pips}pips | placing orders", "NEW"
            )
            self.place_initial_pair()
            return True

        return False

    @property
    def summary(self) -> dict:
        base = super().summary
        base["fvg_top"]    = self.fvg_top
        base["fvg_bottom"] = self.fvg_bottom
        base["tp_buy"]     = self._buy_tp_price
        base["tp_sell"]    = self._sell_tp_price
        return base