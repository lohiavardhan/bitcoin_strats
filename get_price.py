# pip install requests
import time
import requests
from collections import deque
from dataclasses import dataclass

# ----------------------------
# Config
# ----------------------------
PRODUCT_ID = "BTC-USD"
TICKER_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"

POLL_EVERY = 1.0
SESSION_TIMEOUT = 5

# Windows (seconds)
W_2M = 120
W_15M = 15 * 60

# Vol contraction: compare 2m range vs a longer sliding window (no need to wait 30 minutes)
W_LONG = 10 * 60            # 10-minute sliding window
MIN_LONG_SAMPLES = 60       # require at least this many points before using contraction filter

# Strategy knobs
VOL_CONTRACTION_THRESH = 0.35
STOP_K_RANGE = 1.5
REJECTION_TIMEOUT = 10      # seconds: breach must reject (re-enter) within this time

# Higher timeframe bias (15m)
MIN_15M_MOVE_BP = 1.0       # below this -> neutral bias

# Execution-cost model (paper)
TAKER_FEE = 0.006
SLIPPAGE_BP = 2.0
SPREAD_BP = 2.0

# Paper portfolio
START_CASH_USD = 10_000.0
NOTIONAL_PER_TRADE_USD = 1_000.0

# Logging
PRINT_INTERVAL = 30.0       # print status every 30s (but print immediately on events)


# ----------------------------
# Helpers
# ----------------------------
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def bp(x: float) -> float:
    return x * 10000.0

def prune(win: deque, now: float, horizon_s: int):
    cutoff = now - horizon_s
    while win and win[0][0] < cutoff:
        win.popleft()

def window_min_max(win: deque):
    prices = [p for _, p in win]
    return min(prices), max(prices)

def exec_cost_fraction() -> float:
    return TAKER_FEE + (SLIPPAGE_BP / 10000.0) + (SPREAD_BP / 20000.0)

def fill_price(mid: float, side: str) -> float:
    c = exec_cost_fraction()
    if side == "buy":
        return mid * (1.0 + c)
    if side == "sell":
        return mid * (1.0 - c)
    raise ValueError("side must be 'buy' or 'sell'")

def bias_15m(win_15m: deque) -> str:
    if len(win_15m) < 2:
        return "neutral"
    p0 = win_15m[0][1]
    p1 = win_15m[-1][1]
    ret = (p1 / p0) - 1.0
    if abs(bp(ret)) < MIN_15M_MOVE_BP:
        return "neutral"
    return "up" if ret > 0 else "down"

def contraction(win_2m: deque, win_long: deque):
    """
    contraction ratio = (2m_range / long_range)
    Uses sliding window; only enabled once we have MIN_LONG_SAMPLES.
    """
    if len(win_2m) < 2 or len(win_long) < 2:
        return False, None

    if len(win_long) < MIN_LONG_SAMPLES:
        return False, None

    lo2, hi2 = window_min_max(win_2m)
    lol, hil = window_min_max(win_long)
    r2 = hi2 - lo2
    rl = hil - lol
    if rl <= 0:
        return False, None
    ratio = r2 / rl
    return ratio <= VOL_CONTRACTION_THRESH, ratio

def event_print(msg: str):
    print(f"{now_str()} {msg}")


# ----------------------------
# Paper-only portfolio state (NO orders sent anywhere)
# ----------------------------
@dataclass
class Position:
    side: str
    qty_btc: float
    entry_px: float
    stop_px: float
    target_px: float
    entry_time: float

