import asyncio
import websockets
import json

COINBASE_WEBSOCKETS = 'wss://advanced-trade-ws.coinbase.com'
products = ["BTC-USD"]

async def main():

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

                    print(f"\rBTC-USD: {price} ", end="", flush=True)

asyncio.run(main())
