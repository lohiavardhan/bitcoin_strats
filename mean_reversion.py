import time
import requests
from collections import deque
import numpy as np
from dataclasses import dataclass
from typing import List

# ----------------------------
# Config
# ----------------------------
PRODUCT_ID = "BTC-USD"
TICKER_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"
CANDLES_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/candles"

POLL_EVERY = 1.0  # polling every second
SESSION_TIMEOUT = 5  # request timeout in seconds

# Bollinger Band config
BOLLINGER_WINDOW = 20  # Lookback period (20 periods)
BOLLINGER_STD_DEV = 2  # Number of standard deviations for bands

# Strategy knobs
STOP_K_RANGE = 1.5  # stop = K * (std_dev at signal time)
MAX_POSITIONS = 3  # Maximum number of concurrent positions

# Paper portfolio
START_CASH_USD = 10_000.0
NOTIONAL_PER_TRADE_USD = 1_000.0

# Logging
PRINT_EVERY = 10.0  # print every 10 seconds


# ----------------------------
# Helpers
# ----------------------------
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def moving_average(prices, window):
    """Calculate moving average over the last 'window' prices."""
    if len(prices) >= window:
        return np.mean(list(prices)[-window:])
    return None


def std_dev(prices, window):
    """Calculate standard deviation over the last 'window' prices."""
    if len(prices) >= window:
        return np.std(list(prices)[-window:], ddof=1)  # sample std dev
    return None


def exec_cost_fraction() -> float:
    """Approximate per-side market execution cost."""
    return 0  # TAKER_FEE assumption, or 0 for pure paper trading


def fill_price(mid: float, side: str) -> float:
    """Simulate execution price including slippage/fees.
    
    For opening trades:
    - 'buy' means you're paying more (unfavorable slippage)
    - 'sell' means you're receiving less (unfavorable slippage)
    
    Returns the effective price after costs.
    """
    cost = exec_cost_fraction()
    if side == "buy":
        return mid * (1.0 + cost)  # Pay more when buying
    elif side == "sell":
        return mid * (1.0 - cost)  # Receive less when selling
    else:
        raise ValueError("side must be 'buy' or 'sell'")


# ----------------------------
# Paper-only portfolio state (NO orders sent anywhere)
# ----------------------------
@dataclass
class Position:
    id: int  # Unique position ID
    side: str
    qty_btc: float
    entry_px: float
    stop_px: float
    target_px: float
    entry_time: float


@dataclass
class Portfolio:
    cash_usd: float
    positions: List[Position]
    next_pos_id: int = 0

    def mark_to_market(self, mid_px: float) -> float:
        """Calculate total portfolio value at current market price."""
        total = self.cash_usd
        for pos in self.positions:
            if pos.side == "long":
                total += pos.qty_btc * mid_px
            else:  # short
                total -= pos.qty_btc * mid_px
        return total

    def add_position(self, pos: Position):
        """Add a new position to the portfolio."""
        self.positions.append(pos)

    def remove_position(self, pos_id: int):
        """Remove a position by ID."""
        self.positions = [p for p in self.positions if p.id != pos_id]

    def get_position_count(self) -> int:
        """Get current number of open positions."""
        return len(self.positions)

    def has_same_side_position(self, side: str) -> bool:
        """Check if we already have a position on the same side."""
        return any(p.side == side for p in self.positions)


