import asyncio
import websockets
import json

COINBASE_WEBSOCKETS = 'wss://advanced-trade-ws.coinbase.com'
products = ["BTC-USD"]

async def btc_pricer(queue: asyncio.Queue):

    async with websockets.connect(COINBASE_WEBSOCKETS) as websocket:

        subscribe_message = {
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "ticker"
        }

        await websocket.send(json.dumps(subscribe_message))

        while True:
            message = json.loads(await websocket.recv())

            for event in message.get("events", []):
                for ticker in event.get("tickers", []):
                    price = ticker.get('price')
                    if price is None:
                        continue
                    await queue.put(float(price))


async def client(queue: asyncio.Queue):
    while True:
        price = await queue.get()
        print(f"\r BTC-USD: {price}", end="", flush=True)
        queue.task_done()

async def main():
    q = asyncio.Queue()
    await asyncio.gather(btc_pricer(q), client(q), return_exceptions=False)

asyncio.run(main())
