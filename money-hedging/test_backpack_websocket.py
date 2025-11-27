#!/usr/bin/env python3
"""
Simple test to verify Backpack WebSocket connectivity
"""

import asyncio
import json
import websockets
from bpx.public import Public


async def test_backpack_websocket():
    """Test Backpack WebSocket connection and subscription"""
    print("🔌 Testing Backpack WebSocket connectivity...")

    try:
        # First get markets to find the right symbol
        public_client = Public()
        markets = public_client.get_markets()

        eth_symbol = None
        for market in markets:
            if (market.get('marketType', '') == 'PERP' and
                market.get('baseSymbol', '') == 'ETH' and
                market.get('quoteSymbol', '') == 'USDC'):
                eth_symbol = market.get('symbol', '')
                break

        print(f"🔌 Found ETH symbol: {eth_symbol}")

        if not eth_symbol:
            print("❌ No ETH symbol found!")
            return

        # Test WebSocket connection
        ws_url = "wss://ws.backpack.exchange"
        print(f"🔌 Connecting to {ws_url}...")

        async with websockets.connect(ws_url) as websocket:
            print("🔌 ✅ WebSocket connected successfully!")

            # Subscribe to trade data
            subscribe_message = {
                "method": "SUBSCRIBE",
                "params": [f"trade.{eth_symbol}"]
            }

            print(f"🔌 Subscribing to trade.{eth_symbol}")
            print(f"🔌 Subscription message: {json.dumps(subscribe_message)}")

            await websocket.send(json.dumps(subscribe_message))
            print("🔌 ✅ Subscription message sent")

            # Wait for messages
            print("🔌 Waiting for messages (timeout: 10 seconds)...")

            try:
                message_count = 0
                while message_count < 10:  # Max 10 messages
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        message_count += 1

                        print(f"🔌 ✅ Received message #{message_count}: {message}")

                        # Parse the message
                        data = json.loads(message)
                        stream = data.get('stream', '')

                        if stream.startswith('trade.'):
                            print(f"🔌 ✅ ✅ TRADE MESSAGE DETECTED! Stream: {stream}")
                            payload = data.get('data', {})
                            if payload.get('e') == 'trade':
                                price = payload.get('p', 'N/A')
                                quantity = payload.get('q', 'N/A')
                                print(f"🔌 ✅ ✅ ✅ Trade data: Price={price}, Quantity={quantity}")
                                break  # Got trade data, success!

                    except asyncio.TimeoutError:
                        print(f"🔌 ⏳ Waiting for message... ({message_count}/10)")
                        continue

                print(f"🔌 🎯 Total messages received: {message_count}")

            except asyncio.TimeoutError:
                print("🔌 ❌ No messages received within timeout")

    except Exception as e:
        print(f"🔌 ❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_backpack_websocket())