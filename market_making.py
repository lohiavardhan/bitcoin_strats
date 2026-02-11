import time
import requests
from collections import deque
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Dict

# ----------------------------
# Config
# ----------------------------
PRODUCT_ID = "BTC-USD"
TICKER_URL = f"https://api.exchange.coinbase.com/products/{PRODUCT_ID}/ticker"

POLL_EVERY = 1.0
SESSION_TIMEOUT = 5

# Mean reversion parameters
LOOKBACK_WINDOW = 60
UPDATE_PARAMS_EVERY = 20

# Limit order placement (place orders at these z-score levels)
LONG_ENTRY_Z = -1.5   # Adjusted z-score for long entry
SHORT_ENTRY_Z = 1.5   # Adjusted z-score for short entry

# Exit parameters (from entry price, not z-score)
TAKE_PROFIT_Z = 0.3   # Take profit when price reverts this close to mean
STOP_LOSS_SIGMA = 2.0 # Adjusted stop loss (2 sigma for tighter control)

# Portfolio
START_CASH_USD = 10_000.0
NOTIONAL_PERCENTAGE_PER_ORDER = 0.1  # Trade 10% of portfolio value per order
MAX_ORDERS = 6  # Maximum of 3 longs + 3 shorts

# Fees
MAKER_FEE = 0.0035  # 0.35% maker fee
TAKER_FEE = 0.006   # 0.6% taker fee (for stop losses)

# Logging
PRINT_EVERY = 10.0

# ----------------------------
# Mean Reversion Model
# ----------------------------
class MeanReversionModel:
    def __init__(self):
        self.mean: Optional[float] = None
        self.std: Optional[float] = None
        
    def update(self, prices: deque) -> bool:
        if len(prices) < 30:
            return False
            
        prices_array = np.array(prices)
        self.mean = np.mean(prices_array)
        self.std = np.std(prices_array, ddof=1)
        return True
    
    def get_z_score(self, price: float) -> Optional[float]:
        if self.mean is None or self.std is None or self.std == 0:
            return None
        return (price - self.mean) / self.std
    
    def price_at_z(self, z: float) -> Optional[float]:
        """Calculate what price corresponds to a given z-score."""
        if self.mean is None or self.std is None:
            return None
        return self.mean + (z * self.std)


# ----------------------------
# Limit Order
# ----------------------------
@dataclass
class LimitOrder:
    """Represents a pending limit order with TP/SL."""
    id: int
    side: str  # 'long' or 'short'
    entry_price: float
    quantity: float
    take_profit_price: float
    stop_loss_price: float
    placed_time: float
    filled: bool = False
    filled_time: Optional[float] = None


# ----------------------------
# Position (filled order)
# ----------------------------
@dataclass
class Position:
    id: int
    side: str
    qty_btc: float
    entry_px: float
    take_profit_px: float
    stop_loss_px: float
    entry_time: float


# ----------------------------
# Portfolio
# ----------------------------
@dataclass
class Portfolio:
    cash_usd: float
    orders: List[LimitOrder]  # Pending limit orders
    positions: List[Position]  # Filled positions
    next_order_id: int = 0

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


