import time
import requests
from collections import deque
import numpy as np
from dataclasses import dataclass
from typing import List, Optional

# ----------------------------
# Config
# ----------------------------
PRODUCT_ID = "BTC-USD"
TICKER_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"

POLL_EVERY = 1.0
SESSION_TIMEOUT = 5

# Mean reversion parameters
LOOKBACK_WINDOW = 60  # Window for calculating mean and std
UPDATE_PARAMS_EVERY = 20  # Re-estimate parameters every N ticks

# Trading thresholds (z-score based)
ENTRY_THRESHOLD = 2.5  # Enter when price deviates by this many std devs (wait for bigger moves)
EXIT_THRESHOLD = 0.2   # Exit when price reverts close to mean (take profit quickly)

# Portfolio
START_CASH_USD = 10_000.0
NOTIONAL_PER_TRADE_USD = 1_000.0
MAX_POSITIONS = 3

# Logging
PRINT_EVERY = 10.0

# Execution costs
EXEC_COST_FRACTION = 0.006  # 0.6% per side


# ----------------------------
# Mean Reversion Model
# ----------------------------
class MeanReversionModel:
    """
    Simple but robust mean reversion model using rolling statistics.
    Z-score = (price - mean) / std_dev
    """
    def __init__(self):
        self.mean: Optional[float] = None
        self.std: Optional[float] = None
        self.half_life: Optional[float] = None
        
    def update(self, prices: deque) -> bool:
        """Update mean and std from recent prices."""
        if len(prices) < 30:
            return False
            
        prices_array = np.array(prices)
        
        # Calculate rolling mean and std
        self.mean = np.mean(prices_array)
        self.std = np.std(prices_array, ddof=1)
        
        # Estimate half-life using AR(1) on prices (not log prices)
        if len(prices_array) >= 40:
            try:
                X = prices_array[:-1]
                Y = prices_array[1:]
                
                # Simple linear regression: Y = a + b*X
                n = len(X)
                sum_x = np.sum(X)
                sum_y = np.sum(Y)
                sum_xy = np.sum(X * Y)
                sum_xx = np.sum(X * X)
                
                b = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x)
                
                # Half-life from AR(1) coefficient
                if 0 < b < 1:
                    theta = -np.log(b)
                    self.half_life = np.log(2) / theta if theta > 0 else float('inf')
                else:
                    self.half_life = None
            except:
                self.half_life = None
        
        return True
    
    def get_z_score(self, current_price: float) -> Optional[float]:
        """
        Calculate z-score (how many std devs from mean).
        Positive z-score = overbought (price above mean)
        Negative z-score = oversold (price below mean)
        """
        if self.mean is None or self.std is None or self.std == 0:
            return None
        
        z_score = (current_price - self.mean) / self.std
        return z_score


# ----------------------------
# Position & Portfolio
# ----------------------------
@dataclass
class Position:
    id: int
    side: str
    qty_btc: float
    entry_px: float
    entry_z_score: float
    entry_time: float


@dataclass
class Portfolio:
    cash_usd: float
    positions: List[Position]
    next_pos_id: int = 0

    def mark_to_market(self, mid_px: float) -> float:
        total = self.cash_usd
        for pos in self.positions:
            if pos.side == "long":
                total += pos.qty_btc * mid_px
            else:
                total -= pos.qty_btc * mid_px
        return total


# ----------------------------
# Helpers
# ----------------------------
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fill_price(mid: float, side: str) -> float:
    if side == "buy":
        return mid * (1.0 + EXEC_COST_FRACTION)
    elif side == "sell":
        return mid * (1.0 - EXEC_COST_FRACTION)
    raise ValueError("side must be 'buy' or 'sell'")