# ----------------------------
# Main Trading Loop
# ----------------------------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "btc-mean-reversion/1.0"})

    prices = deque(maxlen=BOLLINGER_WINDOW)

    # Seed with first tick
    try:
        r = session.get(TICKER_URL, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
        mid_px = float(r.json()["price"])
    except Exception as e:
        print(f"Error fetching initial price: {e}")
        return

    t0 = time.time()
    prices.append(mid_px)

    pf = Portfolio(cash_usd=START_CASH_USD, positions=[])
    last_print = time.time()

    print(f"{now_str()} START | cash=${pf.cash_usd:.2f} | max_positions={MAX_POSITIONS} | exec_cost/side≈{exec_cost_fraction()*100:.3f}%")

    while True:
        loop_start = time.time()

        # Poll current market price
        try:
            r = session.get(TICKER_URL, timeout=SESSION_TIMEOUT)
            r.raise_for_status()
            mid_px = float(r.json()["price"])
        except Exception as e:
            print(f"{now_str()} Error fetching price: {e}")
            time.sleep(POLL_EVERY)
            continue

        now = time.time()
        prices.append(mid_px)

        # Calculate Bollinger Bands
        mean = moving_average(prices, BOLLINGER_WINDOW)
        std = std_dev(prices, BOLLINGER_WINDOW)

        if mean is None or std is None:
            if now - last_print >= PRINT_EVERY:
                print(f"{now_str()} Waiting for enough data ({len(prices)}/{BOLLINGER_WINDOW})...")
                last_print = now
            time.sleep(POLL_EVERY)
            continue

        upper_band = mean + (std * BOLLINGER_STD_DEV)
        lower_band = mean - (std * BOLLINGER_STD_DEV)

        # Entry logic (paper only; no orders sent anywhere)
        # Only enter if we have room for more positions
        if pf.get_position_count() < MAX_POSITIONS:
            # Buy when price is below lower band (mean reversion long)
            if mid_px < lower_band and not pf.has_same_side_position("long"):
                # For a long: we buy at current price (with slippage we pay more)
                fill_px = fill_price(mid_px, "buy")  # Price we actually pay
                qty = NOTIONAL_PER_TRADE_USD / fill_px  # Size based on what we can afford
                
                # Stop loss: if price goes DOWN too much, we lose
                stop_distance = max(STOP_K_RANGE * std, mid_px * 0.02)
                stop_px = mid_px - stop_distance  # Stop is BELOW entry for longs
                target_px = mean  # take profit at mean (price goes UP)
                
                # When going long, we PAY cash
                pf.cash_usd -= qty * fill_px
                new_pos = Position(pf.next_pos_id, "long", qty, mid_px, stop_px, target_px, now)
                pf.add_position(new_pos)
                pf.next_pos_id += 1
                
                print(
                    f"{now_str()} PAPER ENTER LONG #{new_pos.id} | px={mid_px:.2f} filled={fill_px:.2f} stop={stop_px:.2f} "
                    f"target={target_px:.2f} | BB=[{lower_band:.2f}, {mean:.2f}, {upper_band:.2f}] | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                )

            # Sell (short) when price is above upper band (mean reversion short)
            elif mid_px > upper_band and not pf.has_same_side_position("short"):
                # For a short: we sell at current price (with slippage we receive less cash)
                fill_px = fill_price(mid_px, "sell")  # Cash we receive
                qty = NOTIONAL_PER_TRADE_USD / mid_px  # Size based on market price
                
                # Stop loss: if price goes UP too much, we lose
                stop_distance = max(STOP_K_RANGE * std, mid_px * 0.02)
                stop_px = mid_px + stop_distance  # Stop is ABOVE entry for shorts
                target_px = mean  # take profit at mean (price goes DOWN)
                
                # When shorting, we RECEIVE cash (we're selling BTC we don't own)
                pf.cash_usd += qty * fill_px  
                new_pos = Position(pf.next_pos_id, "short", qty, mid_px, stop_px, target_px, now)
                pf.add_position(new_pos)
                pf.next_pos_id += 1
                
                print(
                    f"{now_str()} PAPER ENTER SHORT #{new_pos.id} | px={mid_px:.2f} filled={fill_px:.2f} stop={stop_px:.2f} "
                    f"target={target_px:.2f} | BB=[{lower_band:.2f}, {mean:.2f}, {upper_band:.2f}] | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                )

        # Manage open positions (paper fills only)
        # Use list copy to avoid modification during iteration
        for p in list(pf.positions):
            if p.side == "long":
                if mid_px <= p.stop_px:  # stop-loss hit
                    exit_px = fill_price(mid_px, "sell")
                    pnl = (exit_px - p.entry_px) * p.qty_btc
                    pf.cash_usd += p.qty_btc * exit_px
                    pf.remove_position(p.id)
                    print(
                        f"{now_str()} PAPER STOP LONG #{p.id} | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f} | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                    )

                elif mid_px >= p.target_px:  # take-profit hit
                    exit_px = fill_price(mid_px, "sell")
                    pnl = (exit_px - p.entry_px) * p.qty_btc
                    pf.cash_usd += p.qty_btc * exit_px
                    pf.remove_position(p.id)
                    print(
                        f"{now_str()} PAPER TP LONG #{p.id} | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f} | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                    )

            else:  # short
                if mid_px >= p.stop_px:  # stop-loss hit
                    exit_px = fill_price(mid_px, "buy")
                    pnl = (p.entry_px - exit_px) * p.qty_btc
                    pf.cash_usd -= p.qty_btc * exit_px
                    pf.remove_position(p.id)
                    print(
                        f"{now_str()} PAPER STOP SHORT #{p.id} | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f} | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                    )

                elif mid_px <= p.target_px:  # take-profit hit
                    exit_px = fill_price(mid_px, "buy")
                    pnl = (p.entry_px - exit_px) * p.qty_btc
                    pf.cash_usd -= p.qty_btc * exit_px
                    pf.remove_position(p.id)
                    print(
                        f"{now_str()} PAPER TP SHORT #{p.id} | entry={p.entry_px:.2f} exit={exit_px:.2f} pnl=${pnl:.2f} | positions={pf.get_position_count()}/{MAX_POSITIONS}"
                    )

        # Print portfolio status every PRINT_EVERY seconds
        if now - last_print >= PRINT_EVERY:
            pv = pf.mark_to_market(mid_px)
            pnl_total = pv - START_CASH_USD
            
            if pf.get_position_count() == 0:
                pos_txt = "FLAT"
            else:
                pos_summaries = []
                for p in pf.positions:
                    upnl = (
                        p.qty_btc * (mid_px - p.entry_px)
                        if p.side == "long"
                        else p.qty_btc * (p.entry_px - mid_px)
                    )
                    pos_summaries.append(
                        f"#{p.id}:{p.side.upper()}@{p.entry_px:.2f}(uPnL=${upnl:.2f})"
                    )
                pos_txt = " | ".join(pos_summaries)
            
            print(
                f"{now_str()} | px={mid_px:.2f} | "
                f"BB: [{lower_band:.2f}, {mean:.2f}, {upper_band:.2f}] | "
                f"positions={pf.get_position_count()}/{MAX_POSITIONS}: {pos_txt} | "
                f"cash=${pf.cash_usd:.2f} PV=${pv:.2f} totalPnL=${pnl_total:.2f}"
            )
            last_print = now

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, POLL_EVERY - elapsed))


if __name__ == "__main__":
    main()