"""
Binance mock exchange client implementation.

This mock client simulates trading logic without real orders:
1. Checks order book to determine if order can be filled immediately
2. If not fillable, creates a virtual maker order
3. Monitors real trade data to determine if virtual order gets filled
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

from .base import BaseExchangeClient, OrderInfo, OrderResult, query_retry
from helpers.logger import TradingLogger

BINANCE_WS_URL = "wss://fstream.binance.com/stream"
BINANCE_REST_URL = "https://fapi.binance.com"
DEFAULT_SYMBOL_SUFFIX = "USDT"
BOOK_TICKER_PATH = "/fapi/v1/ticker/bookTicker"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"


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


class BinanceMockWebSocketManager:
    """WebSocket manager for Binance mock exchange - subscribes to trade data."""

    def __init__(self, symbol: str, trade_update_callback, ws_url: str = BINANCE_WS_URL):
        self.symbol = symbol.lower()
        self.trade_update_callback = trade_update_callback
        self.ws_url = ws_url
        self.stream_name = f"{self.symbol}@trade"
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self.logger = None

    async def connect(self):
        """Connect to Binance WebSocket and subscribe to trades."""
        if self.logger:
            self.logger.log(
                f"🔌 [WS-MANAGER] Starting WebSocket connection for stream: {self.stream_name}",
                "INFO",
            )

        self.running = True

        while self.running:
            try:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Connecting to {self.ws_url}", "INFO"
                    )

                self.websocket = await websockets.connect(self.ws_url)
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] WebSocket connected, subscribing to {self.stream_name}",
                        "INFO",
                    )

                subscribe_message = {"method": "SUBSCRIBE", "params": [self.stream_name]}
                await self.websocket.send(json.dumps(subscribe_message))

                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Subscription message sent: {subscribe_message}",
                        "INFO",
                    )

                await self._listen()

            except websockets.exceptions.ConnectionClosed as exc:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] WebSocket connection closed: {exc}", "INFO"
                    )
            except Exception as exc:
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] WebSocket connection error: {exc}",
                        "ERROR",
                    )
                    import traceback

                    self.logger.log(traceback.format_exc(), "ERROR")

            if self.running:
                if self.logger:
                    self.logger.log(
                        "🔌 [WS-MANAGER] Retrying WebSocket connection in 5 seconds",
                        "INFO",
                    )
                await asyncio.sleep(5)

    async def _listen(self):
        """Listen for WebSocket messages."""
        if not self.websocket:
            return

        try:
            message_count = 0
            async for message in self.websocket:
                if not self.running:
                    break

                message_count += 1
                if self.logger:
                    self.logger.log(
                        f"🔌 [WS-MANAGER] Received message #{message_count}", "DEBUG"
                    )

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as exc:
                    if self.logger:
                        self.logger.log(
                            f"🔌 [WS-MANAGER] Failed to parse WebSocket message: {exc}",
                            "ERROR",
                        )
                except Exception as exc:
                    if self.logger:
                        self.logger.log(
                            f"🔌 [WS-MANAGER] Error handling WebSocket message: {exc}",
                            "ERROR",
                        )

        except websockets.exceptions.ConnectionClosed as exc:
            if self.logger:
                self.logger.log(
                    f"🔌 [WS-MANAGER] WebSocket connection closed in listener: {exc}",
                    "WARNING",
                )

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket messages."""
        stream = data.get("stream", "")
        payload = data.get("data", {})

        is_trade_message = stream.endswith("@trade") or (
            isinstance(payload, dict) and payload.get("e") == "trade"
        )

        if not is_trade_message:
            if self.logger:
                self.logger.log(f"🔍 Ignoring non-trade message: {data}", "DEBUG")
            return

        if self.trade_update_callback:
            await self.trade_update_callback(payload)

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            if self.logger:
                self.logger.log("WebSocket disconnected", "INFO")

    def set_logger(self, logger):
        """Set the logger instance."""
        self.logger = logger


