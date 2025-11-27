#!/usr/bin/env python3
"""
Simple test to verify backpack mock initialization.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from decimal import Decimal
from exchanges.factory import ExchangeFactory
from exchanges.base import AccountTradingConfig


def test_mock_initialization():
    """Test that the mock client can be initialized without errors."""
    print("Testing BackpackMockClient initialization...")

    # Create configuration
    config = {
        'name': 'test_account',
        'exchange': 'backpack_mock',
        'ticker': 'SOL',
        'quantity': Decimal('0.1'),
        'direction': 'buy'
    }

    try:
        # Create mock client
        print("Creating mock client...")
        client = ExchangeFactory.create_exchange('backpack_mock', config)

        print(f"✅ Client created successfully: {type(client).__name__}")
        print(f"   Exchange name: {client.get_exchange_name()}")
        print(f"   Logger exists: {hasattr(client, 'logger')}")
        print(f"   Virtual orders dict exists: {hasattr(client, 'virtual_orders')}")
        print(f"   Public client exists: {hasattr(client, 'public_client')}")

        # Test basic methods that don't require async
        print("\nTesting basic configuration...")
        client._validate_config()
        print("✅ Configuration validation passed")

        return True

    except Exception as e:
        print(f"❌ Error during initialization: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_validation():
    """Test configuration validation."""
    print("\nTesting configuration validation...")

    # Test with empty ticker
    try:
        config = {
            'name': 'test_account',
            'exchange': 'backpack_mock',
            'ticker': '',  # Empty ticker
            'quantity': Decimal('0.1'),
            'direction': 'buy'
        }
        client = ExchangeFactory.create_exchange('backpack_mock', config)
        client._validate_config()
        print("❌ Should have failed with empty ticker")
        return False
    except ValueError as e:
        print(f"✅ Correctly caught empty ticker error: {e}")
        return True
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


if __name__ == "__main__":
    print("=== Backpack Mock Initialization Test ===")

    success1 = test_mock_initialization()
    success2 = test_config_validation()

    if success1 and success2:
        print("\n🎉 All tests passed! Mock client is ready to use.")
    else:
        print("\n❌ Some tests failed. Check the error messages above.")

    print("\n=== Test completed ===")