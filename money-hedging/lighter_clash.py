#!/usr/bin/env python3
"""
Lighter Clash Strategy

Coordinates two Lighter accounts to trade against each other when the order
book spread widens enough to safely cross both sides at the midpoint price.
Accounts alternately open and close positions after a configurable holding
period while monitoring the best bid/ask via WebSocket data exposed by the
standard Lighter client.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from exchanges import BaseExchangeClient, ExchangeFactory
from helpers import TradingLogger


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Optional[str] = None
    console: bool = True


@dataclass
class AccountConfig:
    name: str
    credentials: Dict[str, Any]


@dataclass
class StrategyConfig:
    ticker: str
    quantity: Decimal
    spread_threshold: Decimal
    hold_seconds: float
    poll_interval: float
    order_timeout: float
    accounts: List[AccountConfig]
    logging: LoggingConfig


@dataclass
class AccountTradingConfig:
    name: str
    exchange: str
    ticker: str
    quantity: Decimal
    direction: str
    contract_id: str = ""
    tick_size: Decimal = Decimal("0")

    @property
    def close_order_side(self) -> str:
        return "buy" if self.direction == "sell" else "sell"


@dataclass
class AccountRuntime:
    name: str
    config: AccountTradingConfig
    client: BaseExchangeClient
    order_trackers: Dict[str, "OrderTracker"] = field(default_factory=dict)


class OrderTracker:
    """Track fill progress for a single order via WebSocket callbacks."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        account: AccountRuntime,
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
        self.raw_total_size = Decimal("0")
        self.raw_filled = Decimal("0")

        self.fill_event = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.failure_event = asyncio.Event()

    def handle_update(self, update: Dict[str, Any]) -> None:
        """Normalize callback payloads and update async events."""

        def _process():
            try:
                filled_raw = Decimal(str(update.get("filled_size") or "0"))
                total_raw = update.get("size")
                if total_raw is not None:
                    self.raw_total_size = Decimal(str(total_raw))
                delta_raw = filled_raw - self.raw_filled
                self.raw_filled = filled_raw

                price_val = update.get("price")
                price = (
                    Decimal(str(price_val)) if price_val is not None else Decimal("0")
                )
                status = str(update.get("status", "")).upper()

                if (
                    delta_raw > 0
                    and self.raw_total_size > 0
                    and self.expected_size > 0
                ):
                    ratio = delta_raw / self.raw_total_size
                    delta = self.expected_size * ratio
                    task = self.loop.create_task(
                        self.on_fill_callback(delta, price, update)
                    )
                    task.add_done_callback(self._handle_callback_failure)

                if status == "FILLED":
                    self.status = "FILLED"
                    self.fill_event.set()
                elif status == "CANCELED":
                    self.status = "CANCELED"
                    self.cancel_event.set()
                elif status == "FAILED":
                    self.status = "FAILED"
                    self.failure_event.set()
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.log(
                    f"Failed to process order update for {self.order_id}: {exc}", "ERROR"
                )

        self.loop.call_soon_threadsafe(_process)

    def _handle_callback_failure(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.logger.log(
                f"Order tracker callback raised for {self.order_id}: {exc}", "ERROR"
            )


class LighterClashStrategy:
    """Main strategy controller."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.strategy_config = self._load_config(self.config_path)
        self.logger = TradingLogger(
            exchange="lighter_clash",
            ticker=self.strategy_config.ticker,
            log_to_console=self.strategy_config.logging.console,
        )

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.shutdown_event = asyncio.Event()

        self.accounts: Dict[str, AccountRuntime] = {}
        self.account_positions: Dict[str, Decimal] = {}
        self.primary_account: Optional[AccountRuntime] = None

        self.entry_layout: List[tuple[AccountRuntime, str]] = []
        self.exit_layout: List[tuple[AccountRuntime, str]] = []

        self.position_open = False
        self.last_entry_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self, path: Path) -> StrategyConfig:
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError("Invalid configuration file")

        try:
            quantity = Decimal(str(raw["quantity"]))
            ticker = str(raw["ticker"]).upper()
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ValueError("ticker and quantity are required in config") from exc

        spread_ratio = raw.get("spread_threshold_ratio")
        if spread_ratio is not None:
            spread_threshold = Decimal(str(spread_ratio))
        else:
            pct_value = Decimal(str(raw.get("spread_threshold_pct", "0.1")))
            spread_threshold = pct_value / Decimal("100")

        logging_cfg = raw.get("logging", {}) or {}
        logging_config = LoggingConfig(
            level=logging_cfg.get("level", "INFO"),
            file=logging_cfg.get("file"),
            console=bool(logging_cfg.get("console", True)),
        )

        account_entries = raw.get("accounts", [])
        if not account_entries or len(account_entries) != 2:
            raise ValueError("Exactly two lighter accounts must be configured")

        accounts = []
        for entry in account_entries:
            name = entry.get("name")
            credentials = entry.get("credentials") or {}
            if not name or not credentials:
                raise ValueError("Each account requires name and credentials")
            accounts.append(AccountConfig(name=name, credentials=credentials))

        return StrategyConfig(
            ticker=ticker,
            quantity=quantity,
            spread_threshold=spread_threshold,
            hold_seconds=float(raw.get("hold_seconds", 5)),
            poll_interval=float(raw.get("poll_interval", 0.5)),
            order_timeout=float(raw.get("order_timeout", 5)),
            accounts=accounts,
            logging=logging_config,
        )

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _temporary_env(self, mapping: Dict[str, Any]):
        previous: Dict[str, Optional[str]] = {}
        try:
            for key, value in mapping.items():
                key_str = str(key)
                previous[key_str] = os.environ.get(key_str)
                os.environ[key_str] = str(value)
            yield
        finally:
            for key, original in previous.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original

    async def _initialize_accounts(self):
        self.loop = asyncio.get_running_loop()

        for idx, account_cfg in enumerate(self.strategy_config.accounts):
            direction = "buy" if idx == 0 else "sell"
            trading_config = AccountTradingConfig(
                name=account_cfg.name,
                exchange="lighter",
                ticker=self.strategy_config.ticker,
                quantity=self.strategy_config.quantity,
                direction=direction,
            )

            credential_env = dict(account_cfg.credentials)
            credential_env["ACCOUNT_NAME"] = account_cfg.name

            with self._temporary_env(credential_env):
                client = ExchangeFactory.create_exchange("lighter", trading_config)

            runtime = AccountRuntime(
                name=account_cfg.name,
                config=trading_config,
                client=client,
            )
            self.accounts[account_cfg.name] = runtime
            self.account_positions[account_cfg.name] = Decimal("0")

        if len(self.accounts) != 2:
            raise RuntimeError("Account initialization failed")

        self.primary_account = list(self.accounts.values())[0]

        await asyncio.gather(*(self._prepare_account(acc) for acc in self.accounts.values()))

        ordered_accounts = [
            self.accounts[cfg.name] for cfg in self.strategy_config.accounts
        ]
        self.entry_layout = [
            (ordered_accounts[0], "buy"),
            (ordered_accounts[1], "sell"),
        ]
        self.exit_layout = [
            (ordered_accounts[0], "sell"),
            (ordered_accounts[1], "buy"),
        ]

    async def _prepare_account(self, account: AccountRuntime):
        client = account.client

        contract_id, tick_size = await client.get_contract_attributes()
        account.config.contract_id = contract_id
        account.config.tick_size = tick_size

        if account.config.tick_size <= 0:
            raise ValueError("Tick size must be positive for Lighter markets")

        def handler(order_updates):
            updates = order_updates if isinstance(order_updates, list) else [order_updates]
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

        with self._temporary_env({"ACCOUNT_NAME": account.name}):
            await client.connect()

        await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Order utilities
    # ------------------------------------------------------------------

    def _register_tracker(
        self,
        account: AccountRuntime,
        order_id: str,
        expected_size: Decimal,
        side: str,
    ) -> OrderTracker:
        async def on_fill(delta: Decimal, price: Decimal, update: Dict[str, Any]):
            signed_delta = delta if side.lower() == "buy" else -delta
            self.account_positions[account.name] += signed_delta
            self.logger.log(
                f"[{account.name}] {side.upper()} fill {delta:.8f} @ {price}",
                "INFO",
            )

        tracker = OrderTracker(
            self.loop,
            account,
            order_id,
            expected_size,
            on_fill,
            self.logger,
        )
        account.order_trackers[order_id] = tracker
        return tracker

    def _unregister_tracker(self, account: AccountRuntime, order_id: str):
        account.order_trackers.pop(order_id, None)

    async def _await_completion(self, tracker: OrderTracker) -> str:
        try:
            await asyncio.wait_for(
                tracker.fill_event.wait(), timeout=self.strategy_config.order_timeout
            )
            return tracker.status
        except asyncio.TimeoutError:
            account = tracker.account
            remaining = tracker.expected_size
            self.logger.log(
                f"[{account.name}] Order {tracker.order_id} timed out, cancelling remainder {remaining}",
                "WARNING",
            )
            cancel_result = await account.client.cancel_order(tracker.order_id)
            if not cancel_result.success:
                self.logger.log(
                    f"[{account.name}] Cancel failed for {tracker.order_id}: {cancel_result.error_message}",
                    "ERROR",
                )
            tracker.status = "CANCELED"
            return tracker.status

    # ------------------------------------------------------------------
    # Trading logic
    # ------------------------------------------------------------------

    async def _place_pair_orders(
        self, legs: List[tuple[AccountRuntime, str]], price: Decimal
    ) -> bool:
        trackers: List[OrderTracker] = []

        for account, side in legs:
            try:
                order_result = await account.client.place_limit_order(
                    account.config.contract_id,
                    self.strategy_config.quantity,
                    price,
                    side,
                )
            except Exception as exc:
                self.logger.log(
                    f"[{account.name}] Failed to submit {side} order: {exc}",
                    "ERROR",
                )
                return False

            if not order_result.success or not order_result.order_id:
                self.logger.log(
                    f"[{account.name}] Order rejected: {order_result.error_message}",
                    "ERROR",
                )
                return False

            tracker = self._register_tracker(
                account, order_result.order_id, self.strategy_config.quantity, side
            )
            trackers.append(tracker)

        results = await asyncio.gather(
            *(self._await_completion(trk) for trk in trackers), return_exceptions=True
        )

        success = True
        for tracker, status in zip(trackers, results):
            if isinstance(status, Exception):
                self.logger.log(
                    f"[{tracker.account.name}] Error awaiting order {tracker.order_id}: {status}",
                    "ERROR",
                )
                success = False
            elif status != "FILLED":
                self.logger.log(
                    f"[{tracker.account.name}] Order {tracker.order_id} completed with status {status}",
                    "WARNING",
                )
                success = False
            self._unregister_tracker(tracker.account, tracker.order_id)

        return success

    async def _attempt_cycle(
        self, legs: List[tuple[AccountRuntime, str]], best_bid: Decimal, best_ask: Decimal
    ) -> bool:
        if not self.primary_account:
            raise RuntimeError("Primary account not initialized")

        mid_price = (best_bid + best_ask) / Decimal("2")
        price = self.primary_account.client.round_to_tick(mid_price)

        if price <= 0:
            self.logger.log("Computed price is not positive, skipping trade", "WARNING")
            return False

        leg_desc = ", ".join(f"{acc.name}:{side}" for acc, side in legs)
        self.logger.log(
            f"Submitting clash orders at {price} ({leg_desc})",
            "INFO",
        )

        return await self._place_pair_orders(legs, price)

    async def _monitor_market(self):
        if not self.primary_account:
            raise RuntimeError("Primary account missing")

        poll_interval = max(self.strategy_config.poll_interval, 0.1)

        while not self.shutdown_event.is_set():
            try:
                best_bid, best_ask = await self.primary_account.client.fetch_bbo_prices(
                    self.primary_account.config.contract_id
                )
            except Exception as exc:
                self.logger.log(f"Failed to fetch BBO: {exc}", "WARNING")
                await asyncio.sleep(poll_interval)
                continue

            if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
                await asyncio.sleep(poll_interval)
                continue

            mid_price = (best_bid + best_ask) / Decimal("2")
            if mid_price <= 0:
                await asyncio.sleep(poll_interval)
                continue

            spread_ratio = (best_ask - best_bid) / mid_price

            if spread_ratio < self.strategy_config.spread_threshold:
                await asyncio.sleep(poll_interval)
                continue

            now = time.time()
            if not self.position_open:
                self.logger.log(
                    f"Entry condition met - spread {spread_ratio:.6f}, "
                    f"bid {best_bid}, ask {best_ask}",
                    "INFO",
                )
                entry_success = await self._attempt_cycle(
                    self.entry_layout, best_bid, best_ask
                )
                if entry_success:
                    self.position_open = True
                    self.last_entry_ts = now
                    self.logger.log("Entry orders filled", "INFO")
                else:
                    self.logger.log("Entry attempt failed", "WARNING")
            else:
                if self.last_entry_ts and (now - self.last_entry_ts) < self.strategy_config.hold_seconds:
                    await asyncio.sleep(poll_interval)
                    continue

                self.logger.log(
                    f"Exit condition met - spread {spread_ratio:.6f}, "
                    f"holding for {now - (self.last_entry_ts or now):.2f}s",
                    "INFO",
                )
                exit_success = await self._attempt_cycle(
                    self.exit_layout, best_bid, best_ask
                )
                if exit_success:
                    self.position_open = False
                    self.last_entry_ts = None
                    self.logger.log("Exit orders filled", "INFO")
                else:
                    self.logger.log("Exit attempt failed, retaining position", "WARNING")

            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self._register_signals()

        await self._initialize_accounts()
        self.logger.log("Accounts connected - starting monitor loop", "INFO")

        try:
            await self._monitor_market()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.log(f"Fatal error: {exc}", "ERROR")
            self.logger.log(traceback.format_exc(), "ERROR")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        self.logger.log("Shutting down lighter clash strategy...", "INFO")
        for account in self.accounts.values():
            try:
                await account.client.disconnect()
            except Exception as exc:
                self.logger.log(f"Error disconnecting {account.name}: {exc}", "ERROR")
        self.logger.log("Shutdown complete", "INFO")

    def request_shutdown(self, reason: str = "User requested"):
        if self.shutdown_event.is_set():
            return
        self.logger.log(f"Shutdown requested: {reason}", "WARNING")
        self.shutdown_event.set()

    def _register_signals(self):
        loop = asyncio.get_running_loop()

        def _handler(signame):
            self.request_shutdown(f"Received {signame}")

        for signame in ("SIGINT", "SIGTERM"):
            if hasattr(signal, signame):
                loop.add_signal_handler(
                    getattr(signal, signame), lambda s=signame: _handler(s)
                )


async def main():
    if len(sys.argv) < 2:
        print("Usage: python lighter_clash.py <config_path>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    strategy = LighterClashStrategy(config_path)
    await strategy.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
