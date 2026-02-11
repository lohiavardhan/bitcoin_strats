import asyncio
import json
import websockets
import numpy as np
import time
from collections import deque
from dataclasses import dataclass
from typing import List

# --- Config ---
WS_URL = "wss://ws-feed.exchange.coinbase.com"
PRODUCT_ID = "BTC-USD"

# ZOOMED OUT WINDOWS: Looking for moves that actually pay for fees
FAST_WINDOW = 50  
SLOW_WINDOW = 200 

NOTIONAL_PER_TRADE = 1000.0 
MAX_POSITIONS = 1      # Focus on one quality trend at a time
FEE_RATE = 0.001       # 0.1% Coinbase Fee
STOP_MULT = 2.5        # Slightly wider stop to avoid "noise" exits

@dataclass
class Position:
    side: str
    qty: float
    entry_px: float
    stop_px: float
    be_px: float       # Breakeven Price (includes fees)

class PaperExchange:
    def __init__(self, cash=10000.0):
        self.cash = cash
        self.total_realized_pnl = 0.0
        self.positions: List[Position] = []

    def get_equity(self, current_price):
        unrealized = 0.0
        for p in self.positions:
            if p.side == "long":
                unrealized += (current_price - p.entry_px) * p.qty
            else:
                unrealized += (p.entry_px - current_price) * p.qty
        return self.cash + unrealized

def calculate_ema(prices, window):
    if len(prices) < window: return None
    prices_arr = np.array(prices)
    alpha = 2 / (window + 1)
    ema = prices_arr[0]
    for price in prices_arr[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

class AsyncTrendBot:
    def __init__(self):
        self.prices = deque(maxlen=SLOW_WINDOW + 20)
        self.current_price = None
        self.exchange = PaperExchange()
        self.is_running = True

    async def socket_listener(self):
        msg = {"type": "subscribe", "product_ids": [PRODUCT_ID], "channels": ["ticker"]}
        while self.is_running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps(msg))
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("type") == "ticker":
                            self.current_price = float(data["price"])
                            self.prices.append(self.current_price)
            except Exception:
                await asyncio.sleep(5)

    async def strategy_manager(self):
        print(f"Bot Active | Fee-Shield ON | Windows: {FAST_WINDOW}/{SLOW_WINDOW}")
        
        while self.is_running:
            if len(self.prices) < SLOW_WINDOW or not self.current_price:
                await asyncio.sleep(1)
                continue

            px = self.current_price
            price_list = list(self.prices)
            fast_ema = calculate_ema(price_list[-FAST_WINDOW:], FAST_WINDOW)
            slow_ema = calculate_ema(price_list[-SLOW_WINDOW:], SLOW_WINDOW)
            std = np.std(price_list[-FAST_WINDOW:])

            # 1. MANAGE EXITS
            for p in self.exchange.positions[:]:
                fees = (NOTIONAL_PER_TRADE * FEE_RATE) * 2 
                closed = False
                
                if p.side == "long":
                    # Trail stop 2.0 StdDevs behind
                    new_stop = px - (std * 2.0)
                    if new_stop > p.stop_px: p.stop_px = new_stop
                    
                    # Exit if stop hit OR trend flips AND we have at least partial fee recovery
                    if px <= p.stop_px or (fast_ema < slow_ema and px > p.entry_px):
                        pnl = ((px - p.entry_px) * p.qty) - fees
                        closed = True

                elif p.side == "short":
                    new_stop = px + (std * 2.0)
                    if new_stop < p.stop_px: p.stop_px = new_stop
                        
                    if px >= p.stop_px or (fast_ema > slow_ema and px < p.entry_px):
                        pnl = ((p.entry_px - px) * p.qty) - fees
                        closed = True

                if closed:
                    self.exchange.cash += pnl
                    self.exchange.total_realized_pnl += pnl
                    self.exchange.positions.remove(p)
                    print(f"\n--- EXIT {p.side.upper()} | Net: ${pnl:.2f} | Total: ${self.exchange.total_realized_pnl:.2f}")

            # 2. ENTRY LOGIC with FEE SHIELD
            if len(self.exchange.positions) < MAX_POSITIONS:
                fee_gap = px * (FEE_RATE * 2.5) # The distance BTC must move to be "worth it"
                
                # Golden Cross + Check if trend is strong enough to outpace fees
                if fast_ema > slow_ema and (fast_ema - slow_ema) > (fee_gap * 0.5):
                    if not any(p.side == "long" for p in self.exchange.positions):
                        stop = px - (std * STOP_MULT)
                        be_price = px + (px * FEE_RATE * 2) # Entry price + round trip fee cost
                        self.exchange.positions.append(Position("long", NOTIONAL_PER_TRADE/px, px, stop, be_price))
                        print(f"\n+++ LONG @ {px:.2f} | Need > {be_price:.2f} to profit")

                # Death Cross
                elif fast_ema < slow_ema and (slow_ema - fast_ema) > (fee_gap * 0.5):
                    if not any(p.side == "short" for p in self.exchange.positions):
                        stop = px + (std * STOP_MULT)
                        be_price = px - (px * FEE_RATE * 2)
                        self.exchange.positions.append(Position("short", NOTIONAL_PER_TRADE/px, px, stop, be_price))
                        print(f"\n+++ SHORT @ {px:.2f} | Need < {be_price:.2f} to profit")

            await asyncio.sleep(0.5)

    async def logger(self):
        while self.is_running:
            if self.current_price and len(self.prices) >= SLOW_WINDOW:
                eq = self.exchange.get_equity(self.current_price)
                be_info = ""
                if self.exchange.positions:
                    p = self.exchange.positions[0]
                    be_info = f" | BE: ${p.be_px:.2f}"
                
                print(f"BTC: ${self.current_price:.2f} | Equity: ${eq:.2f} | Realized: ${self.exchange.total_realized_pnl:.2f}{be_info}", end='\r')
            await asyncio.sleep(1)

    async def run(self):
        await asyncio.gather(self.socket_listener(), self.strategy_manager(), self.logger())

if __name__ == "__main__":
    bot = AsyncTrendBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.is_running = False
        print("\nBot Stopped.")