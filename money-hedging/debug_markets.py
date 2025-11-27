#!/usr/bin/env python3
"""
Debug the market finding logic
"""

from bpx.public import Public


def debug_markets():
    """Debug market finding logic"""
    print("🔌 Debugging market finding...")

    public_client = Public()
    markets = public_client.get_markets()

    print(f"🔌 Found {len(markets)} markets")

    # Find all ETH markets
    eth_markets = []
    for market in markets:
        if market.get('baseSymbol', '') == 'ETH':
            eth_markets.append(market)

    print(f"🔌 Found {len(eth_markets)} ETH markets:")
    for i, market in enumerate(eth_markets[:10]):  # Show first 10
        print(f"  {i+1}. Market: {market.get('symbol')} "
              f"Type: {market.get('marketType')} "
              f"Base: {market.get('baseSymbol')} "
              f"Quote: {market.get('quoteSymbol')}")

    # Find all PERP markets
    perp_markets = []
    for market in markets:
        if market.get('marketType', '') == 'PERP':
            perp_markets.append(market)

    print(f"\n🔌 Found {len(perp_markets)} PERP markets:")
    for i, market in enumerate(perp_markets[:10]):  # Show first 10
        print(f"  {i+1}. Market: {market.get('symbol')} "
              f"Type: {market.get('marketType')} "
              f"Base: {market.get('baseSymbol')} "
              f"Quote: {market.get('quoteSymbol')}")

    # Find ETH + PERP + USDC markets
    target_markets = []
    for market in markets:
        if (market.get('marketType', '') == 'PERP' and
            market.get('baseSymbol', '') == 'ETH' and
            market.get('quoteSymbol', '') == 'USDC'):
            target_markets.append(market)

    print(f"\n🔌 Found {len(target_markets)} ETH+PERP+USDC markets:")
    for i, market in enumerate(target_markets):
        print(f"  {i+1}. Market: {market.get('symbol')}")

    # Check if ETH_USDC exists
    eth_usdc_market = None
    for market in markets:
        if market.get('symbol', '') == 'ETH_USDC':
            eth_usdc_market = market
            break

    print(f"\n🔌 ETH_USDC market:")
    if eth_usdc_market:
        print(f"  Symbol: {eth_usdc_market.get('symbol')}")
        print(f"  Type: {eth_usdc_market.get('marketType')}")
        print(f"  Base: {eth_usdc_market.get('baseSymbol')}")
        print(f"  Quote: {eth_usdc_market.get('quoteSymbol')}")
    else:
        print("  Not found!")


if __name__ == "__main__":
    debug_markets()