@dataclass
class Portfolio:
    cash_usd: float
    pos: Position | None = None

    def mark_to_market(self, mid_px: float) -> float:
        if self.pos is None:
            return self.cash_usd
        if self.pos.side == "long":
            return self.cash_usd + self.pos.qty_btc * mid_px
        # paper short MTM (simplified)
        return self.cash_usd - self.pos.qty_btc * mid_px


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "btc-paper-only/1.0"})

    # Sliding windows
    win_2m = deque()
    win_15m = deque()
    win_long = deque()

    # Seed one tick
    r = session.get(TICKER_URL, timeout=SESSION_TIMEOUT)
    r.raise_for_status()
    mid_px = float(r.json()["price"])
    t0 = time.time()
    for w in (win_2m, win_15m, win_long):
        w.append((t0, mid_px))

    pf = Portfolio(cash_usd=START_CASH_USD)

    breach = None
    # breach = {"dir","hi","lo","mid","t","range","bias","contracted","ratio"}

    last_print = time.time()
    event_print(f"START | cash=${pf.cash_usd:.2f} | exec_cost/side≈{exec_cost_fraction()*100:.3f}% | "
                f"W_LONG={W_LONG}s MIN_LONG_SAMPLES={MIN_LONG_SAMPLES}")

    while True:
        loop_start = time.time()

        # Poll
        r = session.get(TICKER_URL, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
        mid_px = float(r.json()["price"])
        now = time.time()

        # Compute bands from PREVIOUS windows (exclude current tick)
        lo2_prev, hi2_prev = window_min_max(win_2m) if len(win_2m) >= 2 else (mid_px, mid_px)
        mid2_prev = (hi2_prev + lo2_prev) / 2.0
        r2_prev = hi2_prev - lo2_prev

        b15 = bias_15m(win_15m)
        contracted, ratio = contraction(win_2m, win_long)

        # ----------------------------
        # Manage open position (paper fills only)
        # ----------------------------
        if pf.pos is not None:
            p = pf.pos
            if p.side == "long":
                if mid_px <= p.stop_px:
                    exit_px = fill_price(mid_px, "sell")
                    pnl = (exit_px - p.entry_px) * p.qty_btc
                    pf.cash_usd += p.qty_btc * exit_px
                    pf.pos = None
                    event_print(f"PAPER STOP LONG | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f}")
                elif mid_px >= p.target_px:
                    exit_px = fill_price(mid_px, "sell")
                    pnl = (exit_px - p.entry_px) * p.qty_btc
                    pf.cash_usd += p.qty_btc * exit_px
                    pf.pos = None
                    event_print(f"PAPER TP LONG | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f}")
            else:  # short
                if mid_px >= p.stop_px:
                    exit_px = fill_price(mid_px, "buy")
                    pnl = (p.entry_px - exit_px) * p.qty_btc
                    pf.cash_usd -= p.qty_btc * exit_px
                    pf.pos = None
                    event_print(f"PAPER STOP SHORT | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f}")
                elif mid_px <= p.target_px:
                    exit_px = fill_price(mid_px, "buy")
                    pnl = (p.entry_px - exit_px) * p.qty_btc
                    pf.cash_usd -= p.qty_btc * exit_px
                    pf.pos = None
                    event_print(f"PAPER TP SHORT | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f}")

        # ----------------------------
        # Entry logic (paper only; sends nothing)
        # ----------------------------
        if pf.pos is None:
            # If breach exists, wait for rejection (re-entry)
            if breach is not None:
                if (now - breach["t"]) > REJECTION_TIMEOUT:
                    breach = None
                    event_print("BREACH EXPIRED (no rejection)")
                else:
                    # Re-entry confirmation
                    if breach["dir"] == "above" and mid_px <= breach["hi"]:
                        if breach["contracted"] and breach["bias"] in ("neutral", "down"):
                            entry_px = fill_price(mid_px, "sell")
                            qty = NOTIONAL_PER_TRADE_USD / entry_px
                            stop_px = entry_px + STOP_K_RANGE * breach["range"]
                            target_px = breach["mid"]
                            pf.cash_usd += qty * entry_px
                            pf.pos = Position("short", qty, entry_px, stop_px, target_px, now)
                            event_print(
                                f"PAPER ENTER SHORT | entry={entry_px:.2f} stop={stop_px:.2f} target={target_px:.2f} "
                                f"range={breach['range']:.2f} bias15={breach['bias']} ratio={breach['ratio']:.3f}"
                            )
                        breach = None

                    elif breach["dir"] == "below" and mid_px >= breach["lo"]:
                        if breach["contracted"] and breach["bias"] in ("neutral", "up"):
                            entry_px = fill_price(mid_px, "buy")
                            qty = NOTIONAL_PER_TRADE_USD / entry_px
                            stop_px = entry_px - STOP_K_RANGE * breach["range"]
                            target_px = breach["mid"]
                            pf.cash_usd -= qty * entry_px
                            pf.pos = Position("long", qty, entry_px, stop_px, target_px, now)
                            event_print(
                                f"PAPER ENTER LONG | entry={entry_px:.2f} stop={stop_px:.2f} target={target_px:.2f} "
                                f"range={breach['range']:.2f} bias15={breach['bias']} ratio={breach['ratio']:.3f}"
                            )
                        breach = None

            # No active breach: detect breach vs PREVIOUS 2m band
            if breach is None and len(win_2m) >= 10:
                if mid_px > hi2_prev:
                    breach = {
                        "dir": "above",
                        "hi": hi2_prev, "lo": lo2_prev, "mid": mid2_prev,
                        "t": now,
                        "range": max(r2_prev, 1e-9),
                        "bias": b15,
                        "contracted": contracted,
                        "ratio": (ratio if ratio is not None else float("nan")),
                    }
                    event_print(
                        f"BREACH ARMED ABOVE | hi={hi2_prev:.2f} lo={lo2_prev:.2f} range={r2_prev:.2f} "
                        f"bias15={b15} contracted={contracted} ratio={(ratio if ratio is not None else float('nan')):.3f}"
                    )

                elif mid_px < lo2_prev:
                    breach = {
                        "dir": "below",
                        "hi": hi2_prev, "lo": lo2_prev, "mid": mid2_prev,
                        "t": now,
                        "range": max(r2_prev, 1e-9),
                        "bias": b15,
                        "contracted": contracted,
                        "ratio": (ratio if ratio is not None else float("nan")),
                    }
                    event_print(
                        f"BREACH ARMED BELOW | hi={hi2_prev:.2f} lo={lo2_prev:.2f} range={r2_prev:.2f} "
                        f"bias15={b15} contracted={contracted} ratio={(ratio if ratio is not None else float('nan')):.3f}"
                    )

        # ----------------------------
        # Append current tick and prune (sliding windows)
        # ----------------------------
        for w, horizon in ((win_2m, W_2M), (win_15m, W_15M), (win_long, W_LONG)):
            w.append((now, mid_px))
            prune(w, now, horizon)

        # ----------------------------
        # Status print every 30 seconds (or immediate on events via event_print)
        # ----------------------------
        if (now - last_print) >= PRINT_INTERVAL:
            pv = pf.mark_to_market(mid_px)

            pos_txt = "FLAT"
            if pf.pos is not None:
                p = pf.pos
                upnl = p.qty_btc * (mid_px - p.entry_px) if p.side == "long" else p.qty_btc * (p.entry_px - mid_px)
                pos_txt = (
                    f"{p.side.upper()} qty={p.qty_btc:.6f} "
                    f"entry={p.entry_px:.2f} stop={p.stop_px:.2f} "
                    f"tgt={p.target_px:.2f} uPnL=${upnl:.2f}"
                )

            btxt = "none"
            if breach is not None:
                btxt = (
                    f"{breach['dir']} "
                    f"(bias15={breach['bias']} contracted={breach['contracted']} ratio={breach['ratio']:.3f})"
                )

            ratio_txt = f"{ratio:.3f}" if ratio is not None else "NA"

            print(
                f"{now_str()} | px={mid_px:.2f} | prev2m_hi={hi2_prev:.2f} prev2m_lo={lo2_prev:.2f} prev_r2={r2_prev:.2f} | "
                f"bias15={b15} | contracted={contracted} ratio={ratio_txt} (2m/long {W_LONG//60}m) | "
                f"pos={pos_txt} | cash=${pf.cash_usd:.2f} PV=${pv:.2f} | breach={btxt} | long_samples={len(win_long)}"
            )

            last_print = now

        # Sleep
        elapsed = time.time() - loop_start
        time.sleep(max(0.0, POLL_EVERY - elapsed))


if __name__ == "__main__":
    main()