# ----------------------------
# Main Trading Loop
# ----------------------------
def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "btc-limit-order-mr/1.0"})

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
    pf = Portfolio(cash_usd=START_CASH_USD, orders=[], positions=[])
    last_print = time.time()
    tick_count = 0

    print(f"{now_str()} START | cash=${pf.cash_usd:.2f}")
    print(f"Strategy: Limit Order Mean Reversion")
    print(f"Entry: Long@{LONG_ENTRY_Z}σ, Short@{SHORT_ENTRY_Z}σ | TP: {TAKE_PROFIT_Z}σ | SL: {STOP_LOSS_SIGMA}σ")
    print(f"Maker fee: {MAKER_FEE*100:.2f}% | Taker fee: {TAKER_FEE*100:.2f}%\n")

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

        # Update model parameters
        if tick_count % UPDATE_PARAMS_EVERY == 0 or model.mean is None:
            if model.update(prices):
                print(f"{now_str()} Parameters: mean=${model.mean:.2f} std=${model.std:.2f}")

        z_score = model.get_z_score(mid_px)
        
        if z_score is None:
            if now - last_print >= PRINT_EVERY:
                print(f"{now_str()} Waiting... ({len(prices)}/{LOOKBACK_WINDOW} ticks)")
                last_print = now
            time.sleep(POLL_EVERY)
            continue

        # Check if any limit orders got filled
        for order in list(pf.orders):
            filled = False
            
            if order.side == "long" and mid_px <= order.entry_price:
                # Long limit order filled (price dropped to our buy level)
                filled = True
                fill_price = order.entry_price * (1 + MAKER_FEE)  # Pay maker fee
                
            elif order.side == "short" and mid_px >= order.entry_price:
                # Short limit order filled (price rose to our sell level)
                filled = True
                fill_price = order.entry_price * (1 - MAKER_FEE)  # Receive less due to fee
            
            if filled:
                order.filled = True
                order.filled_time = now
                
                # Convert to position
                if order.side == "long":
                    pf.cash_usd -= order.quantity * fill_price
                else:
                    pf.cash_usd += order.quantity * fill_price
                
                pos = Position(
                    order.id,
                    order.side,
                    order.quantity,
                    fill_price,
                    order.take_profit_price,
                    order.stop_loss_price,
                    now
                )
                pf.positions.append(pos)
                pf.orders.remove(order)
                
                print(f"{now_str()} ✓ FILLED {order.side.upper()} #{order.id} | "
                      f"entry=${fill_price:.2f} TP=${order.take_profit_price:.2f} SL=${order.stop_loss_price:.2f}")

        # Manage positions (check TP/SL)
        for pos in list(pf.positions):
            should_exit = False
            exit_reason = ""
            
            if pos.side == "long":
                if mid_px >= pos.take_profit_px:
                    should_exit = True
                    exit_reason = "TP"
                    exit_price = pos.take_profit_px * (1 - MAKER_FEE)  # Maker fee on TP
                elif mid_px <= pos.stop_loss_px:
                    should_exit = True
                    exit_reason = "SL"
                    exit_price = pos.stop_loss_px * (1 - TAKER_FEE)  # Taker fee on SL
            else:  # short
                if mid_px <= pos.take_profit_px:
                    should_exit = True
                    exit_reason = "TP"
                    exit_price = pos.take_profit_px * (1 + MAKER_FEE)  # Maker fee on TP
                elif mid_px >= pos.stop_loss_px:
                    should_exit = True
                    exit_reason = "SL"
                    exit_price = pos.stop_loss_px * (1 + TAKER_FEE)  # Taker fee on SL
            
            if should_exit:
                if pos.side == "long":
                    pnl = (exit_price - pos.entry_px) * pos.qty_btc
                    pf.cash_usd += pos.qty_btc * exit_price
                else:
                    pnl = (pos.entry_px - exit_price) * pos.qty_btc
                    pf.cash_usd -= pos.qty_btc * exit_price
                
                pf.positions.remove(pos)
                print(f"{now_str()} ✗ EXIT {pos.side.upper()} #{pos.id} ({exit_reason}) | "
                      f"entry=${pos.entry_px:.2f} exit=${exit_price:.2f} pnl=${pnl:.2f}")

        # Place new limit orders if we have capacity
        total_orders = len(pf.orders) + len(pf.positions)
        
        if total_orders < MAX_ORDERS:
            # Place LONG limit order (buy below current price)
            long_entry_price = model.price_at_z(LONG_ENTRY_Z)
            if long_entry_price and long_entry_price < mid_px:
                # Calculate dynamic TP/SL prices based on volatility
                tp_price = model.price_at_z(TAKE_PROFIT_Z)
                sl_price = long_entry_price - (STOP_LOSS_SIGMA * model.std)
                
                qty = pf.cash_usd * NOTIONAL_PERCENTAGE_PER_ORDER / long_entry_price
                
                order = LimitOrder(
                    pf.next_order_id,
                    "long",
                    long_entry_price,
                    qty,
                    tp_price,
                    sl_price,
                    now
                )
                pf.orders.append(order)
                pf.next_order_id += 1
                
                print(f"{now_str()} → PLACE LONG #{order.id} | "
                      f"entry=${long_entry_price:.2f} (z={LONG_ENTRY_Z}) "
                      f"TP=${tp_price:.2f} SL=${sl_price:.2f}")
            
            # Place SHORT limit order (sell above current price)
            short_entry_price = model.price_at_z(SHORT_ENTRY_Z)
            if short_entry_price and short_entry_price > mid_px:
                # Calculate dynamic TP/SL prices based on volatility
                tp_price = model.price_at_z(TAKE_PROFIT_Z)
                sl_price = short_entry_price + (STOP_LOSS_SIGMA * model.std)
                
                qty = pf.cash_usd * NOTIONAL_PERCENTAGE_PER_ORDER / short_entry_price
                
                order = LimitOrder(
                    pf.next_order_id,
                    "short",
                    short_entry_price,
                    qty,
                    tp_price,
                    sl_price,
                    now
                )
                pf.orders.append(order)
                pf.next_order_id += 1
                
                print(f"{now_str()} → PLACE SHORT #{order.id} | "
                      f"entry=${short_entry_price:.2f} (z={SHORT_ENTRY_Z}) "
                      f"TP=${tp_price:.2f} SL=${sl_price:.2f}")

        # Print portfolio status
        if now - last_print >= PRINT_EVERY:
            pv = pf.mark_to_market(mid_px)
            total_pnl = pv - START_CASH_USD
            
            pending_txt = f"{len(pf.orders)} pending"
            if len(pf.positions) == 0:
                pos_txt = "no positions"
            else:
                pos_summaries = []
                for p in pf.positions:
                    upnl = (
                        p.qty_btc * (mid_px - p.entry_px)
                        if p.side == "long"
                        else p.qty_btc * (p.entry_px - mid_px)
                    )
                    pos_summaries.append(f"#{p.id}:{p.side[0].upper()}(${upnl:.0f})")
                pos_txt = " ".join(pos_summaries)
            
            print(
                f"{now_str()} | px=${mid_px:.2f} z={z_score:.2f} | "
                f"{pending_txt}, {pos_txt} | "
                f"PV=${pv:.2f} PnL=${total_pnl:.2f}"
            )
            last_print = now

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, POLL_EVERY - elapsed))


if __name__ == "__main__":
    main()
