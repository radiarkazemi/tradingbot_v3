"""
resume.py — Bot State Recovery
================================
When the bot stops unexpectedly and restarts, this module scans MT5
for existing positions and pending orders placed by the bot (identified
by MAGIC_NUMBER), and rebuilds SourceState objects so monitoring
continues without interruption.

How it works:
  1. On start, scan mt5.positions_get() + mt5.orders_get() for bot magic
  2. Group them by source line price (inferred from SL — SL mirrors the line)
  3. Reconstruct a SourceState in ACTIVE or PENDING state
  4. Register it in the watcher's _sources dict

Source line price inference:
  BUY position:  entry = line + dist  →  line = entry - dist
                 SL    = line - dist  →  line = SL    + dist
  SELL position: entry = line - dist  →  line = entry + dist
                 SL    = line + dist  →  line = SL    - dist
  We use the SL formula as it's more stable.
"""
import MetaTrader5 as mt5
import logging
import time as _time
from config import MAGIC_NUMBER, MAX_ROUNDS, ORDER_DISTANCE_PIPS
from core.order_manager import get_pip_size, lot_for_round
from core.position_monitor import SourceState

log = logging.getLogger("resume")


def infer_line_price(position=None, order=None,
                     dist_pips: float = None, pip_size: float = None) -> float:
    """
    Infer the source line price from an existing position or pending order.

    For a BUY position: line = SL + dist  (SL is below line)
    For a SELL position: line = SL - dist  (SL is above line)
    For a BUY-STOP order: line = entry - dist
    For a SELL-STOP order: line = entry + dist
    """
    dist = (dist_pips or ORDER_DISTANCE_PIPS) * (pip_size or 0.0001)

    if position is not None:
        is_buy = position.type == 0
        sl     = position.sl
        if sl and sl > 0:
            return round(sl + dist if is_buy else sl - dist, 5)
        # Fallback: use entry
        entry = position.price_open
        return round(entry - dist if is_buy else entry + dist, 5)

    if order is not None:
        is_buy = order.type == mt5.ORDER_TYPE_BUY_STOP
        entry  = order.price_open
        return round(entry - dist if is_buy else entry + dist, 5)

    return 0.0


def scan_and_resume(symbol: str, dist_pips: float, pip_size: float,
                    base_lot: float, start_balance: float,
                    log_fn=None, stop_fn=None) -> list:
    """
    Scan MT5 for existing bot positions/orders and return reconstructed
    SourceState objects ready to be monitored.

    Returns list of (name, SourceState) tuples.
    """
    _log = log_fn or (lambda msg, level="INFO": log.info(msg))

    positions = mt5.positions_get(symbol=symbol) or []
    orders    = mt5.orders_get(symbol=symbol) or []

    bot_pos = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_ord = [o for o in orders    if o.magic == MAGIC_NUMBER]

    if not bot_pos and not bot_ord:
        return []

    _log(f"🔄  Resume: found {len(bot_pos)} position(s), {len(bot_ord)} pending order(s) from previous session", "NEW")

    # Group everything by inferred line price
    # key = rounded line price, value = dict of what we found
    groups: dict[float, dict] = {}

    def _key(price: float) -> float:
        return round(price, 5)

    for p in bot_pos:
        line = infer_line_price(position=p, dist_pips=dist_pips, pip_size=pip_size)
        k    = _key(line)
        if k not in groups:
            groups[k] = {"line": line, "positions": [], "orders": []}
        groups[k]["positions"].append(p)

    for o in bot_ord:
        line = infer_line_price(order=o, dist_pips=dist_pips, pip_size=pip_size)
        k    = _key(line)
        if k not in groups:
            groups[k] = {"line": line, "positions": [], "orders": []}
        groups[k]["orders"].append(o)

    recovered = []
    for k, g in groups.items():
        line  = g["line"]
        plist = g["positions"]
        olist = g["orders"]

        name  = f"RESUMED_{int(line * 100000)}"
        state = SourceState(
            name          = name,
            price         = line,
            pip_size      = pip_size,
            symbol        = symbol,
            base_lot      = base_lot,
            dist_pips     = dist_pips,
            start_balance = start_balance,
            log_fn        = log_fn,
            stop_fn       = stop_fn,
        )

        # Reconstruct buy/sell positions
        buy_pos  = [p for p in plist if p.type == 0]
        sell_pos = [p for p in plist if p.type == 1]
        buy_ord  = [o for o in olist if o.type == mt5.ORDER_TYPE_BUY_STOP]
        sell_ord = [o for o in olist if o.type == mt5.ORDER_TYPE_SELL_STOP]

        # Determine round from lot size
        if buy_pos:
            bp = buy_pos[0]
            state.buy_pos_ticket  = bp.ticket
            state.buy_sl          = bp.sl
            state.buy_lot         = bp.volume
            state._buy_confirmed  = True
            state.direction       = "BUY"

        if sell_pos:
            sp = sell_pos[0]
            state.sell_pos_ticket = sp.ticket
            state.sell_sl         = sp.sl
            state.sell_lot        = sp.volume
            state._sell_confirmed = True
            if not state.direction:
                state.direction = "SELL"

        if buy_ord:
            bo = sorted(buy_ord, key=lambda o: o.time_setup, reverse=True)[0]
            state.buy_ticket = bo.ticket
            state.buy_lot    = bo.volume_current

        if sell_ord:
            so = sorted(sell_ord, key=lambda o: o.time_setup, reverse=True)[0]
            state.sell_ticket = so.ticket
            state.sell_lot    = so.volume_current

        # Infer round from highest lot seen
        all_lots = [
            p.volume for p in plist
        ] + [o.volume_current for o in olist]
        if all_lots:
            max_lot = max(all_lots)
            # Estimate round: lot = base × 2^(round-1) → round = log2(lot/base) + 1
            import math
            if base_lot > 0 and max_lot >= base_lot:
                try:
                    state.round = max(1, round(math.log2(max_lot / base_lot) + 1))
                except Exception:
                    state.round = 1
            else:
                state.round = 1

        # Set correct state
        if plist:
            state.state          = SourceState.ACTIVE
            state._activated_at  = _time.time() - 30  # pretend it's been active
        elif olist:
            state.state = SourceState.PENDING
        else:
            state.state = SourceState.IDLE

        desc_parts = []
        if buy_pos:  desc_parts.append(f"BUY pos#{buy_pos[0].ticket} lot={buy_pos[0].volume:.2f}")
        if sell_pos: desc_parts.append(f"SELL pos#{sell_pos[0].ticket} lot={sell_pos[0].volume:.2f}")
        if buy_ord:  desc_parts.append(f"BUY-STOP#{buy_ord[0].ticket} lot={buy_ord[0].volume_current:.2f}")
        if sell_ord: desc_parts.append(f"SELL-STOP#{sell_ord[0].ticket} lot={sell_ord[0].volume_current:.2f}")

        _log(
            f"✅  Resumed: line≈{line:.5f} R{state.round} | "
            + " | ".join(desc_parts), "NEW"
        )
        recovered.append((name, state))

    return recovered