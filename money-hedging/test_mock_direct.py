#!/usr/bin/env python3
"""
Direct test of the mock exchange logic
"""

import asyncio
import json
from decimal import Decimal
from exchanges.factory import ExchangeFactory

# Define config class inline since it's in strategy file
class AccountTradingConfig:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


async def test_mock_direct():
    """Test the mock exchange directly"""
    print("🔌 Testing mock exchange directly...")

    # Create config
    config = AccountTradingConfig(
        name='test_mock',
        exchange='backpack_mock',
        ticker='ETH',
        quantity=Decimal('0.1'),
        direction='buy'
    )

    try:
        # Create mock client
        print("🔌 Creating mock client...")
        client = ExchangeFactory.create_exchange('backpack_mock', config)

        print(f"🔌 Client created: {type(client).__name__}")
        print(f"🔌 Exchange name: {client.get_exchange_name()}")

        # Test contract attributes
        print("🔌 Getting contract attributes...")
        contract_id, tick_size = await client.get_contract_attributes()
        print(f"🔌 Contract ID: {contract_id}")
        print(f"🔌 Tick size: {tick_size}")

        # Debug: Let's also check what markets are available
        markets = client.public_client.get_markets()
        print(f"🔌 Debug: Total markets available: {len(markets)}")

        # Debug: Show ETH markets
        eth_markets = [m for m in markets if m.get('baseSymbol', '') == 'ETH']
        print(f"🔌 Debug: ETH markets found: {len(eth_markets)}")
        for i, m in enumerate(eth_markets[:3]):
            print(f"  {i+1}. {m.get('symbol')} - Type: {m.get('marketType')}")

        # Debug: Show PERP markets
        perp_markets = [m for m in markets if m.get('marketType', '') == 'PERP']
        print(f"🔌 Debug: PERP markets found: {len(perp_markets)}")
        for i, m in enumerate(perp_markets[:3]):
            print(f"  {i+1}. {m.get('symbol')} - Base: {m.get('baseSymbol')}")

        # The problem: found ETH_USDC but Backpack needs ETH_USDC_PERP
        # Let's check if we should add _PERP suffix
        if contract_id == 'ETH_USDC':
            correct_contract_id = 'ETH_USDC_PERP'
            print(f"🔌 ❌ Contract ID mismatch! Found: {contract_id}, Backpack needs: {correct_contract_id}")

            # Let's manually set the correct contract ID for testing
            client.config.contract_id = correct_contract_id
            print(f"🔌 🔧 Manually corrected contract ID to: {correct_contract_id}")

        # Test WebSocket connection
        print("🔌 Connecting to WebSocket...")
        await client.connect()

        # Wait a bit and see if we get any messages
        print("🔌 Waiting 15 seconds for WebSocket messages...")

        # Check task status every 2 seconds
        for i in range(8):  # 15 seconds / 2 = 7.5, so 8 checks
            await asyncio.sleep(2)

            # Check if the task is still running
            if hasattr(client, 'ws_manager') and client.ws_manager:
                print(f"🔌 Task {i+1}/8 - WebSocket running: {client.ws_manager.running}")

            # Check if task is done or failed
            if hasattr(client, 'ws_manager') and client.ws_manager and hasattr(client.ws_manager, 'websocket'):
                print(f"🔌 Task {i+1}/8 - WebSocket state: {client.ws_manager.websocket}")

            # Let's try to manually check if we got any data by calling a test method
            if hasattr(client, 'virtual_orders'):
                print(f"🔌 Task {i+1}/8 - Virtual orders count: {len(client.virtual_orders)}")

        # Check subscription status
        print(f"🔌 Final subscription confirmed: {client.is_subscription_confirmed()}")

    except Exception as e:
        print(f"🔌 ❌ Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        try:
            await client.disconnect()
            print("🔌 ✅ Disconnected")
        except:
            pass


if __name__ == "__main__":
    asyncio.run(test_mock_direct())