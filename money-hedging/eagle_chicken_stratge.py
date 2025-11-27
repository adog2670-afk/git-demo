#!/usr/bin/env python3
"""
Eagle Chicken Strategy implementation.

This strategy coordinates a maker account and multiple taker accounts to perform
low-risk market making/hedging cycles following the specification in
`eagle_chicken_strategy_design.md`. The overall structure mirrors the modular
patterns used in `trading_bot.py` while introducing multi-account orchestration,
partial-fill hedging, randomized holding periods, and periodic cleanup rounds.
"""

from __future__ import annotations

# Force unbuffered output for nohup compatibility
import datetime
import sys
import os

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Also set environment variables for unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"

import asyncio
import contextlib
import os
import random
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from exchanges import BaseExchangeClient, ExchangeFactory
from helpers import TradingLogger

# ---------------------------------------------------------------------------
# Configuration data classes
# ---------------------------------------------------------------------------


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Optional[str] = None
    console: bool = True


@dataclass
class AccountConfig:
    name: str
    exchange: str
    credentials: Dict[str, Any]
    symbols: Dict[str, str] = field(default_factory=dict)
    ticker: Optional[str] = None


@dataclass
class StrategyConfig:
    ticker: str
    quantity: Decimal
    maker_tick_offset: int
    accounts: Dict[str, AccountConfig]
    maker_account: str
    no_chicken_mode: bool
    taker_accounts: List[str]
    min_holding_time: int
    max_holding_time: int
    cleanup_rounds: int
    order_timeout: float
    cancel_timeout: float
    logging: LoggingConfig
    hedging_ratio: Decimal = Decimal("1")  # 对冲比例，默认1:1对冲
    target_volume: Optional[Decimal] = None
    max_play_rounds: Optional[int] = None
    maker_side: str = "buy"
    price_pause_threshold: Optional[Decimal] = None


@dataclass
class AccountTradingConfig:
    """Lightweight configuration passed into exchange clients."""

    name: str
    exchange: str
    ticker: str
    quantity: Decimal
    direction: str
    contract_id: str = ""
    tick_size: Decimal = Decimal("0")
    take_profit: Decimal = Decimal("0")
    max_orders: int = 1
    wait_time: int = 0
    grid_step: Decimal = Decimal("0")
    stop_price: Decimal = Decimal("-1")
    pause_price: Decimal = Decimal("-1")
    boost_mode: bool = False

    @property
    def close_order_side(self) -> str:
        return "buy" if self.direction == "sell" else "sell"


# ---------------------------------------------------------------------------
# Helper structures
# ---------------------------------------------------------------------------


