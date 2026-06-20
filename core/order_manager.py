"""
order_manager.py — TraderBot v2
"""
import MetaTrader5 as mt5
import logging
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

log = logging.getLogger("orders_v2")


def _filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_RETURN
    m = info.filling_mode
    if m & 4: return mt5.ORDER_FILLING_RETURN
    if m & 2: return mt5.ORDER_FILLING_IOC
    if m & 1: return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _min_stop_dist(symbol: str) -> float:
    """
    Minimum allowed distance for SL/TP from entry, in price units.
    Broker-side floor (trade_stops_level × point × 1.5) plus an
    explicit SLIPPAGE_BUFFER_PIPS on top — the broker's number is the
    bare minimum it will accept; ordinary slippage on a fill can still
    push a placement that's only just past that minimum into outright
    rejection, so this adds real headroom rather than skating the line.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0
    broker_floor = info.trade_stops_level * info.point * 1.5
    try:
        from config import SLIPPAGE_BUFFER_PIPS
        pip_size = get_pip_size(symbol)
        broker_floor += SLIPPAGE_BUFFER_PIPS * pip_size
    except Exception:
        pass
    return broker_floor


def _round_price(price: float, symbol: str) -> float:
    info   = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    return round(price, digits)


def _adjust_sl(entry: float, sl: float, symbol: str, is_buy: bool) -> float:
    min_d = _min_stop_dist(symbol)
    if min_d <= 0:
        return sl
    if is_buy:
        if entry - sl < min_d:
            sl = entry - min_d
    else:
        if sl - entry < min_d:
            sl = entry + min_d
    return _round_price(sl, symbol)


def _adjust_tp(entry: float, tp: float, symbol: str, is_buy: bool) -> float:
    if tp == 0.0:
        return 0.0
    min_d = _min_stop_dist(symbol)
    if min_d <= 0:
        return tp
    if is_buy:
        if tp - entry < min_d:
            tp = entry + min_d
    else:
        if entry - tp < min_d:
            tp = entry - min_d
    return _round_price(tp, symbol)


def get_pip_size(symbol: str) -> float:
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None:
        sym = symbol.upper()
        if "JPY" in sym: return 0.01
        if "XAU" in sym: return 0.10
        if "XAG" in sym: return 0.01
        if any(x in sym for x in ["US30","NAS","DAX","FTSE","SPX"]): return 1.0
        return 0.0001
    if info.digits in (0, 1): return 1.0
    if info.digits in (2, 3): return info.point * 10
    return info.point * 10


def lot_for_round(round_num: int, base_lot: float) -> float:
    from config import LOT_MULTIPLIER
    lot = base_lot * (LOT_MULTIPLIER ** (round_num - 1))
    return round(lot, 2)


def build_pair(source_price: float, pip_size: float, dist_pips: float,
               symbol: str, round_num: int = 1, lot: float = 0.01) -> list:
    """
    Build BUY-STOP + SELL-STOP around source_price.
    BUY-STOP  entry = source + dist   SL = source - dist
    SELL-STOP entry = source - dist   SL = source + dist
    SL widened if inside broker minimum stop distance.
    """
    from config import TP_RR_RATIO
    dist = dist_pips * pip_size

    buy_entry  = _round_price(source_price + dist, symbol)
    sell_entry = _round_price(source_price - dist, symbol)
    buy_sl     = _adjust_sl(buy_entry,  _round_price(source_price - dist, symbol), symbol, True)
    sell_sl    = _adjust_sl(sell_entry, _round_price(source_price + dist, symbol), symbol, False)

    sl_dist_buy  = buy_entry  - buy_sl
    sl_dist_sell = sell_sl    - sell_entry

    if TP_RR_RATIO > 0:
        buy_tp  = _adjust_tp(buy_entry,
                             _round_price(buy_entry  + sl_dist_buy  * TP_RR_RATIO, symbol),
                             symbol, True)
        sell_tp = _adjust_tp(sell_entry,
                             _round_price(sell_entry - sl_dist_sell * TP_RR_RATIO, symbol),
                             symbol, False)
    else:
        buy_tp  = 0.0
        sell_tp = 0.0

    return [
        {"type": "BUY_STOP",  "entry": buy_entry,  "sl": buy_sl,  "tp": buy_tp,
         "lot": lot, "source": source_price, "round": round_num,
         "sl_pips": round(sl_dist_buy  / pip_size, 1)},
        {"type": "SELL_STOP", "entry": sell_entry, "sl": sell_sl, "tp": sell_tp,
         "lot": lot, "source": source_price, "round": round_num,
         "sl_pips": round(sl_dist_sell / pip_size, 1)},
    ]


def send_pair(orders: list, symbol: str) -> list:
    """
    Send orders to MT5.

    If a stop order's entry is already past the current market price
    (i.e. price moved through it before we could place it), automatically
    convert it to a market order so it fills immediately at current price
    with the correct SL/TP.
    """
    from config import MAGIC_NUMBER
    mt5.symbol_select(symbol, True)
    filling = _filling_mode(symbol)

    tick = mt5.symbol_info_tick(symbol)
    bid  = tick.bid if tick else 0.0
    ask  = tick.ask if tick else 0.0

    results = []
    for o in orders:
        is_buy     = o["type"] == "BUY_STOP"
        rnd        = o.get("round", 1)
        entry      = o["entry"]
        sl         = o["sl"]
        tp         = o["tp"]
        lot        = o["lot"]

        # Determine if stop entry is already inside the market
        # BUY_STOP  is invalid if entry <= ask  (price already above entry)
        # SELL_STOP is invalid if entry >= bid  (price already below entry)
        already_past = False
        if is_buy  and ask > 0 and entry <= ask:
            already_past = True
        if not is_buy and bid > 0 and entry >= bid:
            already_past = True

        if already_past:
            # Convert to market order — fill at current price with same SL/TP
            market_price = ask if is_buy else bid
            order_type   = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            action       = mt5.TRADE_ACTION_DEAL

            # Recalculate SL/TP relative to actual market price
            # keep same distance as original SL was from entry
            sl_dist = abs(entry - sl)
            tp_dist = abs(entry - tp) if tp else 0.0

            new_sl = _round_price(market_price - sl_dist if is_buy else market_price + sl_dist, symbol)
            new_tp = _round_price(market_price + tp_dist if is_buy else market_price - tp_dist, symbol) if tp_dist else 0.0
            new_sl = _adjust_sl(market_price, new_sl, symbol, is_buy)
            if new_tp:
                new_tp = _adjust_tp(market_price, new_tp, symbol, is_buy)

            log.warning("⚡ %s R%d entry=%.5f already past market (bid=%.5f ask=%.5f) — "
                        "converting to MARKET order @ %.5f sl=%.5f tp=%.5f",
                        o["type"], rnd, entry, bid, ask, market_price, new_sl, new_tp)

            request = {
                "action":       action,
                "symbol":       symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        market_price,
                "sl":           new_sl,
                "tp":           new_tp,
                "deviation":    30,
                "magic":        MAGIC_NUMBER,
                "comment":      f"TB2_R{rnd}{'B' if is_buy else 'S'}m",
                "type_filling": filling,
            }
        else:
            # Normal pending stop order
            order_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
            request = {
                "action":       mt5.TRADE_ACTION_PENDING,
                "symbol":       symbol,
                "volume":       lot,
                "type":         order_type,
                "price":        entry,
                "sl":           sl,
                "tp":           tp,
                "deviation":    20,
                "magic":        MAGIC_NUMBER,
                "comment":      f"TB2_R{rnd}{'B' if is_buy else 'S'}",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }

        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            mode = "MARKET" if already_past else "STOP"
            log.info("✅ %s R%d [%s] | ticket=#%d entry/price=%.5f sl=%.5f tp=%.5f lot=%.2f",
                     o["type"], rnd, mode, res.order,
                     request["price"], request["sl"], request.get("tp", 0), lot)
            results.append({"order": o, "ticket": res.order, "ok": True,
                            "market": already_past})
        else:
            code    = res.retcode if res else -1
            comment = (res.comment if res else "no response") or ""
            ERRS    = {
                10018: "Market closed",
                10019: "No money",
                10016: "Stops too close",
                10015: "Invalid SL/TP",
                10014: "Invalid volume",
                10013: "Invalid price",
                10006: "Rejected",
                10004: "Requote",
            }
            reason = ERRS.get(code, f"retcode={code}")
            log.error("❌ %s R%d FAILED | %s | broker: '%s' | "
                      "entry=%.5f sl=%.5f bid=%.5f ask=%.5f",
                      o["type"], rnd, reason, comment,
                      entry, sl, bid, ask)
            results.append({"order": o, "ticket": None, "ok": False,
                            "retcode": code, "reason": f"{reason} | {comment}"})
    return results


def cancel_order(ticket: int) -> bool:
    res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
    ok  = res and res.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log.info("🗑️  Cancelled order #%d", ticket)
    else:
        log.warning("⚠️  Failed to cancel #%d: %s", ticket,
                    res.retcode if res else "no response")
    return ok


def cancel_all_bot_orders(symbol: str) -> int:
    from config import MAGIC_NUMBER
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return 0
    cancelled = 0
    for o in orders:
        if o.magic == MAGIC_NUMBER:
            if cancel_order(o.ticket):
                cancelled += 1
    return cancelled


def get_bot_positions(symbol: str) -> list:
    from config import MAGIC_NUMBER
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [p for p in positions if p.magic == MAGIC_NUMBER]


def get_bot_pending(symbol: str) -> list:
    from config import MAGIC_NUMBER
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return []
    return [o for o in orders if o.magic == MAGIC_NUMBER]