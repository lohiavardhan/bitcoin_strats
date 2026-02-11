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
BOLLINGER_WINDOW = 50  # Increased for more price history stability
STD_DEV_MULT = 3.0     # Increased: only trade during significant "over-extensions"
NOTIONAL_PER_TRADE = 1000.0 
MAX_POSITIONS = 3
FEE_RATE = 0.001       # 0.1% per side (total 0.2% round trip)
MIN_PROFIT_RATIO = 1.5 # The target profit must be 1.5x the cost of fees to enter

@dataclass
class Position:
    side: str
    qty: float
    entry_px: float
    stop_px: float
    target_px: float

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

class AsyncPaperBot:
    def __init__(self):
        self.prices = deque(maxlen=BOLLINGER_WINDOW)
        self.current_price = None
        self.exchange = PaperExchange()
        self.is_running = True

    async def socket_listener(self):
        subscribe_msg = {"type": "subscribe", "product_ids": [PRODUCT_ID], "channels": ["ticker"]}
        while self.is_running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("type") == "ticker":
                            self.current_price = float(data["price"])
                            self.prices.append(self.current_price)
            except:
                await asyncio.sleep(5)

    async def strategy_manager(self):
        print(f"Simulator Active. Fees: {FEE_RATE*200:.2f}% round-trip.")
        
        while self.is_running:
            if len(self.prices) < BOLLINGER_WINDOW or not self.current_price:
                await asyncio.sleep(1)
                continue

            px = self.current_price
            mean = np.mean(self.prices)
            std = np.std(self.prices)
            lower = mean - (std * STD_DEV_MULT)
            upper = mean + (std * STD_DEV_MULT)

            # 1. MANAGE EXITS
            for p in self.exchange.positions[:]:
                pnl = 0.0
                closed = False
                # Round trip fees based on notional value
                fees = (NOTIONAL_PER_TRADE * FEE_RATE) * 2 
                
                if p.side == "long" and (px <= p.stop_px or px >= p.target_px):
                    pnl = ((px - p.entry_px) * p.qty) - fees
                    closed = True
                elif p.side == "short" and (px >= p.stop_px or px <= p.target_px):
                    pnl = ((p.entry_px - px) * p.qty) - fees
                    closed = True

                if closed:
                    self.exchange.cash += pnl
                    self.exchange.total_realized_pnl += pnl
                    self.exchange.positions.remove(p)
                    print(f"\n--- EXIT {p.side.upper()} | Net PnL: ${pnl:.2f} | Total: ${self.exchange.total_realized_pnl:.2f}")

            # 2. CHECK FOR ENTRY
            if len(self.exchange.positions) < MAX_POSITIONS:
                # Fee Trap Guard:
                fees_to_cover = (NOTIONAL_PER_TRADE * FEE_RATE) * 2
                expected_gross_profit = abs(px - mean) * (NOTIONAL_PER_TRADE / px)

                if expected_gross_profit > (fees_to_cover * MIN_PROFIT_RATIO):
                    if px < lower:
                        new_pos = Position("long", NOTIONAL_PER_TRADE/px, px, px-(std*2), mean)
                        self.exchange.positions.append(new_pos)
                        print(f"\n+++ ENTER LONG @ {px:.2f} | Target: {mean:.2f} | Est. Profit: ${expected_gross_profit:.2f}")

                    elif px > upper:
                        new_pos = Position("short", NOTIONAL_PER_TRADE/px, px, px+(std*2), mean)
                        self.exchange.positions.append(new_pos)
                        print(f"\n+++ ENTER SHORT @ {px:.2f} | Target: {mean:.2f} | Est. Profit: ${expected_gross_profit:.2f}")

            await asyncio.sleep(0.1)

    async def logger(self):
        while self.is_running:
            if self.current_price and len(self.prices) >= BOLLINGER_WINDOW:
                eq = self.exchange.get_equity(self.current_price)
                print(f"BTC: ${self.current_price:.2f} | Equity: ${eq:.2f} | Active: {len(self.exchange.positions)}", end='\r')
            await asyncio.sleep(0.5)

    async def run(self):
        await asyncio.gather(self.socket_listener(), self.strategy_manager(), self.logger())

if __name__ == "__main__":
    bot = AsyncPaperBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.is_running = False
        print("\nSimulation Terminated.")