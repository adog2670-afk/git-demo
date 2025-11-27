"""
Backpack mock exchange client implementation.

This mock client simulates trading logic without real orders:
1. Checks order book to determine if order can be filled immediately
2. If not fillable, creates a virtual maker order
3. Monitors real trade data to determine if virtual order gets filled
"""

import os
import asyncio
import json
import time
import base64
import sys
import uuid
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from cryptography.hazmat.primitives.asymmetric import ed25519
import websockets
import requests
from bpx.public import Public

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger


@dataclass
class VirtualOrder:
    """Virtual order stored in memory."""

    order_id: str
    symbol: str
    side: str  # 'buy' or 'sell'
    quantity: Decimal
    price: Decimal
    order_type: str = "limit"
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    filled: bool = False
    fill_price: Optional[Decimal] = None
    filled_quantity: Decimal = field(default_factory=lambda: Decimal("0"))
    status: str = "OPEN"

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal("0"), self.quantity - self.filled_quantity)


class BackpackMockWebSocketManager:
    """WebSocket manager for Backpack mock exchange - subscribes to trade data."""

    def __init__(self, symbol: str, trade_update_callback):
        self.symbol = symbol
        self.trade_update_callback = trade_update_callback
        self.websocket = None
        self.running = False
        self.ws_url = "wss://ws.backpack.exchange"
        self.logger = None

    async def connect(self):
        """Connect to Backpack WebSocket and subscribe to trades."""
        if self.logger:
            self.logger.log(
                f"🔌 [WS-MANAGER] Starting WebSocket connection for symbol: {self.symbol}",
                "INFO",
            )

        # Set running to True when starting connection
        self.running = True

        while self.running:  # Check running flag to allow graceful shutdown
            try:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Connecting to {self.ws_url}", "INFO"
                    )

                try:
                    self.websocket = await websockets.connect(self.ws_url)
                    self.running = True

                    if self.logger:
                        self.logger.log(
                            "🔌 [WS-MANAGER] WebSocket connected successfully", "INFO"
                        )

                except Exception as connect_error:
                    raise

                # Subscribe to trade data for the specific symbol (using "trade." not "trades.")
                subscribe_message = {
                    "method": "SUBSCRIBE",
                    "params": [f"trade.{self.symbol}"],
                }

                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Subscribing to trade.{self.symbol}", "INFO"
                    )
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Subscription message: {json.dumps(subscribe_message)}",
                        "INFO",
                    )

                await self.websocket.send(json.dumps(subscribe_message))
                if self.logger:
                    self.logger.log("🔌 [WS-MANAGER] Subscription message sent", "INFO")

                # Wait a moment to see if subscription is accepted
                await asyncio.sleep(1)

                if self.logger:
                    self.logger.log(
                        "🔌 [WS-MANAGER] Starting to listen for messages...", "INFO"
                    )

                # Start listening for messages
                await self._listen()

            except websockets.exceptions.ConnectionClosed as e:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] WebSocket connection closed: {e}", "INFO"
                    )

            except Exception as e:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] WebSocket connection error: {e}", "ERROR"
                    )
                    import traceback

                    self.logger.log(traceback.format_exc(), "ERROR")

                traceback.print_exc()

                if self.running:  # Only retry if still running
                    if self.logger:
                        self.logger.log(
                            "🔌 [WS-MANAGER] Retrying in 5 seconds...", "INFO"
                        )

                    await asyncio.sleep(5)  # Wait before retry

    async def _listen(self):
        """Listen for WebSocket messages."""
        if self.logger:
            self.logger.log("🔌 [WS-MANAGER] Starting message listener...", "INFO")

        try:
            message_count = 0
            async for message in self.websocket:
                if not self.running:
                    if self.logger:
                        self.logger.log(
                            "🔌 [WS-MANAGER] Stopping message listener (running=False)",
                            "INFO",
                        )

                    break

                message_count += 1
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Received message #{message_count}", "INFO"
                    )

                try:
                    data = json.loads(message)

                    # Print all received messages for debugging
                    if self.logger:
                        self.logger.log(
                            f"🔍 [WS-MANAGER] RAW MESSAGE #{message_count}: {message}",
                            "INFO",
                        )
                        self.logger.log(
                            f"🔍 [WS-MANAGER] PARSED DATA #{message_count}: {data}",
                            "INFO",
                        )

                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    if self.logger:
                        self.logger.log(
                            f"🔌 [WS-MANAGER] Failed to parse WebSocket message #{message_count}: {e}",
                            "ERROR",
                        )
                        self.logger.log(
                            f"🔍 [WS-MANAGER] RAW MESSAGE THAT FAILED: {message}",
                            "ERROR",
                        )

                except Exception as e:
                    if self.logger:
                        self.logger.log(
                            f"🔌 [WS-MANAGER] Error handling WebSocket message #{message_count}: {e}",
                            "ERROR",
                        )

            if self.logger:
                self.logger.log(
                    f"🔌 [WS-MANAGER] Message listener stopped. Total messages received: {message_count}",
                    "INFO",
                )

        except websockets.exceptions.ConnectionClosed as e:
            if self.logger:
                self.logger.log(
                    f"🔌 [WS-MANAGER] WebSocket connection closed in listener: {e}",
                    "WARNING",
                )
        except Exception as e:
            if self.logger:
                self.logger.log(f"🔌 [WS-MANAGER] WebSocket listen error: {e}", "ERROR")
                import traceback

                self.logger.log(traceback.format_exc(), "ERROR")

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket messages."""
        try:
            stream = data.get("stream", "")
            payload = data.get("data", {})

            if self.logger:
                self.logger.log(
                    f"🔍 WebSocket Message - Stream: {stream}, Data: {data}", "INFO"
                )

            # Check for different trade message formats
            is_trade_message = False

            # Stream format should be "trade.SYMBOL" not "trades.SYMBOL"
            if stream.startswith("trade."):
                is_trade_message = True
                self.logger.log(
                    f"🔍 *** ✅ TRADE MESSAGE DETECTED *** in stream: {stream}", "INFO"
                )
            elif isinstance(payload, dict) and payload.get("e") == "trade":
                is_trade_message = True
                self.logger.log(
                    f"🔍 *** ✅ TRADE EVENT DETECTED ***: {payload}", "INFO"
                )

            if is_trade_message:
                self.logger.log(f"🔍 *** PROCESSING TRADE DATA ***: {payload}", "INFO")
                await self._handle_trade_update(payload)
            else:
                if self.logger:
                    self.logger.log(f"🔍 Non-trade message received: {data}", "INFO")

        except Exception as e:
            if self.logger:
                self.logger.log(f"Error handling WebSocket message: {e}", "ERROR")
                import traceback

                self.logger.log(traceback.format_exc(), "DEBUG")

    async def _handle_trade_update(self, trade_data: Dict[str, Any]):
        """Handle trade update messages."""
        try:
            if self.logger:
                self.logger.log(
                    f"🔍 *** TRADE UPDATE CALLBACK *** - Data: {trade_data}", "INFO"
                )

            if self.trade_update_callback:
                self.logger.log(
                    f"🔍 *** CALLING TRADE UPDATE CALLBACK *** with data: {trade_data}",
                    "INFO",
                )
                await self.trade_update_callback(trade_data)
            else:
                if self.logger:
                    self.logger.log(
                        "🔍 *** NO TRADE UPDATE CALLBACK REGISTERED ***", "WARNING"
                    )
        except Exception as e:
            if self.logger:
                self.logger.log(f"Error handling trade update: {e}", "ERROR")
                import traceback

                self.logger.log(traceback.format_exc(), "DEBUG")

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            if self.logger:
                self.logger.log("WebSocket disconnected", "INFO")

    def set_logger(self, logger):
        """Set the logger instance."""
        self.logger = logger  # 这里不赋值就可以关闭掉[WS-MANAGER]


class BackpackMockClient(BaseExchangeClient):
    """Backpack mock exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Backpack mock client."""
        super().__init__(config)

        # Initialize logger early
        self.logger = TradingLogger(
            exchange="backpack_mock", ticker=self.config.ticker, log_to_console=False
        )

        # Initialize public client for order book data
        self.public_client = Public()

        # Virtual order management
        self.virtual_orders: Dict[str, VirtualOrder] = {}
        self.completed_orders: Dict[str, VirtualOrder] = {}
        self._order_update_handler = None
        self.positions: Dict[str, Decimal] = {}
        self.quote_balances: Dict[str, Decimal] = {}
        self.initial_quote_balance = Decimal("100000")
        self._state_lock = asyncio.Lock()
        self._ws_task: Optional[asyncio.Task] = None

        # WebSocket manager for trade data
        self.ws_manager = None

        # Subscription validation
        self.subscription_confirmed = False  # 是否收到第一笔交易数据
        self.first_trade_received = False

    def _ensure_symbol_tracking(self, symbol: str):
        if symbol not in self.positions:
            self.positions[symbol] = Decimal("0")
        if symbol not in self.quote_balances:
            self.quote_balances[symbol] = self.initial_quote_balance

    def _apply_fill_locked(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal
    ):
        self._ensure_symbol_tracking(symbol)
        signed_qty = quantity if side.lower() == "buy" else -quantity
        self.positions[symbol] += signed_qty

        notional = quantity * price
        if side.lower() == "buy":
            self.quote_balances[symbol] -= notional
        else:
            self.quote_balances[symbol] += notional

    def _build_order_update_payload(
        self, order: VirtualOrder, status: str, fill_price: Decimal
    ) -> Dict[str, Any]:
        return {
            "order_id": order.order_id,
            "side": order.side,
            "order_type": order.order_type.upper(),
            "status": status.upper(),
            "size": str(order.quantity),
            "price": str(fill_price),
            "contract_id": order.symbol,
            "filled_size": str(order.filled_quantity),
        }

    def _emit_order_update(self, payload: Dict[str, Any]):
        if not self._order_update_handler:
            return
        try:
            self._order_update_handler(payload)
        except Exception as exc:
            self.logger.log(
                f"🔍 Error emitting mock order update for {payload.get('order_id')}: {exc}",
                "ERROR",
            )

    async def _finalize_virtual_order_fill(
        self, virtual_order: VirtualOrder, fill_price: Decimal
    ):
        """Mark a virtual limit order as filled and emit an update."""
        async with self._state_lock:
            if virtual_order.order_id not in self.virtual_orders:
                return

            fill_quantity = virtual_order.remaining_quantity
            if fill_quantity <= 0:
                return

            virtual_order.filled_quantity += fill_quantity
            virtual_order.fill_price = fill_price
            virtual_order.filled = True
            virtual_order.status = "FILLED"

            self._apply_fill_locked(
                virtual_order.symbol, virtual_order.side, fill_quantity, fill_price
            )

            # Move to completed orders for introspection
            self.completed_orders[virtual_order.order_id] = virtual_order
            del self.virtual_orders[virtual_order.order_id]

        payload = self._build_order_update_payload(
            virtual_order, virtual_order.status, fill_price
        )
        self._emit_order_update(payload)

    def _validate_config(self) -> None:
        """Validate mock configuration (no real credentials needed)."""
        if not self.config.ticker:
            raise ValueError("Ticker must be specified in config")

    async def connect(self) -> None:
        """Connect to Backpack WebSocket for trade data."""
        self.logger.log(
            "🔌 [MOCK] *** BACKPACK MOCK EXCHANGE CONNECT CALLED ***", "INFO"
        )
        print("🔌 [MOCK] *** BACKPACK MOCK EXCHANGE CONNECT CALLED ***")
        self.logger.log(f"🔌 [MOCK] Contract ID: '{self.config.contract_id}'", "INFO")
        print(f"🔌 [MOCK] Contract ID: '{self.config.contract_id}'")
        self.logger.log(f"🔌 [MOCK] Ticker: '{self.config.ticker}'", "INFO")
        print(f"🔌 [MOCK] Ticker: '{self.config.ticker}'")

        if not self.config.contract_id:
            self.logger.log("🔌 [MOCK] WARNING: Contract ID is empty!", "WARNING")
            print("🔌 [MOCK] WARNING: Contract ID is empty!")
            return

        # Initialize WebSocket manager for trades
        self.logger.log("🔌 [MOCK] Creating WebSocket manager...", "INFO")
        print("🔌 [MOCK] Creating WebSocket manager...")
        self.ws_manager = BackpackMockWebSocketManager(
            symbol=self.config.contract_id,
            trade_update_callback=self._handle_trade_update,
        )

        # Set logger for WebSocket manager (logger already initialized in __init__)
        self.ws_manager.set_logger(self.logger)

        try:
            self.logger.log("🔌 [MOCK] Creating background WebSocket task...", "INFO")
            print("🔌 [MOCK] Creating background WebSocket task...")

            # Create a wrapper task that includes exception handling
            async def websocket_task_wrapper():
                try:
                    await self.ws_manager.connect()
                except Exception as e:
                    self.logger.log(
                        f"🔌 [MOCK] WebSocket wrapper caught exception: {e}", "ERROR"
                    )
                    print(f"🔌 [MOCK] WebSocket wrapper caught exception: {e}")
                    import traceback

                    self.logger.log(traceback.format_exc(), "ERROR")
                    traceback.print_exc()
                    raise

            # Start WebSocket connection in background task
            task = asyncio.create_task(websocket_task_wrapper())
            self._ws_task = task
            self.logger.log(
                f"🔌 [MOCK] WebSocket task created: {task.get_name()}", "INFO"
            )
            print(f"🔌 [MOCK] WebSocket task created: {task.get_name()}")

            # Wait a moment for connection to establish
            self.logger.log(
                "🔌 [MOCK] Waiting for WebSocket connection to establish...", "INFO"
            )
            print("🔌 [MOCK] Waiting for WebSocket connection to establish...")

            # Check task status at intervals
            for i in range(5):  # Check 5 times over 5 seconds
                await asyncio.sleep(1)

                if task.done():
                    self.logger.log(
                        f"🔌 [MOCK] Task completed after {i + 1} seconds", "INFO"
                    )
                    print(f"🔌 [MOCK] Task completed after {i + 1} seconds")

                    if task.exception():
                        self.logger.log(
                            f"🔌 [MOCK] WebSocket task failed: {task.exception()}",
                            "ERROR",
                        )
                        print(f"🔌 [MOCK] WebSocket task failed: {task.exception()}")
                        raise task.exception()
                    else:
                        self.logger.log(
                            f"🔌 [MOCK] Task completed without exception", "WARNING"
                        )
                        print(f"🔌 [MOCK] Task completed without exception")
                        break
                else:
                    self.logger.log(
                        f"🔌 [MOCK] Task still running after {i + 1} seconds", "INFO"
                    )
                    print(f"🔌 [MOCK] Task still running after {i + 1} seconds")

            # Final check if task is still running
            if not task.done():
                self.logger.log("🔌 [MOCK] *** WEBSOCKET SETUP COMPLETED ***", "INFO")
                print("🔌 [MOCK] *** WEBSOCKET SETUP COMPLETED ***")
            else:
                self.logger.log("🔌 [MOCK] Task finished unexpectedly", "ERROR")
                print("🔌 [MOCK] Task finished unexpectedly")

        except Exception as e:
            self.logger.log(
                f"🔌 [MOCK] Error connecting to Backpack WebSocket: {e}", "ERROR"
            )
            print(f"🔌 [MOCK] Error connecting to Backpack WebSocket: {e}")
            import traceback

            self.logger.log(traceback.format_exc(), "ERROR")
            traceback.print_exc()
            raise

    async def disconnect(self) -> None:
        """Disconnect from Backpack."""
        try:
            self.logger.log("🔍 Starting disconnect process...", "INFO")

            if hasattr(self, "ws_manager") and self.ws_manager:
                self.logger.log("🔍 Disconnecting WebSocket manager...", "INFO")
                await self.ws_manager.disconnect()

            async with self._state_lock:
                if self.virtual_orders:
                    self.logger.log(
                        f"🔍 Clearing {len(self.virtual_orders)} virtual orders", "INFO"
                    )
                    self.virtual_orders.clear()
                if self.completed_orders:
                    self.logger.log(
                        f"🔍 Clearing {len(self.completed_orders)} completed orders",
                        "INFO",
                    )
                    self.completed_orders.clear()

            self.logger.log("🔍 Disconnect completed", "INFO")

        except Exception as e:
            if hasattr(self, "logger"):
                self.logger.log(f"Error during Backpack disconnect: {e}", "ERROR")
            else:
                print(f"Error during Backpack disconnect: {e}")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "backpack_mock"

    def is_subscription_confirmed(self) -> bool:
        """Check if WebSocket subscription is confirmed (received first trade data)."""
        return self.subscription_confirmed

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler."""
        self._order_update_handler = handler

    async def _handle_trade_update(self, trade_data: Dict[str, Any]):
        """Handle trade updates and check if virtual orders get filled."""
        try:
            price = None
            quantity = None

            self.logger.log(f"🔍 Trade Update: {trade_data}", "INFO")

            if "p" in trade_data:
                price = Decimal(str(trade_data.get("p", "0")))
                quantity = Decimal(str(trade_data.get("q", "0")))
            elif "price" in trade_data:
                price = Decimal(str(trade_data.get("price", "0")))
                quantity = Decimal(str(trade_data.get("quantity", "0")))
            else:
                self.logger.log(
                    f"🔍 Unknown trade data format: {trade_data}", "WARNING"
                )
                return

            if not price or price <= 0 or not quantity or quantity <= 0:
                self.logger.log(
                    f"🔍 Invalid trade data: price={price}, quantity={quantity}",
                    "WARNING",
                )
                return

            if not self.first_trade_received:
                self.first_trade_received = True
                self.subscription_confirmed = True
                self.logger.log(
                    "🎉 *** FIRST TRADE DATA RECEIVED *** - Subscription confirmed!",
                    "INFO",
                )

            async with self._state_lock:
                open_orders = list(self.virtual_orders.values())

            if not open_orders:
                return

            to_fill: List[VirtualOrder] = []
            for virtual_order in open_orders:
                if virtual_order.side == "buy" and price < virtual_order.price:
                    to_fill.append(virtual_order)
                    self.logger.log(
                        f"[MOCK-FILL]✅ order {virtual_order.side} at price:{virtual_order.price},filled price{price}"
                    )
                elif virtual_order.side == "sell" and price > virtual_order.price:
                    to_fill.append(virtual_order)
                    self.logger.log(
                        f"[MOCK-FILL]✅ order {virtual_order.side} at price:{virtual_order.price},filled price{price}"
                    )
                else:
                    self.logger.log(
                        f"[MOCK-FILL]❌ order {virtual_order.side} at price:{virtual_order.price},unfill price{price}"
                    )

            if not to_fill:
                return

            for order in to_fill:
                await self._finalize_virtual_order_fill(order, price)

        except Exception as e:
            self.logger.log(f"🔍 ❌ Error handling trade update: {e}", "ERROR")
            import traceback

            self.logger.log(traceback.format_exc(), "DEBUG")

    async def get_order_price(self, direction: str) -> Decimal:
        """Get the price of an order using current market data."""
        best_bid, best_ask = await self.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0:
            self.logger.log("Invalid bid/ask prices", "ERROR")
            raise ValueError("Invalid bid/ask prices")

        if direction == "buy":
            # For buy orders, place slightly below best ask
            order_price = best_ask - self.config.tick_size
        else:
            # For sell orders, place slightly above best bid
            order_price = best_bid + self.config.tick_size
        return self.round_to_tick(order_price)

    @query_retry(default_return=(0, 0))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        """Get current BBO prices from Backpack."""
        # Get order book depth from Backpack
        order_book = self.public_client.get_depth(contract_id)

        # Extract bids and asks
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])

        # Sort bids and asks
        bids = sorted(
            bids, key=lambda x: Decimal(x[0]), reverse=True
        )  # highest price first
        asks = sorted(asks, key=lambda x: Decimal(x[0]))  # lowest price first

        # Best bid is the highest price someone is willing to buy at
        best_bid = Decimal(bids[0][0]) if bids and len(bids) > 0 else 0
        # Best ask is the lowest price someone is willing to sell at
        best_ask = Decimal(asks[0][0]) if asks and len(asks) > 0 else 0

        return best_bid, best_ask

    async def _create_virtual_limit_order(
        self, contract_id: str, quantity: Decimal, side: str, price: Decimal
    ) -> OrderResult:
        """Create an in-memory limit order that waits for fills."""
        order_id = str(uuid.uuid4())
        virtual_order = VirtualOrder(
            order_id=order_id,
            symbol=contract_id,
            side=side.lower(),
            quantity=quantity,
            price=self.round_to_tick(price),
        )

        async with self._state_lock:
            self.virtual_orders[order_id] = virtual_order

        self.logger.log(
            f"[MOCK-LIMIT] Created virtual order {order_id} side={virtual_order.side} "
            f"qty={quantity} price={virtual_order.price}",
            "INFO",
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            side=virtual_order.side,
            size=quantity,
            price=virtual_order.price,
            status="NEW",
        )

    async def place_open_order(
        self,
        contract_id: str,
        quantity: Decimal,
        direction: str,
        for_trigger: bool = False,
    ) -> OrderResult:
        """Place a mock open order."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(
                    success=False, error_message="Invalid bid/ask prices"
                )

            maker_tick_offset = getattr(self.config, "maker_tick_offset", 1)

            if direction == "buy":
                if for_trigger:
                    order_price = best_bid
                else:
                    order_price = best_ask - (self.config.tick_size * maker_tick_offset)
            elif direction == "sell":
                if for_trigger:
                    order_price = best_ask
                else:
                    order_price = best_bid + (self.config.tick_size * maker_tick_offset)
            else:
                return OrderResult(
                    success=False, error_message=f"Invalid direction: {direction}"
                )

            return await self._create_virtual_limit_order(
                contract_id, quantity, direction, order_price
            )

        except Exception as e:
            self.logger.log(f"Error placing mock open order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    async def place_market_order(
        self, contract_id: str, quantity: Decimal, direction: str
    ) -> OrderResult:
        """Place a mock market order (immediate fill at current market price)."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(
                    success=False, error_message="Invalid bid/ask prices"
                )

            if direction == "buy":
                fill_price = best_ask
            elif direction == "sell":
                fill_price = best_bid
            else:
                return OrderResult(
                    success=False, error_message=f"Invalid direction: {direction}"
                )

            order_id = str(uuid.uuid4())
            market_order = VirtualOrder(
                order_id=order_id,
                symbol=contract_id,
                side=direction.lower(),
                quantity=quantity,
                price=fill_price,
                order_type="market",
                filled=True,
                fill_price=fill_price,
                filled_quantity=quantity,
                status="FILLED",
            )

            async with self._state_lock:
                self._apply_fill_locked(contract_id, direction, quantity, fill_price)
                self.completed_orders[order_id] = market_order

            self.logger.log(
                f"[MOCK-MARKET] Filled order {order_id} side={direction} qty={quantity} price={fill_price}",
                "INFO",
            )

            payload = self._build_order_update_payload(
                market_order, "FILLED", fill_price
            )
            self._emit_order_update(payload)

            return OrderResult(
                success=True,
                order_id=order_id,
                side=direction,
                size=quantity,
                price=fill_price,
                status="FILLED",
            )

        except Exception as e:
            self.logger.log(f"Error placing mock market order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    async def place_close_order(
        self, contract_id: str, quantity: Decimal, price: Decimal, side: str
    ) -> OrderResult:
        """Place a mock close order that mirrors maker-style limits."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(
                    success=False, error_message="Invalid bid/ask prices"
                )

            adjusted_price = self.round_to_tick(price) if price and price > 0 else None
            if adjusted_price is None or adjusted_price <= 0:
                adjusted_price = best_ask if side.lower() == "buy" else best_bid

            if side.lower() == "sell":
                adjusted_price = best_bid + self.config.tick_size
            elif side.lower() == "buy":
                adjusted_price = best_ask - self.config.tick_size

            return await self._create_virtual_limit_order(
                contract_id, quantity, side, adjusted_price
            )

        except Exception as e:
            self.logger.log(f"Error placing mock close order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a mock order."""
        try:
            virtual_order = None
            async with self._state_lock:
                virtual_order = self.virtual_orders.pop(order_id, None)
                if virtual_order:
                    virtual_order.status = "CANCELED"
                    self.completed_orders[order_id] = virtual_order

            if virtual_order:
                self.logger.log(
                    f"[MOCK-CANCEL] Canceled virtual order {order_id}", "INFO"
                )
                payload = self._build_order_update_payload(
                    virtual_order,
                    virtual_order.status,
                    virtual_order.fill_price or virtual_order.price,
                )
                self._emit_order_update(payload)
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status="CANCELED",
                    filled_size=virtual_order.filled_quantity,
                )

            self.logger.log(
                f"[MOCK-CANCEL] Order {order_id} not found or already closed",
                "WARNING",
            )
            return OrderResult(success=True, order_id=order_id, status="NOT_FOUND")

        except Exception as e:
            self.logger.log(f"Error canceling mock order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry()
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get mock order information."""
        order = self.virtual_orders.get(order_id) or self.completed_orders.get(order_id)
        if not order:
            return None

        return OrderInfo(
            order_id=order.order_id,
            side=order.side,
            size=order.quantity,
            price=order.fill_price or order.price,
            status=order.status.upper(),
            filled_size=order.filled_quantity,
            remaining_size=order.remaining_quantity,
        )

    @query_retry(default_return=[])
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active mock orders."""
        orders: List[OrderInfo] = []
        for virtual_order in self.virtual_orders.values():
            if virtual_order.symbol != contract_id:
                continue
            orders.append(
                OrderInfo(
                    order_id=virtual_order.order_id,
                    side=virtual_order.side,
                    size=virtual_order.quantity,
                    price=virtual_order.price,
                    status=virtual_order.status.upper(),
                    filled_size=virtual_order.filled_quantity,
                    remaining_size=virtual_order.remaining_quantity,
                )
            )
        return orders

    @query_retry(default_return=0)
    async def get_account_positions(self) -> Decimal:
        """Mock positions - always return 0 since we're not tracking real positions."""
        symbol = getattr(self.config, "contract_id", None)
        if not symbol:
            return Decimal("0")
        return self.positions.get(symbol, Decimal("0"))

    async def get_account_balance(self) -> Decimal:
        """Mock balance - simulate balance changes based on virtual orders."""
        symbol = getattr(self.config, "contract_id", None)
        if not symbol:
            return self.initial_quote_balance
        return self.quote_balances.get(symbol, self.initial_quote_balance)

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract attributes for the ticker."""
        self.logger.log("🔌 [MOCK] *** GET CONTRACT ATTRIBUTES CALLED ***", "INFO")

        ticker = self.config.ticker
        if len(ticker) == 0:
            self.logger.log("🔌 [MOCK] Ticker is empty", "ERROR")
            raise ValueError("Ticker is empty")

        self.logger.log(f"🔌 [MOCK] Searching markets for ticker: '{ticker}'", "INFO")

        try:
            markets = self.public_client.get_markets()
            self.logger.log(f"🔌 [MOCK] Found {len(markets)} markets", "INFO")
        except Exception as e:
            self.logger.log(f"🔌 [MOCK] Error getting markets: {e}", "ERROR")
            raise

        # Try different matching strategies - prioritize PERP over SPOT
        contract_found = False

        # Separate markets into PERP and others to prioritize PERP
        perp_markets = [m for m in markets if m.get("marketType") == "PERP"]
        other_markets = [m for m in markets if m.get("marketType") != "PERP"]
        # Process PERP markets first, then others
        ordered_markets = perp_markets + other_markets

        for market in ordered_markets:
            market_type = market.get("marketType", "")
            base_symbol = market.get("baseSymbol", "")
            quote_symbol = market.get("quoteSymbol", "")
            symbol = market.get("symbol", "")

            self.logger.log(
                f"Checking market: {symbol} (type: {market_type}, base: {base_symbol}, quote: {quote_symbol})",
                "DEBUG",
            )

            # Strategy 1: Direct match for PERP contracts (highest priority)
            if (
                market_type == "PERP"
                and base_symbol == ticker
                and quote_symbol == "USDC"
            ):
                self.config.contract_id = symbol
                min_quantity = Decimal(
                    market.get("filters", {}).get("quantity", {}).get("minQuantity", 0)
                )
                self.config.tick_size = Decimal(
                    market.get("filters", {}).get("price", {}).get("tickSize", 0)
                )
                contract_found = True
                self.logger.log(f"Found PERP contract: {symbol}", "INFO")
                break

            # Strategy 2: Case-insensitive match for PERP contracts (second priority)
            elif (
                market_type == "PERP"
                and base_symbol.upper() == ticker.upper()
                and quote_symbol == "USDC"
            ):
                self.config.contract_id = symbol
                min_quantity = Decimal(
                    market.get("filters", {}).get("quantity", {}).get("minQuantity", 0)
                )
                self.config.tick_size = Decimal(
                    market.get("filters", {}).get("price", {}).get("tickSize", 0)
                )
                contract_found = True
                self.logger.log(f"Found case-insensitive PERP match: {symbol}", "INFO")
                break

            # Strategy 3: Match by symbol directly (lower priority)
            elif symbol == ticker:
                self.config.contract_id = symbol
                min_quantity = Decimal(
                    market.get("filters", {}).get("quantity", {}).get("minQuantity", 0)
                )
                self.config.tick_size = Decimal(
                    market.get("filters", {}).get("price", {}).get("tickSize", 0)
                )
                contract_found = True
                self.logger.log(f"Found direct symbol match: {symbol}", "INFO")
                break

            # Strategy 4: Case-insensitive match for non-PERP (lowest priority)
            elif base_symbol.upper() == ticker.upper() and quote_symbol == "USDC":
                self.config.contract_id = symbol
                min_quantity = Decimal(
                    market.get("filters", {}).get("quantity", {}).get("minQuantity", 0)
                )
                self.config.tick_size = Decimal(
                    market.get("filters", {}).get("price", {}).get("tickSize", 0)
                )
                contract_found = True
                self.logger.log(f"Found case-insensitive match: {symbol}", "INFO")
                break

        if not contract_found:
            # List available markets for debugging
            available_perps = [
                m.get("symbol", "") for m in markets if m.get("marketType") == "PERP"
            ]
            self.logger.log(f"Failed to get contract ID for ticker: {ticker}", "ERROR")
            self.logger.log(f"Available PERP markets: {available_perps[:10]}", "ERROR")
            raise ValueError(f"Failed to get contract ID for ticker: {ticker}")

        if self.config.quantity < min_quantity:
            self.logger.log(
                f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}",
                "ERROR",
            )
            raise ValueError(
                f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}"
            )

        if self.config.tick_size == 0:
            self.logger.log("Failed to get tick size for ticker", "ERROR")
            raise ValueError("Failed to get tick size for ticker")

        return self.config.contract_id, self.config.tick_size
