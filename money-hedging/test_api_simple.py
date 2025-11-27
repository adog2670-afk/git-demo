#!/usr/bin/env python3
"""
Simple test for Backpack API without external dependencies.
"""

import requests
import json
from decimal import Decimal


def test_backpack_api():
    """Test Backpack public API."""
    print("Testing Backpack public API...")

    base_url = "https://api.backpack.exchange"

    try:
        # Get markets
        print("Getting markets...")
        response = requests.get(f"{base_url}/api/v1/markets")
        if response.status_code != 200:
            print(f"Failed to get markets: {response.status_code}")
            return

        markets = response.json()
        print(f"Found {len(markets)} markets")

        # Find SOL market
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
        print(f"Getting order book for {sol_contract}...")
        response = requests.get(f"{base_url}/api/v1/depth", params={"symbol": sol_contract})
        if response.status_code != 200:
            print(f"Failed to get order book: {response.status_code}")
            return

        order_book = response.json()
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

            # Test virtual order logic
            print("\nTesting virtual order logic...")
            virtual_buy_price = best_ask - tick_size  # Normal maker price
            virtual_sell_price = best_bid + tick_size  # Normal maker price

            print(f"Virtual buy order price: {virtual_buy_price} (below best ask {best_ask})")
            print(f"Virtual sell order price: {virtual_sell_price} (above best bid {best_bid})")

            # Check if these would be immediate fills
            buy_immediate = virtual_buy_price >= best_ask
            sell_immediate = virtual_sell_price <= best_bid

            print(f"Virtual buy would fill immediately? {buy_immediate}")
            print(f"Virtual sell would fill immediately? {sell_immediate}")

        else:
            print("No bids or asks found!")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


def test_mock_logic():
    """Test mock trading logic."""
    print("\n=== Testing Mock Trading Logic ===")

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

    for i, trade in enumerate(trade_examples, 1):
        price = trade["price"]
        print(f"\nTrade {i}: price={price}")

        # Check virtual orders
        for order_id, order in list(virtual_orders.items()):
            if order["filled"]:
                continue

            should_fill = False
            if order["side"] == "buy" and price <= order["price"]:
                should_fill = True
                print(f"  ✅ Buy order {order_id} filled: trade price {price} <= order price {order['price']}")
            elif order["side"] == "sell" and price >= order["price"]:
                should_fill = True
                print(f"  ✅ Sell order {order_id} filled: trade price {price} >= order price {order['price']}")

            if should_fill:
                order["filled"] = True
                order["fill_price"] = price
            else:
                print(f"  ❌ Order {order_id} not filled")

    print(f"\nFinal virtual orders:")
    for order_id, order in virtual_orders.items():
        status = "FILLED" if order["filled"] else "OPEN"
        fill_price = f" @ {order.get('fill_price', 'N/A')}" if order["filled"] else ""
        print(f"  {order_id}: {order['side']} {order['price']} - {status}{fill_price}")


if __name__ == "__main__":
    print("=== Backpack Mock Exchange Test ===")

    # Test 1: Backpack API
    test_backpack_api()

    # Test 2: Mock logic
    test_mock_logic()

    print("\n=== Test completed ===")