class BinanceMockClient(BaseExchangeClient):
    """Binance mock exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Binance mock client."""
        super().__init__(config)

        self.logger = TradingLogger(
            exchange="binance_mock", ticker=self.config.ticker, log_to_console=False
        )

        self._http = requests.Session()
        self.rest_base_url = BINANCE_REST_URL

        self.virtual_orders: Dict[str, VirtualOrder] = {}
        self.completed_orders: Dict[str, VirtualOrder] = {}
        self._order_update_handler = None
        self.positions: Dict[str, Decimal] = {}
        self.quote_balances: Dict[str, Decimal] = {}
        self.initial_quote_balance = Decimal("100000")
        self._state_lock = asyncio.Lock()
        self._ws_task: Optional[asyncio.Task] = None

        self.ws_manager: Optional[BinanceMockWebSocketManager] = None

        self.subscription_confirmed = False
        self.first_trade_received = False

        if not hasattr(self.config, "symbol_suffix"):
            setattr(self.config, "symbol_suffix", DEFAULT_SYMBOL_SUFFIX)

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

            self.completed_orders[virtual_order.order_id] = virtual_order
            del self.virtual_orders[virtual_order.order_id]

        payload = self._build_order_update_payload(
            virtual_order, virtual_order.status, fill_price
        )
        self._emit_order_update(payload)

    def _validate_config(self) -> None:
        """Validate mock configuration (no real credentials needed)."""
        if not getattr(self.config, "ticker", None):
            raise ValueError("Ticker must be specified in config")

    async def connect(self) -> None:
        """Connect to Binance WebSocket for trade data."""
        self.logger.log("🔌 [MOCK] *** BINANCE MOCK EXCHANGE CONNECT CALLED ***", "INFO")
        print("🔌 [MOCK] *** BINANCE MOCK EXCHANGE CONNECT CALLED ***")
        self.logger.log(f"🔌 [MOCK] Contract ID: '{self.config.contract_id}'", "INFO")
        print(f"🔌 [MOCK] Contract ID: '{self.config.contract_id}'")
        self.logger.log(f"🔌 [MOCK] Ticker: '{self.config.ticker}'", "INFO")
        print(f"🔌 [MOCK] Ticker: '{self.config.ticker}'")

        if not self.config.contract_id:
            self.logger.log("🔌 [MOCK] WARNING: Contract ID is empty!", "WARNING")
            print("🔌 [MOCK] WARNING: Contract ID is empty!")
            return

        self.logger.log("🔌 [MOCK] Creating WebSocket manager...", "INFO")
        print("🔌 [MOCK] Creating WebSocket manager...")
        self.ws_manager = BinanceMockWebSocketManager(
            symbol=self.config.contract_id,
            trade_update_callback=self._handle_trade_update,
        )
        self.ws_manager.set_logger(self.logger)

        try:
            self.logger.log("🔌 [MOCK] Creating background WebSocket task...", "INFO")
            print("🔌 [MOCK] Creating background WebSocket task...")

            async def websocket_task_wrapper():
                try:
                    await self.ws_manager.connect()
                except Exception as exc:
                    self.logger.log(
                        f"🔌 [MOCK] WebSocket wrapper caught exception: {exc}", "ERROR"
                    )
                    print(f"🔌 [MOCK] WebSocket wrapper caught exception: {exc}")
                    import traceback

                    self.logger.log(traceback.format_exc(), "ERROR")
                    traceback.print_exc()
                    raise

            task = asyncio.create_task(websocket_task_wrapper())
            self._ws_task = task
            self.logger.log(
                f"🔌 [MOCK] WebSocket task created: {task.get_name()}", "INFO"
            )
            print(f"🔌 [MOCK] WebSocket task created: {task.get_name()}")

            self.logger.log(
                "🔌 [MOCK] Waiting for WebSocket connection to establish...", "INFO"
            )
            print("🔌 [MOCK] Waiting for WebSocket connection to establish...")

            for i in range(5):
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
                        print(
                            f"🔌 [MOCK] WebSocket task failed: {task.exception()}"
                        )
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

            if not task.done():
                self.logger.log("🔌 [MOCK] *** WEBSOCKET SETUP COMPLETED ***", "INFO")
                print("🔌 [MOCK] *** WEBSOCKET SETUP COMPLETED ***")
            else:
                self.logger.log("🔌 [MOCK] Task finished unexpectedly", "ERROR")
                print("🔌 [MOCK] Task finished unexpectedly")

        except Exception as exc:
            self.logger.log(
                f"🔌 [MOCK] Error connecting to Binance WebSocket: {exc}", "ERROR"
            )
            print(f"🔌 [MOCK] Error connecting to Binance WebSocket: {exc}")
            import traceback

            self.logger.log(traceback.format_exc(), "ERROR")
            traceback.print_exc()
            raise

    async def disconnect(self) -> None:
        """Disconnect from Binance."""
        try:
            self.logger.log("🔍 Starting disconnect process...", "INFO")

            if self.ws_manager:
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

            self._http.close()
            self.logger.log("🔍 Disconnect completed", "INFO")

        except Exception as exc:
            if hasattr(self, "logger"):
                self.logger.log(f"Error during Binance disconnect: {exc}", "ERROR")
            else:
                print(f"Error during Binance disconnect: {exc}")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "binance_mock"

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
                self.logger.log(f"🔍 Unknown trade data format: {trade_data}", "WARNING")
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

        except Exception as exc:
            self.logger.log(f"🔍 ❌ Error handling trade update: {exc}", "ERROR")
            import traceback

            self.logger.log(traceback.format_exc(), "DEBUG")

    async def get_order_price(self, direction: str) -> Decimal:
        """Get the price of an order using current market data."""
        best_bid, best_ask = await self.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0:
            self.logger.log("Invalid bid/ask prices", "ERROR")
            raise ValueError("Invalid bid/ask prices")

        if direction == "buy":
            order_price = best_ask - self.config.tick_size
        else:
            order_price = best_bid + self.config.tick_size
        return self.round_to_tick(order_price)

    def _rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.rest_base_url}{path}"
        response = self._http.get(url, params=params, timeout=5)
        response.raise_for_status()
        return response.json()

    @query_retry(default_return=(Decimal("0"), Decimal("0")))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        """Get current BBO prices from Binance."""
        try:
            data = self._rest_get(BOOK_TICKER_PATH, params={"symbol": contract_id})
            best_bid = Decimal(data.get("bidPrice", "0"))
            best_ask = Decimal(data.get("askPrice", "0"))
            return best_bid, best_ask
        except Exception as exc:
            self.logger.log(f"Failed to fetch Binance BBO: {exc}", "ERROR")
            raise

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
                return OrderResult(success=False, error_message="Invalid bid/ask prices")

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

        except Exception as exc:
            self.logger.log(f"Error placing mock open order: {exc}", "ERROR")
            return OrderResult(success=False, error_message=str(exc))

    async def place_market_order(
        self, contract_id: str, quantity: Decimal, direction: str
    ) -> OrderResult:
        """Place a mock market order (immediate fill at current market price)."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message="Invalid bid/ask prices")

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

            payload = self._build_order_update_payload(market_order, "FILLED", fill_price)
            self._emit_order_update(payload)

            return OrderResult(
                success=True,
                order_id=order_id,
                side=direction,
                size=quantity,
                price=fill_price,
                status="FILLED",
            )

        except Exception as exc:
            self.logger.log(f"Error placing mock market order: {exc}", "ERROR")
            return OrderResult(success=False, error_message=str(exc))

    async def place_close_order(
        self, contract_id: str, quantity: Decimal, price: Decimal, side: str
    ) -> OrderResult:
        """Place a mock close order that mirrors maker-style limits."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message="Invalid bid/ask prices")

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

        except Exception as exc:
            self.logger.log(f"Error placing mock close order: {exc}", "ERROR")
            return OrderResult(success=False, error_message=str(exc))

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

        except Exception as exc:
            self.logger.log(f"Error canceling mock order: {exc}", "ERROR")
            return OrderResult(success=False, error_message=str(exc))

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

    @query_retry(default_return=Decimal("0"))
    async def get_account_positions(self) -> Decimal:
        """Mock positions - always return tracked position."""
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

    def _derive_symbol(self, ticker: str) -> str:
        if not ticker:
            raise ValueError("Ticker is empty")

        symbol = ticker.upper().replace("-", "").replace("_", "")
        if symbol.endswith("PERP"):
            symbol = symbol[:-4]

        suffix = getattr(self.config, "symbol_suffix", DEFAULT_SYMBOL_SUFFIX).upper()
        if not symbol.endswith(suffix):
            symbol = f"{symbol}{suffix}"

        return symbol

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract attributes for the ticker."""
        self.logger.log("🔌 [MOCK] *** GET CONTRACT ATTRIBUTES CALLED ***", "INFO")

        ticker = self.config.ticker
        if len(ticker) == 0:
            self.logger.log("🔌 [MOCK] Ticker is empty", "ERROR")
            raise ValueError("Ticker is empty")

        target_symbol = self._derive_symbol(ticker)
        self.logger.log(
            f"🔌 [MOCK] Requesting Binance exchangeInfo for symbol: '{target_symbol}'",
            "INFO",
        )

        try:
            data = self._rest_get(EXCHANGE_INFO_PATH, params={"symbol": target_symbol})
        except Exception as exc:
            self.logger.log(f"🔌 [MOCK] Error getting exchange info: {exc}", "ERROR")
            raise

        symbols = data.get("symbols", [])
        if not symbols:
            raise ValueError(f"Failed to get contract info for ticker: {ticker}")

        symbol_info = next(
            (item for item in symbols if item.get("symbol") == target_symbol), None
        )
        if not symbol_info:
            available = [item.get("symbol") for item in symbols[:5]]
            self.logger.log(
                f"Target symbol {target_symbol} not found in Binance response. Sample entries: {available}",
                "ERROR",
            )
            raise ValueError(f"Failed to find symbol {target_symbol} in Binance exchange info")

        filters = {f.get("filterType"): f for f in symbol_info.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})

        tick_size = Decimal(price_filter.get("tickSize", "0"))
        min_quantity = Decimal(lot_filter.get("minQty", "0"))

        self.config.contract_id = symbol_info.get("symbol")
        self.config.tick_size = tick_size

        self.logger.log(
            f"🔌 [MOCK] Found Binance contract: {self.config.contract_id} (tick_size={tick_size}, min_qty={min_quantity})",
            "INFO",
        )

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