class OrderTracker:
    """Track order state via WebSocket updates."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        account: "AccountRuntime",
        order_id: str,
        expected_size: Decimal,
        on_fill: Callable[[Decimal, Decimal, Dict[str, Any]], asyncio.Future],
        logger: TradingLogger,
    ):
        self.loop = loop
        self.account = account
        self.order_id = order_id
        self.expected_size = expected_size
        self.on_fill_callback = on_fill
        self.logger = logger

        self.status: str = "PENDING"
        self.total_filled: Decimal = Decimal("0")
        self.average_price: Decimal = Decimal("0")
        self._last_update_ts = time.time()

        self.fill_event = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.failure_event = asyncio.Event()

    def handle_update(self, update: Dict[str, Any]) -> None:
        """Process an order update in a thread-safe way."""

        def _process():
            filled_size = Decimal(
                str(update.get("filled_size", update.get("size", 0) or 0))
            )
            price_raw = update.get("price")
            price = Decimal(str(price_raw)) if price_raw is not None else Decimal("0")
            status = str(update.get("status", "")).upper()
            delta = filled_size - self.total_filled

            self._last_update_ts = time.time()

            if delta > 0:
                self.total_filled = filled_size
                # Fire partial fill callback for every incremental fill.
                task = self.loop.create_task(
                    self.on_fill_callback(delta, price, update)
                )
                task.add_done_callback(self._log_callback_exception)

            if status == "FILLED":
                self.status = "FILLED"
                if price > 0:
                    self.average_price = price
                self.fill_event.set()
            elif status == "CANCELED":
                self.status = "CANCELED"
                self.cancel_event.set()
            elif status == "FAILED":
                self.status = "FAILED"
                self.failure_event.set()

        self.loop.call_soon_threadsafe(_process)

    def _log_callback_exception(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.logger.log(
                f"Order tracker callback raised exception for {self.order_id}: {exc}",
                "ERROR",
            )


@dataclass
class AccountRuntime:
    name: str
    exchange: str
    config: AccountTradingConfig
    client: BaseExchangeClient
    order_trackers: Dict[str, OrderTracker] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strategy implementation
# ---------------------------------------------------------------------------


class EagleChickenStrategy:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.strategy_config = self._load_config(self.config_path)
        self.logger = TradingLogger(
            exchange="eagle_chicken",
            ticker=self.strategy_config.ticker,
            log_to_console=self.strategy_config.logging.console,
        )

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.shutdown_event = asyncio.Event()
        self.shutdown_requested = False
        self.graceful_shutdown_in_progress = False  # 双击Ctrl+C标志

        self.maker: Optional[AccountRuntime] = None
        self.takers: List[AccountRuntime] = []
        self.accounts: Dict[str, AccountRuntime] = {}

        # Position tracking
        self.maker_position: Decimal = Decimal("0")
        self.taker_positions: Dict[str, Decimal] = {}

        # Round counters
        self.completed_rounds = 0
        self.cleanup_counter = 0
        self.cleanup_rounds = 0

        # Taker account statistics tracking
        self.taker_initial_stats: Dict[str, Dict[str, Any]] = {}
        self.taker_round_stats: Dict[str, Dict[str, Any]] = {}
        self.taker_trade_volumes: Dict[str, Decimal] = {}
        self.taker_last_cleanup_round = 0

    # ------------------------------------------------------------------
    # Configuration loading & setup
    # ------------------------------------------------------------------

    def _load_config(self, path: Path) -> StrategyConfig:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError("Invalid configuration file")

        logging_cfg = raw.get("logging", {}) or {}
        logging_config = LoggingConfig(
            level=logging_cfg.get("level", "INFO"),
            file=logging_cfg.get("file"),
            console=bool(logging_cfg.get("console", True)),
        )

        account_entries = raw.get("accounts", [])
        if not account_entries:
            raise ValueError("No accounts configured")

        accounts: Dict[str, AccountConfig] = {}
        for entry in account_entries:
            name = entry.get("name")
            if not name:
                raise ValueError("Each account must define a name")
            accounts[name] = AccountConfig(
                name=name,
                exchange=entry.get("exchange"),
                credentials=entry.get("credentials", {}),
                symbols=entry.get("symbols", {}) or {},
                ticker=entry.get("ticker"),
            )

        maker_account = raw.get("maker_account")
        if not maker_account:
            raise ValueError("maker_account must be provided in configuration")
        no_chicken_mode = bool(raw.get("no_chicken_mode", False))
        taker_accounts = raw.get("taker_accounts", [])
        if not taker_accounts:
            no_chicken_mode = True
            # raise ValueError("At least one taker account must be specified")

        maker_side = raw.get("maker_side", "buy").lower()
        if maker_side not in ("buy", "sell"):
            raise ValueError("maker_side must be either 'buy' or 'sell'")

        max_rounds = raw.get("max_play_rounds")
        if max_rounds is not None:
            max_rounds = int(max_rounds)

        strategy_config = StrategyConfig(
            ticker=str(raw.get("ticker", "")).upper(),
            quantity=Decimal(str(raw.get("quantity", "0"))),
            maker_tick_offset=int(raw.get("maker_tick_offset", 1)),
            accounts=accounts,
            maker_account=str(maker_account),
            no_chicken_mode=no_chicken_mode,
            taker_accounts=[str(v) for v in taker_accounts],
            min_holding_time=int(raw.get("min_holding_time", 0)),
            max_holding_time=int(raw.get("max_holding_time", 0)),
            cleanup_rounds=int(raw.get("cleanup_rounds", 1)),
            order_timeout=float(raw.get("order_timeout", 3)),
            cancel_timeout=float(raw.get("cancel_timeout", 5)),
            logging=logging_config,
            hedging_ratio=Decimal(str(raw.get("hedging_ratio", "1"))),
            target_volume=(
                Decimal(str(raw["target_volume"]))
                if "target_volume" in raw and raw["target_volume"] is not None
                else None
            ),
            max_play_rounds=max_rounds,
            maker_side=maker_side,
            price_pause_threshold=(
                Decimal(str(raw["price_pause_threshold"]))
                if "price_pause_threshold" in raw
                and raw["price_pause_threshold"] is not None
                else None
            ),
        )

        if strategy_config.quantity <= 0:
            raise ValueError("quantity must be positive")

        if strategy_config.maker_account not in accounts:
            raise ValueError(
                f"Maker account '{strategy_config.maker_account}' missing from accounts list"
            )

        for taker_name in strategy_config.taker_accounts:
            if taker_name not in accounts:
                raise ValueError(
                    f"Taker account '{taker_name}' missing from accounts list"
                )

        return strategy_config

    def _resolve_account_symbol(self, account: AccountConfig) -> str:
        if account.ticker:
            return account.ticker
        symbol = account.symbols.get(self.strategy_config.ticker)
        if symbol:
            return symbol
        return self.strategy_config.ticker

    @contextlib.contextmanager
    def _temporary_env(self, mapping: Dict[str, Any]):
        """Temporarily set environment variables for account credentials."""
        previous: Dict[str, Optional[str]] = {}
        try:
            for key, value in mapping.items():
                key_str = str(key)
                previous[key_str] = os.environ.get(key_str)
                os.environ[key_str] = str(value)
            yield
        finally:
            for key, value in mapping.items():
                key_str = str(key)
                original = previous.get(key_str)
                if original is None:
                    os.environ.pop(key_str, None)
                else:
                    os.environ[key_str] = original

    async def _initialize_accounts(self):
        self.loop = asyncio.get_running_loop()

        for name, account_cfg in self.strategy_config.accounts.items():
            symbol = self._resolve_account_symbol(account_cfg)
            direction = (
                self.strategy_config.maker_side
                if name == self.strategy_config.maker_account
                else ("sell" if self.strategy_config.maker_side == "buy" else "buy")
            )

            trading_config = AccountTradingConfig(
                name=name,
                exchange=account_cfg.exchange.lower(),
                ticker=symbol.upper(),
                quantity=self.strategy_config.quantity,
                direction=direction,
            )

            credential_env = dict(account_cfg.credentials)
            credential_env["ACCOUNT_NAME"] = name

            with self._temporary_env(credential_env):
                client = ExchangeFactory.create_exchange(
                    account_cfg.exchange, trading_config
                )

            runtime = AccountRuntime(
                name=name,
                exchange=account_cfg.exchange.lower(),
                config=trading_config,
                client=client,
            )

            self.accounts[name] = runtime
            if name == self.strategy_config.maker_account:
                self.maker = runtime
            elif name in self.strategy_config.taker_accounts:
                self.takers.append(runtime)
                self.taker_positions[name] = Decimal("0")

        if not self.maker:
            raise RuntimeError("Maker account failed to initialize")

        # Establish WebSocket handlers and warm up exchange metadata.
        init_tasks = [
            self._prepare_account(account) for account in self.accounts.values()
        ]
        await asyncio.gather(*init_tasks)

        # Record initial taker account statistics
        await self._record_initial_taker_stats()

    async def _prepare_account(self, account: AccountRuntime):
        config = account.config
        client = account.client

        contract_id, tick_size = await client.get_contract_attributes()
        config.contract_id = contract_id
        config.tick_size = tick_size

        # Register WebSocket order update handlers before connecting.
        if account.exchange == "lighter":

            def handler(order_updates):
                updates = (
                    order_updates
                    if isinstance(order_updates, list)
                    else [order_updates]
                )
                for raw in updates:
                    order_id = str(raw.get("order_index") or raw.get("order_id") or "")
                    if not order_id:
                        continue
                    normalized = {
                        "order_id": order_id,
                        "status": raw.get("status"),
                        "price": raw.get("price"),
                        "filled_size": raw.get("filled_base_amount"),
                        "size": raw.get("initial_base_amount"),
                    }
                    tracker = account.order_trackers.get(order_id)
                    if tracker:
                        tracker.handle_update(normalized)

            client.setup_order_update_handler(handler)
        else:

            def handler(update):
                order_id_raw = update.get("order_id") or update.get("orderId")
                if not order_id_raw:
                    return
                order_id = str(order_id_raw)
                tracker = account.order_trackers.get(order_id)
                if tracker:
                    tracker.handle_update(update)

            client.setup_order_update_handler(handler)

        with self._temporary_env({"ACCOUNT_NAME": account.name}):
            await client.connect()
        await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Order handling helpers
    # ------------------------------------------------------------------

    def _register_tracker(
        self,
        account: AccountRuntime,
        order_id: str,
        expected_size: Decimal,
        on_fill: Callable[[Decimal, Decimal, Dict[str, Any]], asyncio.Future],
    ) -> OrderTracker:
        tracker = OrderTracker(
            self.loop, account, order_id, expected_size, on_fill, self.logger
        )
        account.order_trackers[order_id] = tracker
        return tracker

    def _unregister_tracker(self, account: AccountRuntime, order_id: str):
        account.order_trackers.pop(order_id, None)

    async def _await_order_completion(self, tracker: OrderTracker) -> str:
        account = tracker.account

        try:
            await asyncio.wait_for(
                tracker.fill_event.wait(), timeout=self.strategy_config.order_timeout
            )
            return tracker.status
        except asyncio.TimeoutError:
            remaining = tracker.expected_size - tracker.total_filled
            if remaining > 0:
                self.logger.log(
                    f"[{account.name}] Order {tracker.order_id} timeout, cancelling remaining {remaining}",
                    "WARNING",
                )
                self.logger.log(
                    f"[{account.name}] Initiating cancel for order {tracker.order_id}",
                    "INFO",
                )
                cancel_result = await account.client.cancel_order(tracker.order_id)
                if not cancel_result.success:
                    self.logger.log(
                        f"[{account.name}] Failed to cancel order {tracker.order_id}: {cancel_result.error_message}",
                        "ERROR",
                    )
                else:
                    self.logger.log(
                        f"[{account.name}] Success to cancel order {tracker.order_id}",
                        "INFO",
                    )
                tracker.status = "CANCELED"
            return tracker.status

    async def _maker_price(self, side: str) -> Decimal:
        if not self.maker:
            raise RuntimeError("Maker account not initialized")

        best_bid, best_ask = await self.maker.client.fetch_bbo_prices(
            self.maker.config.contract_id
        )
        tick = self.maker.config.tick_size
        offset = tick * self.strategy_config.maker_tick_offset

        if side == "buy":
            price = best_ask - offset
            if price <= 0:
                price = best_ask - tick
        else:
            price = best_bid + offset
            if price <= 0:
                price = best_bid + tick

        return self.maker.client.round_to_tick(price)

    async def _handle_maker_fill(self, delta: Decimal, price: Decimal, stage: str):
        direction = 1 if self.strategy_config.maker_side == "buy" else -1
        signed_delta = delta * direction if stage == "open" else delta * -direction

        if stage == "open":
            self.maker_position += signed_delta
        else:
            self.maker_position += signed_delta

        if not self.strategy_config.no_chicken_mode:
            taker_side = (
                "sell"
                if stage == "open" and self.strategy_config.maker_side == "buy"
                else None
            )
            if stage == "open" and self.strategy_config.maker_side == "sell":
                taker_side = "buy"
            if stage == "close" and self.strategy_config.maker_side == "buy":
                taker_side = "buy"
            if stage == "close" and self.strategy_config.maker_side == "sell":
                taker_side = "sell"

            if taker_side is None:
                raise RuntimeError("Unable to resolve taker side for hedging")

            await self._execute_taker_hedge(delta, taker_side, stage)

        self.logger.log(
            f"[MAKER-FILLED] Stage:{stage} {delta} @ {price} | maker_position={self.maker_position}",
            "INFO",
        )

        # await self._validate_positions()

    async def _execute_taker_hedge(self, quantity: Decimal, side: str, stage: str):
        taker = random.choice(self.takers)
        config = taker.config

        # 应用对冲比例
        hedge_quantity = quantity * self.strategy_config.hedging_ratio

        # 检查是否需要反向对冲 (负数对冲比例)
        if self.strategy_config.hedging_ratio < Decimal("0"):
            # 反向对冲：当对冲比例为负数时，taker开相同方向的仓
            actual_side = side  # 保持和maker相同的方向
            hedge_quantity = abs(hedge_quantity)  # 确保数量为正数
            self.logger.log(
                f"[TAKER-REVERSE-HEDGE] {taker.name} reverse hedging activated - maker {side}, taker also {side} (negative ratio: {self.strategy_config.hedging_ratio})",
                "INFO",
            )
        else:
            # 正常对冲：保持原有逻辑
            actual_side = side

        self.logger.log(
            f"[TAKER-HEDGE] {taker.name} placing market order side={actual_side} qty={hedge_quantity} (original={quantity}, ratio={self.strategy_config.hedging_ratio}) stage={stage}",
            "INFO",
        )
        result = await taker.client.place_market_order(
            config.contract_id, hedge_quantity, actual_side
        )
        if not result.success:
            raise RuntimeError(
                f"[{taker.name}] Hedge market order failed: {result.error_message}"
            )
        self.logger.log(
            f"[TAKER-HEDGE-SUCCESS] {taker.name} placing market order side={actual_side} qty={hedge_quantity} stage={stage}",
            "INFO",
        )
        signed = hedge_quantity if actual_side == "buy" else -hedge_quantity
        self.taker_positions[taker.name] = (
            self.taker_positions.get(taker.name, Decimal("0")) + signed
        )

        # Update trade volume statistics (calculate USD value)
        # Note: For market orders, we use approximate execution price
        current_price = await self._get_current_price()
        usd_volume = hedge_quantity * current_price
        self._update_trade_volume(taker.name, usd_volume)
        self.logger.log(
            f"[STATS-UPDATE] Updated trade volume for {taker.name}: +{usd_volume:.2f} USD (quantity: {hedge_quantity}, price: {current_price:.2f}), total: {self.taker_trade_volumes[taker.name]:.2f} USD",
            "INFO",
        )

        self.logger.log(
            f"Taker hedge [{taker.name}] stage={stage} side={side} qty={hedge_quantity} | taker_position={self.taker_positions[taker.name]}",
            "INFO",
        )

    async def _validate_positions(self):
        net_position = self.maker_position + sum(self.taker_positions.values())

        # 当hedging_ratio不为1时，预期的净仓位不为0
        # 预期净仓位 = maker_position * (1 - hedging_ratio)
        expected_net_position = self.maker_position * (
            Decimal("1") - self.strategy_config.hedging_ratio
        )
        position_diff = abs(net_position - expected_net_position)

        if position_diff > Decimal("1e-6"):
            self.logger.log(
                f"Position mismatch detected | maker: {self.maker_position} takers: {self.taker_positions} net: {net_position} expected_net: {expected_net_position} diff: {position_diff}",
                "WARNING",
            )

    # ------------------------------------------------------------------
    # Taker account statistics methods
    # ------------------------------------------------------------------

    async def _record_initial_taker_stats(self):
        """Record initial statistics for all taker accounts."""
        self.logger.log("Recording initial taker account statistics...", "INFO")

        for taker in self.takers:
            try:
                # Get initial balance and position
                balance = await taker.client.get_account_balance()
                position = await taker.client.get_account_positions()

                # Get current price for position valuation
                current_price = await self._get_current_price()

                self.taker_initial_stats[taker.name] = {
                    "initial_timestamp": datetime.datetime.now(),
                    "initial_balance": balance,
                    "initial_position": position,
                    "initial_price": current_price,
                    "initial_position_value": position * current_price,
                    "initial_total_value": balance + (position * current_price),
                }
                self.taker_round_stats[taker.name] = {
                    "round_end_value": self.taker_initial_stats[taker.name][
                        "initial_total_value"
                    ],
                    "round_pnl": Decimal("0"),
                    "total_pnl": Decimal("0"),
                    "current_position": Decimal("0"),
                    "current_price": Decimal("0"),
                    "current_total_value": Decimal("0"),
                    "trade_volume": Decimal("0"),
                }

                self.taker_trade_volumes[taker.name] = Decimal("0")

                self.logger.log(
                    f"Initial stats for {taker.name}: balance={balance}, position={position}, price={current_price}",
                    "INFO",
                )
                self.logger.log(
                    f"[INIT-STATS] {taker.name}: trade_volume initialized to 0", "DEBUG"
                )

            except Exception as e:
                self.logger.log(
                    f"Failed to record initial stats for {taker.name}: {e}", "ERROR"
                )
                # Set default values
                self.taker_initial_stats[taker.name] = {
                    "initial_timestamp": datetime.datetime.now(),
                    "initial_balance": Decimal("0"),
                    "initial_position": Decimal("0"),
                    "initial_price": Decimal("0"),
                    "initial_position_value": Decimal("0"),
                    "initial_total_value": Decimal("0"),
                }
                self.taker_round_stats[taker.name] = {
                    "round_end_value": Decimal("0"),
                    "round_pnl": Decimal("0"),
                    "total_pnl": Decimal("0"),
                    "current_position": Decimal("0"),
                    "current_price": Decimal("0"),
                    "current_total_value": Decimal("0"),
                    "trade_volume": Decimal("0"),
                }
                self.taker_trade_volumes[taker.name] = Decimal("0")

    async def _get_current_price(self) -> Decimal:
        """Get current market price for position valuation."""
        try:
            if self.maker:
                best_bid, best_ask = await self.maker.client.fetch_bbo_prices(
                    self.maker.config.contract_id
                )
                # Use mid-price for valuation
                return (best_bid + best_ask) / Decimal("2")
            else:
                return Decimal("0")
        except Exception:
            return Decimal("0")

    async def _calculate_taker_pnl(self, taker: AccountRuntime) -> Dict[str, Decimal]:
        """Calculate P&L for a taker account."""
        try:
            # Get current balance and position
            current_balance = await taker.client.get_account_balance()
            current_position = await taker.client.get_account_positions()
            current_price = await self._get_current_price()

            initial_stats = self.taker_initial_stats.get(taker.name, {})
            initial_total_value = initial_stats.get("initial_total_value", Decimal("0"))

            # Calculate current position value
            current_position_value = current_position * current_price
            current_total_value = current_balance + current_position_value

            # Calculate P&L components
            round_pnl = (
                current_total_value
                - self.taker_round_stats[taker.name]["round_end_value"]
            )
            total_pnl = current_total_value - initial_total_value

            return {
                "round_end_value": current_total_value,
                "round_pnl": round_pnl,
                "total_pnl": total_pnl,
                "current_position": current_position,
                "current_price": current_price,
                "current_total_value": current_total_value,
                "trade_volume": self.taker_trade_volumes.get(taker.name, Decimal("0")),
            }

        except Exception as e:
            self.logger.log(f"Error calculating P&L for {taker.name}: {e}", "ERROR")
            return {
                "round_pnl": Decimal("0"),
                "total_pnl": Decimal("0"),
                "current_position": Decimal("0"),
                "current_price": Decimal("0"),
                "current_total_value": Decimal("0"),
                "trade_volume": Decimal("0"),
            }

    def _update_trade_volume(self, taker_name: str, usd_volume: Decimal):
        """Update trade volume for a taker account (in USD)."""
        if taker_name in self.taker_trade_volumes:
            old_volume = self.taker_trade_volumes[taker_name]
            self.taker_trade_volumes[taker_name] += usd_volume
            self.logger.log(
                f"[TRADE-VOLUME] {taker_name}: ${old_volume:.2f} -> ${self.taker_trade_volumes[taker_name]:.2f} (+${usd_volume:.2f})",
                "DEBUG",
            )
        else:
            self.logger.log(
                f"[TRADE-VOLUME] Warning: {taker_name} not found in trade volume tracking",
                "WARNING",
            )

    def _total_trade_volume(self) -> Decimal:
        """Return the aggregated USD trade volume across all takers."""
        if self.taker_round_stats:
            return sum(
                (stats.get("trade_volume", Decimal("0")) for stats in self.taker_round_stats.values()),
                Decimal("0"),
            )
        return sum(self.taker_trade_volumes.values(), Decimal("0"))

    def _check_target_volume_met(self) -> bool:
        """Return True when the configured target volume has been reached."""
        target = self.strategy_config.target_volume
        if target is None or target <= Decimal("0"):
            return False

        total_volume = self._total_trade_volume()
        if total_volume >= target:
            self.logger.log(
                f"Target trade volume reached: ${total_volume:.2f} / ${target:.2f}",
                "INFO",
            )
            print("Target volume reached", flush=True)
            return True
        else:
            print(f"Target volume {target} not reached by {total_volume}", flush=True)
        return False

    def _print_taker_stats_table(self):
        """Print a formatted table with taker statistics."""
        if not self.taker_round_stats:
            return

        print("\n" + "=" * 100, flush=True)
        print(
            f"📊 TAKER ACCOUNT STATISTICS - Cleanup Round {self.cleanup_rounds}",
            flush=True,
        )
        print("=" * 100, flush=True)

        # Table header
        header = f"{'Account':<15} {'Trade Volume':<15} {'Volume/hr':<15} {'Round P&L':<15} {'Total P&L':<15} {'Account Value':<15}"
        print(header, flush=True)
        print("-" * 100, flush=True)

        # Table rows
        total_volume = Decimal("0")
        total_pnl = Decimal("0")

        for account_name, stats in self.taker_round_stats.items():
            volume = stats["trade_volume"]
            round_pnl = stats["round_pnl"]
            total_pnl_account = stats["total_pnl"]
            current_total_value = stats["current_total_value"]

            total_volume += volume
            total_pnl += total_pnl_account

            seconds_passed = (
                datetime.datetime.now()
                - self.taker_initial_stats[account_name]["initial_timestamp"]
            ).total_seconds()
            hours_passed = seconds_passed / 3600

            # Format numbers (USD for volume, standard format for others)
            volume_str = f"${volume:.2f}"
            volume_speed_str = f"${volume / Decimal(hours_passed):.2f}/hr"
            round_pnl_str = f"{round_pnl:+.6f}"
            total_pnl_str = f"{total_pnl_account:+.6f}"
            current_total_value_str = f"{current_total_value:.6f}"

            # Color coding for P&L (green for positive, red for negative)
            if total_pnl_account >= 0:
                total_pnl_str = f"🟢{total_pnl_str}"
            else:
                total_pnl_str = f"🔴{total_pnl_str}"

            if round_pnl >= 0:
                round_pnl_str = f"🟢{round_pnl_str}"
            else:
                round_pnl_str = f"🔴{round_pnl_str}"

            row = f"{account_name:<15} {volume_str:<15} {volume_speed_str:<15} {round_pnl_str:<15} {total_pnl_str:<15} {current_total_value_str:<15}"
            print(row, flush=True)

        print("-" * 100, flush=True)

        # Summary row
        summary_total_pnl_str = f"{total_pnl:+.6f}"
        if total_pnl >= 0:
            summary_total_pnl_str = f"🟢{summary_total_pnl_str}"
        else:
            summary_total_pnl_str = f"🔴{summary_total_pnl_str}"

        summary = f"{'TOTAL':<15} ${total_volume:.2f}{'':<9}  {'':<15} {'':<15} {summary_total_pnl_str:<15} {'':<15}"
        print(summary, flush=True)
        print("=" * 100 + "\n", flush=True)

    async def _calculate_taker_statistics(self):
        self.logger.log("Calculating taker account statistics...", "INFO")
        for taker in self.takers:
            stats = await self._calculate_taker_pnl(taker)
            self.taker_round_stats[taker.name] = stats
        return

    def _calculate_maker_side_for_new_round(self) -> str:
        """Keep or flip maker direction based on previous taker performance."""
        current_side = self.strategy_config.maker_side
        if self.takers[0] is None:
            self.logger.log("need at least one taker", "ERROR")
        taker_round_stats = self.taker_round_stats[self.takers[0].name]
        if taker_round_stats is None:
            self.logger.log(
                "[MAKER-DIRECTION] No taker_round_stats data, keep maker side with "
                f"{current_side}",
                "INFO",
            )
            return current_side
        if taker_round_stats["round_pnl"] is None:
            new_side = "sell" if current_side == "buy" else "buy"
            self.strategy_config.maker_side = new_side
            self.logger.log(
                "[MAKER-DIRECTION] No last round taker P&L data, flip maker side to "
                f"{new_side}",
                "INFO",
            )
            return new_side

        if taker_round_stats["round_pnl"] <= Decimal("0"):
            new_side = "sell" if current_side == "buy" else "buy"
            self.strategy_config.maker_side = new_side
            self.logger.log(
                f"[MAKER-DIRECTION] Last round taker P&L {taker_round_stats['round_pnl']:+.6f} <= 0, "
                f"flip maker side to {new_side}",
                "INFO",
            )
            return new_side

        self.logger.log(
            f"[MAKER-DIRECTION] Last round taker P&L {taker_round_stats['round_pnl']:+.6f} > 0, "
            f"keeping maker side {current_side}",
            "INFO",
        )
        return current_side

    # ------------------------------------------------------------------
    # Trading phases
    # ------------------------------------------------------------------

    async def _maker_open_phase(self) -> Decimal:
        side = self.strategy_config.maker_side
        target_quantity = self.strategy_config.quantity
        filled_total = Decimal("0")
        attempt = 0
        tolerance = Decimal("1e-8")

        while filled_total < target_quantity - tolerance:
            remaining = target_quantity - filled_total
            attempt += 1
            self.logger.log(
                f"[MAKER-OPEN] Attempt {attempt} placing limit order side={side} qty={remaining}",
                "INFO",
            )

            order = await self.maker.client.place_open_order(
                self.maker.config.contract_id,
                remaining,
                side,
            )
            if not order.success or not order.order_id:
                raise RuntimeError(
                    f"Failed to place maker open order: {order.error_message}"
                )

            self.logger.log(
                f"[MAKER-OPEN-SUCCESS] order side={order.side} qty={order.size} price={order.price}",
                "INFO",
            )

            tracker = self._register_tracker(
                self.maker,
                order.order_id,
                remaining,
                lambda delta, fill_price, update: self._handle_maker_fill(
                    delta, fill_price, "open"
                ),
            )

            status = await self._await_order_completion(tracker)
            filled_amount = tracker.total_filled
            self._unregister_tracker(self.maker, order.order_id)

            if status not in {"FILLED", "CANCELED"}:
                raise RuntimeError(
                    f"Unexpected status for maker open order {order.order_id}: {status}"
                )

            self.logger.log(
                f"[CHECK-OPEN-FILL] Order {order.order_id} completed with status {status}, "
                f"filled this attempt {filled_amount}, total filled {filled_total}, retrying remaining {target_quantity - filled_total}",
                "INFO",
            )

            if filled_amount > 0:
                filled_total += filled_amount

            if filled_total >= target_quantity - tolerance:
                break

        return filled_total

    async def _holding_phase(self):
        if self.strategy_config.max_holding_time <= 0:
            return

        min_ms = max(0, self.strategy_config.min_holding_time)
        max_ms = max(0, self.strategy_config.max_holding_time)
        if max_ms < min_ms:
            max_ms = min_ms

        hold_ms = random.randint(min_ms, max_ms) if max_ms > 0 else 0
        await asyncio.sleep(hold_ms / 1000.0)

    async def _maker_close_phase(self, quantity: Decimal):
        if quantity <= Decimal("0"):
            self.logger.log("No maker position to close in this round", "INFO")
            return

        close_side = "sell" if self.strategy_config.maker_side == "buy" else "buy"
        target_quantity = quantity
        closed_total = Decimal("0")
        attempt = 0
        tolerance = Decimal("1e-8")

        while closed_total < target_quantity - tolerance:
            remaining = target_quantity - closed_total
            price = await self._maker_price(close_side)

            attempt += 1
            self.logger.log(
                f"[MAKER-CLOSE] Attempt {attempt} placing limit order side={close_side} qty={remaining} price={price}",
                "INFO",
            )
            order = await self.maker.client.place_close_order(
                self.maker.config.contract_id,
                remaining,
                price,
                close_side,
            )
            if not order.success or not order.order_id:
                raise RuntimeError(
                    f"Failed to place maker close order: {order.error_message}"
                )

            self.logger.log(
                f"[MAKER-CLOSE-SUCCESS] order side={order.side} qty={order.size} price={order.price}",
                "INFO",
            )

            tracker = self._register_tracker(
                self.maker,
                order.order_id,
                remaining,
                lambda delta, fill_price, update: self._handle_maker_fill(
                    delta, fill_price, "close"
                ),
            )

            status = await self._await_order_completion(tracker)
            filled_amount = tracker.total_filled
            self._unregister_tracker(self.maker, order.order_id)

            if status not in {"FILLED", "CANCELED"}:
                raise RuntimeError(
                    f"Unexpected status for maker close order {order.order_id}: {status}"
                )

            self.logger.log(
                f"[CHECK-CLOSE-FILL] Order {order.order_id} completed with status {status}, "
                f"filled this attempt {filled_amount}, total closed {closed_total}, retrying remaining {target_quantity - closed_total}",
                "INFO",
            )

            if filled_amount > 0:
                closed_total += filled_amount

            if (
                closed_total >= target_quantity - tolerance
                or abs(self.maker_position) <= tolerance
            ):
                break

    async def _perform_cleanup(self):
        self.logger.log("Starting cleanup routine", "INFO")

        # Check if we have any real positions to clean up
        has_real_positions = False
        positions_info = {}
        self.cleanup_rounds += 1

        # Check maker position
        maker_position = await self.maker.client.get_account_positions()
        positions_info[self.maker.name] = maker_position
        if abs(maker_position) > Decimal("1e-8"):
            has_real_positions = True

        # Check taker positions
        for taker in self.takers:
            taker_position = await taker.client.get_account_positions()
            positions_info[taker.name] = taker_position
            if abs(taker_position) > Decimal("1e-8"):
                has_real_positions = True

        self.logger.log(f"Positions check: {positions_info}", "INFO")
        self.logger.log(f"Has real positions: {has_real_positions}", "INFO")

        if not has_real_positions:
            self.logger.log("No real positions detected, skipping flatten step", "INFO")
            await self._cancel_all_open_orders()
            return

        # Standard cleanup for real positions
        self.logger.log("Performing standard cleanup routine", "INFO")
        await self._cancel_all_open_orders()
        # 这里需要sleep一下，等待网络反馈
        await asyncio.sleep(3)
        await self._flatten_account(self.maker)
        for taker in self.takers:
            await self._flatten_account(taker)
        self.logger.log("Cleanup routine finished", "INFO")

    async def _flatten_account(self, account: AccountRuntime):
        position = await account.client.get_account_positions()
        if abs(position) <= Decimal("1e-8"):
            return

        side = "sell" if position > 0 else "buy"
        qty = abs(position)
        self.logger.log(
            f"[FLATTEN] {account.name} executing market order side={side} qty={qty} to flatten position",
            "INFO",
        )
        result = await account.client.place_market_order(
            account.config.contract_id, qty, side
        )
        if not result.success:
            raise RuntimeError(
                f"[{account.name}] Failed to flatten position: {result.error_message}"
            )

        if account == self.maker:
            self.maker_position = Decimal("0")
        else:
            self.taker_positions[account.name] = Decimal("0")

        self.logger.log(
            f"[{account.name}] Flattened position via market {side} {qty}", "INFO"
        )

    async def _cancel_all_open_orders(self):
        self.logger.log("Cancelling all open orders...", "INFO")

        tasks = []
        for account in self.accounts.values():
            for tracker in list(account.order_trackers.values()):
                tasks.append((account, tracker))

        if not tasks:
            self.logger.log("No open orders to cancel", "INFO")
            return

        for account, tracker in tasks:
            try:
                self.logger.log(
                    f"[CANCEL] Attempting to cancel order {tracker.order_id} on account {account.name}",
                    "INFO",
                )
                result = await account.client.cancel_order(tracker.order_id)
                if not result.success:
                    self.logger.log(
                        f"[{account.name}] Failed to cancel open order {tracker.order_id}: {result.error_message}",
                        "WARNING",
                    )
                self._unregister_tracker(account, tracker.order_id)
            except Exception as exc:
                self.logger.log(
                    f"[{account.name}] Exception cancelling order {tracker.order_id}: {exc}",
                    "ERROR",
                )

        self.logger.log("Open order cancellation completed", "INFO")

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------

    async def run(self):
        self.logger.log("Initializing Eagle Chicken Strategy...", "INFO")
        await self._initialize_accounts()
        self.logger.log("All accounts connected. Strategy starting.", "INFO")

        self._register_signals()

        try:
            while not self.shutdown_requested:
                if (
                    self.strategy_config.max_play_rounds
                    and self.completed_rounds >= self.strategy_config.max_play_rounds
                ):
                    self.logger.log(
                        "Reached max_play_rounds. Initiating shutdown.", "INFO"
                    )
                    break

                self.logger.log(
                    f"--- Starting round {self.completed_rounds + 1} ---", "INFO"
                )

                try:
                    self.logger.log("--- _maker_open_phase ---", "INFO")
                    self._calculate_maker_side_for_new_round()
                    filled = await self._maker_open_phase()
                    if filled <= 0:
                        self.logger.log(
                            "No fill during maker open phase, skipping remainder of round",
                            "WARNING",
                        )
                        continue
                    self.logger.log("--- finish _maker_open_phase ---", "INFO")

                    self.logger.log("--- _holding_phase ---", "INFO")
                    await self._holding_phase()
                    self.logger.log("--- finish _holding_phase ---", "INFO")
                    self.logger.log("--- _maker_close_phase ---", "INFO")
                    await self._maker_close_phase(abs(self.maker_position))
                    self.logger.log("--- finish _maker_close_phase ---", "INFO")

                    self.completed_rounds += 1
                    self.cleanup_counter += 1

                    if self.cleanup_counter >= self.strategy_config.cleanup_rounds:
                        await self._perform_cleanup()
                        self.cleanup_counter = 0

                    # Display taker account statistics after cleanup
                    await self._calculate_taker_statistics()
                    self._print_taker_stats_table()

                    if self._check_target_volume_met():
                        self.shutdown_requested = True
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.log(
                        f"Round {self.completed_rounds + 1} failed with error: {exc}",
                        "ERROR",
                    )
                    self.logger.log(traceback.format_exc(), "ERROR")
                    await self._cancel_all_open_orders()
                    await asyncio.sleep(1)
                    continue

                if self.shutdown_event.is_set():
                    break

            # Check if we should perform cleanup or quick shutdown
            has_real_positions = abs(self.maker_position) > Decimal("1e-8")
            for taker_pos in self.taker_positions.values():
                if abs(taker_pos) > Decimal("1e-8"):
                    has_real_positions = True
                    break

            if has_real_positions:
                self.logger.log("Standard shutdown: Performing cleanup", "INFO")
                await self._perform_cleanup()
            else:
                self.logger.log("Quick shutdown: No real positions remaining", "INFO")
                await self._cancel_all_open_orders()

        except asyncio.CancelledError:
            self.logger.log("Strategy cancelled via CancelledError", "INFO")
            pass
        except Exception as exc:
            self.logger.log(f"Critical error outside trading loop: {exc}", "ERROR")
            self.logger.log(traceback.format_exc(), "DEBUG")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        self.logger.log("Shutting down strategy...", "INFO")
        for account in self.accounts.values():
            try:
                await account.client.disconnect()
            except Exception as exc:
                self.logger.log(f"Error disconnecting {account.name}: {exc}", "ERROR")

        # 重置优雅关闭标志
        self.graceful_shutdown_in_progress = False
        self.logger.log("✅ Graceful shutdown completed successfully.", "INFO")

    def request_shutdown(self, reason: str = "User requested"):
        if not self.shutdown_requested:
            # 第一次Ctrl+C - 开始优雅关闭
            self.logger.log(f"🔸 Graceful shutdown initiated: {reason}", "WARNING")
            self.logger.log("Press Ctrl+C again to force immediate exit", "INFO")
            self.shutdown_requested = True
            self.graceful_shutdown_in_progress = True
            self.shutdown_event.set()
        elif self.graceful_shutdown_in_progress:
            # 第二次Ctrl+C - 强制退出
            self.logger.log(
                "🔴 Force shutdown triggered! Exiting immediately.", "ERROR"
            )
            import os
            import signal

            # 强制终止整个进程
            os._exit(1)

    def _register_signals(self):
        if os.name == "nt":
            # Windows event loop does not expose add_signal_handler
            self.logger.log("Skipping signal handlers on Windows platform", "DEBUG")
            return

        loop = asyncio.get_running_loop()

        def _handler(signame):
            self.request_shutdown(f"Received {signame}")

        for signame in ("SIGINT", "SIGTERM"):
            if hasattr(signal, signame):
                loop.add_signal_handler(
                    getattr(signal, signame), lambda s=signame: _handler(s)
                )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main():
    if len(sys.argv) < 2:
        print("Usage: python eagle_chicken_stratge.py <config_path>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    strategy = EagleChickenStrategy(config_path)
    await strategy.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
