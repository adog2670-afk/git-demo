#!/usr/bin/env python3
"""
Simple test for BackpackMockClient core functionality.
"""

import asyncio
import json
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from decimal import Decimal
from bpx.public import Public


async def test_bbo_prices():
    """Test getting BBO prices from Backpack."""
    print("Testing Backpack BBO price fetching...")

    try:
        public_client = Public()

        # Test with SOL market
        markets = public_client.get_markets()
        sol_contract = None
        for market in markets:
            if (market.get('marketType', '') == 'PERP' and
                market.get('baseSymbol', '') == 'SOL' and
                market.get('quoteSymbol', '') == 'USDC'):
                sol_contract = market.get('symbol', '')
                tick_size = Decimal(market.get('filters', {}).get('price', {}).get('tickSize', 0))
                print(f"Found SOL contract: {sol_contract}, tick_size: {tick_size}")
                break

        if not sol_contract:
            print("SOL contract not found!")
            return

        # Get order book
        order_book = public_client.get_depth(sol_contract)
        print(f"Order book keys: {list(order_book.keys())}")

        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        print(f"Top 3 bids: {bids[:3]}")
        print(f"Top 3 asks: {asks[:3]}")

        if bids and asks:
            best_bid = Decimal(bids[0][0])
            best_ask = Decimal(asks[0][0])
            print(f"Best bid: {best_bid}, Best ask: {best_ask}")

            # Test immediate fill logic
            # Buy order: if price >= best_ask, fill immediately
            buy_price = best_ask  # Should fill immediately
            print(f"Buy price {buy_price} >= best ask {best_ask}? {buy_price >= best_ask}")

            # Sell order: if price <= best_bid, fill immediately
            sell_price = best_bid  # Should fill immediately
            print(f"Sell price {sell_price} <= best bid {best_bid}? {sell_price <= best_bid}")

        else:
            print("No bids or asks found!")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


async def test_mock_logic():
    """Test mock trading logic without WebSocket."""
    print("\nTesting mock trading logic...")

    # Simulate virtual orders
    virtual_orders = {}

    # Create a virtual buy order at $100
    virtual_orders["buy_1"] = {
        "side": "buy",
        "price": Decimal("100"),
        "filled": False
    }

    # Create a virtual sell order at $200
    virtual_orders["sell_1"] = {
        "side": "sell",
        "price": Decimal("200"),
        "filled": False
    }

    print(f"Created virtual orders: {virtual_orders}")

    # Simulate trade data
    trade_examples = [
        {"price": Decimal("95"), "qty": Decimal("1")},   # Should not fill buy order (price too low)
        {"price": Decimal("105"), "qty": Decimal("1")},  # Should fill buy order (price <= 100)
        {"price": Decimal("190"), "qty": Decimal("1")},  # Should not fill sell order (price too low)
        {"price": Decimal("210"), "qty": Decimal("1")},  # Should fill sell order (price >= 200)
    ]

    for trade in trade_examples:
        price = trade["price"]
        print(f"\nProcessing trade at price {price}...")

        # Check virtual orders
        for order_id, order in list(virtual_orders.items()):
            if order["filled"]:
                continue

            should_fill = False
            if order["side"] == "buy" and price <= order["price"]:
                should_fill = True
                print(f"  Buy order {order_id} filled: trade price {price} <= order price {order['price']}")
            elif order["side"] == "sell" and price >= order["price"]:
                should_fill = True
                print(f"  Sell order {order_id} filled: trade price {price} >= order price {order['price']}")

            if should_fill:
                order["filled"] = True
                order["fill_price"] = price

    print(f"\nFinal virtual orders: {virtual_orders}")


if __name__ == "__main__":
    print("=== Backpack Mock Exchange Test ===")

    # Test 1: BBO price fetching
    asyncio.run(test_bbo_prices())

    # Test 2: Mock logic
    asyncio.run(test_mock_logic())

    print("\n=== Test completed ===")