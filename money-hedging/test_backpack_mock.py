#!/usr/bin/env python3
"""
Test script for BackpackMockClient.
"""

import asyncio
import sys
from decimal import Decimal
from exchanges.factory import ExchangeFactory
from exchanges.base import AccountTradingConfig


async def test_backpack_mock():
    """Test the backpack mock exchange client."""

    # Create configuration for mock client
    config = AccountTradingConfig(
        name="test_account",
        exchange="backpack_mock",
        ticker="SOL",
        quantity=Decimal("1"),
        direction="buy"
    )

    # Create mock client
    print("Creating BackpackMockClient...")
    client = ExchangeFactory.create_exchange("backpack_mock", config.__dict__)

    try:
        # Connect to exchange
        print("Connecting to mock exchange...")
        await client.connect()

        # Get contract attributes
        print("Getting contract attributes...")
        contract_id, tick_size = await client.get_contract_attributes()
        print(f"Contract ID: {contract_id}, Tick size: {tick_size}")

        # Get BBO prices
        print("Getting BBO prices...")
        best_bid, best_ask = await client.fetch_bbo_prices(contract_id)
        print(f"Best bid: {best_bid}, Best ask: {best_ask}")

        # Test immediate fill scenario
        print("\n=== Testing immediate fill scenario ===")

        # For buy order, use price >= best_ask
        if best_ask > 0:
            buy_price = best_ask  # Should fill immediately
            print(f"Placing buy order at {buy_price} (>= best ask {best_ask})...")

            # Note: Our mock uses maker pricing logic, so we need to test with normal flow
            result = await client.place_open_order(contract_id, Decimal("0.1"), "buy")
            print(f"Buy order result: success={result.success}, order_id={result.order_id}, status={result.status}")

        # Test virtual order scenario
        print("\n=== Testing virtual order scenario ===")

        # For sell order, use price <= best_bid
        if best_bid > 0:
            print(f"Placing sell order that should create virtual order...")
            result = await client.place_open_order(contract_id, Decimal("0.1"), "sell")
            print(f"Sell order result: success={result.success}, order_id={result.order_id}, status={result.status}")

            if result.success:
                # Check active orders
                print("Checking active orders...")
                active_orders = await client.get_active_orders(contract_id)
                print(f"Active orders count: {len(active_orders)}")
                for order in active_orders:
                    print(f"  Order {order.order_id}: {order.side} {order.size} @ {order.price}")

        # Test market order
        print("\n=== Testing market order ===")
        market_result = await client.place_market_order(contract_id, Decimal("0.05"), "buy")
        print(f"Market order result: success={market_result.success}, price={market_result.price}")

        # Wait a bit to see if any virtual orders get filled by trade data
        print("\nWaiting 10 seconds to monitor trade data...")
        await asyncio.sleep(10)

        # Check active orders again
        print("Checking active orders after wait...")
        final_orders = await client.get_active_orders(contract_id)
        print(f"Final active orders count: {len(final_orders)}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Disconnect
        print("\nDisconnecting...")
        await client.disconnect()
        print("Test completed!")


if __name__ == "__main__":
    asyncio.run(test_backpack_mock())