# ----------------------------
# Main Trading Loop
# ----------------------------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "btc-zscore-mean-reversion/1.0"})

    prices = deque(maxlen=LOOKBACK_WINDOW)
    model = MeanReversionModel()
    
    # Seed with first tick
    try:
        r = session.get(TICKER_URL, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
        mid_px = float(r.json()["price"])
    except Exception as e:
        print(f"Error fetching initial price: {e}")
        return

    prices.append(mid_px)
    pf = Portfolio(cash_usd=START_CASH_USD, positions=[])
    last_print = time.time()
    tick_count = 0

    print(f"{now_str()} START | cash=${pf.cash_usd:.2f} | Strategy: Z-Score Mean Reversion")
    print(f"Entry threshold: {ENTRY_THRESHOLD} σ | Exit threshold: {EXIT_THRESHOLD} σ | Window: {LOOKBACK_WINDOW}")
    print(f"Position size: ${NOTIONAL_PER_TRADE_USD:.0f} | Slippage: {EXEC_COST_FRACTION*100:.1f}% per side")

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
        tick_count += 1

        # Update model parameters periodically
        if tick_count % UPDATE_PARAMS_EVERY == 0 or model.mean is None:
            if model.update(prices):
                hl_str = f"{model.half_life:.1f}" if model.half_life else "N/A"
                print(f"\n{now_str()} Parameters Updated:")
                print(f"  Mean = ${model.mean:.2f}")
                print(f"  Std Dev = ${model.std:.2f}")
                print(f"  Half-life = {hl_str} ticks\n")

        # Calculate z-score
        z_score = model.get_z_score(mid_px)
        
        if z_score is None:
            if now - last_print >= PRINT_EVERY:
                print(f"{now_str()} Waiting for parameter estimation... ({len(prices)}/{LOOKBACK_WINDOW} ticks)")
                last_print = now
            time.sleep(POLL_EVERY)
            continue

        # Entry logic
        if len(pf.positions) < MAX_POSITIONS:
            # Enter SHORT when price is too high (overbought)
            if z_score > ENTRY_THRESHOLD:
                has_short = any(p.side == "short" for p in pf.positions)
                if not has_short:
                    fill_px = fill_price(mid_px, "sell")
                    qty = NOTIONAL_PER_TRADE_USD / fill_px
                    
                    pf.cash_usd += qty * fill_px
                    new_pos = Position(pf.next_pos_id, "short", qty, fill_px, z_score, now)
                    pf.positions.append(new_pos)
                    pf.next_pos_id += 1
                    
                    print(f"{now_str()} ENTER SHORT #{new_pos.id} | px=${mid_px:.2f} z={z_score:.2f} "
                          f"mean=${model.mean:.2f} std=${model.std:.2f} | pos={len(pf.positions)}/{MAX_POSITIONS}")

            # Enter LONG when price is too low (oversold)
            elif z_score < -ENTRY_THRESHOLD:
                has_long = any(p.side == "long" for p in pf.positions)
                if not has_long:
                    fill_px = fill_price(mid_px, "buy")
                    qty = NOTIONAL_PER_TRADE_USD / fill_px
                    
                    pf.cash_usd -= qty * fill_px
                    new_pos = Position(pf.next_pos_id, "long", qty, fill_px, z_score, now)
                    pf.positions.append(new_pos)
                    pf.next_pos_id += 1
                    
                    print(f"{now_str()} ENTER LONG #{new_pos.id} | px=${mid_px:.2f} z={z_score:.2f} "
                          f"mean=${model.mean:.2f} std=${model.std:.2f} | pos={len(pf.positions)}/{MAX_POSITIONS}")

        # Exit logic
        for pos in list(pf.positions):
            should_exit = False
            exit_reason = ""
            
            if pos.side == "long":
                # Exit long when price has reverted (z-score crosses exit threshold)
                if z_score > -EXIT_THRESHOLD:
                    should_exit = True
                    exit_reason = "mean reversion"
                # Stop loss: if z-score gets even more negative (2 sigma worse)
                elif z_score < pos.entry_z_score - 2.0:
                    should_exit = True
                    exit_reason = "stop loss"
            else:  # short
                # Exit short when price has reverted
                if z_score < EXIT_THRESHOLD:
                    should_exit = True
                    exit_reason = "mean reversion"
                # Stop loss: if z-score gets even more positive (2 sigma worse)
                elif z_score > pos.entry_z_score + 2.0:
                    should_exit = True
                    exit_reason = "stop loss"
            
            if should_exit:
                if pos.side == "long":
                    exit_px = fill_price(mid_px, "sell")
                    pnl = (exit_px - pos.entry_px) * pos.qty_btc
                    pf.cash_usd += pos.qty_btc * exit_px
                else:
                    exit_px = fill_price(mid_px, "buy")
                    pnl = (pos.entry_px - exit_px) * pos.qty_btc
                    pf.cash_usd -= pos.qty_btc * exit_px
                
                pf.positions.remove(pos)
                print(f"{now_str()} EXIT {pos.side.upper()} #{pos.id} ({exit_reason}) | "
                      f"entry=${pos.entry_px:.2f} exit=${exit_px:.2f} pnl=${pnl:.2f} | "
                      f"z_entry={pos.entry_z_score:.2f} z_exit={z_score:.2f}")

        # Print portfolio status
        if now - last_print >= PRINT_EVERY:
            pv = pf.mark_to_market(mid_px)
            total_pnl = pv - START_CASH_USD
            
            if len(pf.positions) == 0:
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
                f"{now_str()} | px=${mid_px:.2f} mean=${model.mean:.2f} z={z_score:.2f} | "
                f"pos={len(pf.positions)}/{MAX_POSITIONS}: {pos_txt} | "
                f"cash=${pf.cash_usd:.2f} PV=${pv:.2f} PnL=${total_pnl:.2f}"
            )
            last_print = now

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, POLL_EVERY - elapsed))


if __name__ == "__main__":
    main()