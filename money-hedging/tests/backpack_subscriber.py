import argparse
import asyncio
import json
import logging
import urllib.request
from urllib.error import HTTPError, URLError
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple, Type, Union, cast

import yaml
from pydantic import BaseModel, Field, field_validator
from websockets.legacy.client import WebSocketClientProtocol, connect

from stream_logging import StreamLogWriter, current_timestamp_us
from trade_storage import NormalizedTrade, NormalizedTradeWriter, coerce_timestamp_ms


DEFAULT_WS_URL = "wss://ws.backpack.exchange"
DEFAULT_DB_PATH = "stream_logs.db"
DEFAULT_NORMALIZED_DB_PATH = "data/normalized_trades.db"
DEPTH_REST_URL_TEMPLATE = "https://api.backpack.exchange/api/v1/depth?symbol={symbol}"


def _model_to_dict(model: BaseModel) -> Dict[str, Any]:
    return cast(Dict[str, Any], model.model_dump())


class BaseSubscription(BaseModel):
    def stream_name(self) -> str:
        raise NotImplementedError

    def parameter_values(self) -> List[Optional[str]]:
        return []


class BookTickerSubscription(BaseSubscription):
    stream: Literal["bookTicker"]
    symbol: str

    def stream_name(self) -> str:
        return f"bookTicker.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol]


class DepthSubscription(BaseSubscription):
    stream: Literal["depth"]
    symbol: str
    aggregation: Optional[Literal["200ms", "600ms", "1000ms"]] = None

    def stream_name(self) -> str:
        if self.aggregation:
            return f"depth.{self.aggregation}.{self.symbol}"
        return f"depth.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol, self.aggregation]


class KlineSubscription(BaseSubscription):
    stream: Literal["kline"]
    interval: str
    symbol: str

    def stream_name(self) -> str:
        return f"kline.{self.interval}.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.interval, self.symbol]


class LiquidationSubscription(BaseSubscription):
    stream: Literal["liquidation"]

    def stream_name(self) -> str:
        return "liquidation"


class MarkPriceSubscription(BaseSubscription):
    stream: Literal["markPrice"]
    symbol: str

    def stream_name(self) -> str:
        return f"markPrice.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol]


class TickerSubscription(BaseSubscription):
    stream: Literal["ticker"]
    symbol: str

    def stream_name(self) -> str:
        return f"ticker.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol]


class OpenInterestSubscription(BaseSubscription):
    stream: Literal["openInterest"]
    symbol: str

    def stream_name(self) -> str:
        return f"openInterest.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol]


class TradeSubscription(BaseSubscription):
    stream: Literal["trade"]
    symbol: str

    def stream_name(self) -> str:
        return f"trade.{self.symbol}"

    def parameter_values(self) -> List[Optional[str]]:
        return [self.symbol]


Subscription = Union[
    BookTickerSubscription,
    DepthSubscription,
    KlineSubscription,
    LiquidationSubscription,
    MarkPriceSubscription,
    TickerSubscription,
    OpenInterestSubscription,
    TradeSubscription,
]


class AppConfig(BaseModel):
    websocket_url: str = Field(default=DEFAULT_WS_URL)
    database: str = Field(default=DEFAULT_DB_PATH)
    normalized_database: str = Field(default=DEFAULT_NORMALIZED_DB_PATH)
    subscriptions: List[Subscription]

    @field_validator("subscriptions")
    @classmethod
    def ensure_unique_streams(cls, value: List[Subscription]) -> List[Subscription]:
        names = [subscription.stream_name() for subscription in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Duplicate stream subscriptions detected: {sorted(duplicates)}")
        return value


class BookTickerEvent(BaseModel):
    e: Literal["bookTicker"]
    E: int
    s: str
    a: str
    A: str
    b: str
    B: str
    u: str
    T: int


class DepthEvent(BaseModel):
    e: Literal["depth"]
    E: int
    s: str
    a: List[Tuple[str, str]]
    b: List[Tuple[str, str]]
    U: int
    u: int
    T: int


class KlineEvent(BaseModel):
    e: Literal["kline"]
    E: int
    s: str
    t: Union[int, str]
    T: Union[int, str]
    o: Optional[str]
    c: Optional[str]
    h: Optional[str]
    l: Optional[str]
    v: Optional[str]
    n: Optional[int]
    X: bool


class LiquidationEvent(BaseModel):
    e: Literal["liquidation"]
    E: int
    q: str
    p: str
    S: Literal["Bid", "Ask"]
    s: str
    T: int


class MarkPriceEvent(BaseModel):
    e: Literal["markPrice"]
    E: int
    s: str
    p: str
    f: str
    i: str
    n: int


class TickerEvent(BaseModel):
    e: Literal["ticker"]
    E: int
    s: str
    o: str
    c: str
    h: str
    l: str
    v: str
    V: str
    n: int


class OpenInterestEvent(BaseModel):
    e: Literal["openInterest"]
    E: int
    s: str
    o: str


class TradeEvent(BaseModel):
    e: Literal["trade"]
    E: int
    s: str
    p: str
    q: str
    b: str
    a: str
    t: int
    T: int
    m: bool


STREAM_EVENT_MODELS: Dict[str, Type[BaseModel]] = {
    "bookTicker": BookTickerEvent,
    "depth": DepthEvent,
    "kline": KlineEvent,
    "liquidation": LiquidationEvent,
    "markPrice": MarkPriceEvent,
    "ticker": TickerEvent,
    "openInterest": OpenInterestEvent,
    "trade": TradeEvent,
}


async def iter_messages(socket: WebSocketClientProtocol) -> AsyncIterator[str]:
    try:
        async for message in socket:
            if isinstance(message, bytes):
                try:
                    decoded = message.decode("utf-8")
                except UnicodeDecodeError:
                    logging.debug("Ignoring non-UTF8 message: %s", message)
                    continue
                yield decoded
            else:
                yield message
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.error("Websocket receive loop terminated: %s", exc)
        raise


def load_config(path: Union[str, Path]) -> AppConfig:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


def serialize_params(subscription: Subscription) -> List[Optional[str]]:
    return subscription.parameter_values()


def determine_event_model(stream_name: str) -> Optional[Type[BaseModel]]:
    prefix = stream_name.split(".", 1)[0]
    return STREAM_EVENT_MODELS.get(prefix)


async def fetch_depth_snapshot(symbol: str) -> Dict[str, Any]:
    url = DEPTH_REST_URL_TEMPLATE.format(symbol=symbol)
    loop = asyncio.get_running_loop()

    def _request() -> Dict[str, Any]:
        with urllib.request.urlopen(url, timeout=10) as response:
            try:
                return cast(Dict[str, Any], json.load(response))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON from depth snapshot for {symbol}") from exc

    try:
        return await loop.run_in_executor(None, _request)
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"HTTP error fetching depth snapshot for {symbol}: {exc}") from exc


def extract_event_time(event: BaseModel) -> Optional[int]:
    for attr in ("E", "event_time"):
        if hasattr(event, attr):
            value = getattr(event, attr)
            return int(value) if value is not None else None
    return None


async def run_client(config: AppConfig) -> None:
    stream_map: Dict[str, Subscription] = {
        subscription.stream_name(): subscription for subscription in config.subscriptions
    }

    if not stream_map:
        logging.warning("No subscriptions defined; nothing to do.")
        return

    async with connect(config.websocket_url) as websocket:
        log_writer = StreamLogWriter(config.database)
        normalized_writer = NormalizedTradeWriter(config.normalized_database)
        pending_depth_fetch: Set[str] = set()
        try:
            params = list(stream_map.keys())
            for stream_name in params:
                subscribe_payload = {"method": "SUBSCRIBE", "params": [stream_name]}
                await websocket.send(json.dumps(subscribe_payload))
                logging.debug("Sent subscribe request for %s", stream_name)
            logging.info("Subscribed to %d stream(s).", len(params))

            for stream_name, subscription in stream_map.items():
                start_payload = {
                    "status": "start_sub",
                    "stream": stream_name,
                    "config": _model_to_dict(subscription),
                }
                log_writer.insert_event(
                    stream_name=stream_name,
                    params=serialize_params(subscription),
                    event_type="start_sub",
                    event_time=current_timestamp_us(),
                    event_data=json.dumps(start_payload, separators=(",", ":")),
                )

            async for raw_message in iter_messages(websocket):
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logging.debug("Ignoring non-JSON message: %s", raw_message)
                    continue

                if "error" in message:
                    logging.error(
                        "Exchange rejected request: code=%s message=%s",
                        message["error"].get("code"),
                        message["error"].get("message"),
                    )
                    continue

                if "stream" not in message or "data" not in message:
                    logging.debug("Ignoring unrecognized payload: %s", message)
                    continue

                stream_name = message["stream"]
                payload = message["data"]
                current_subscription = stream_map.get(stream_name)
                if current_subscription is None:
                    logging.debug("Skipping message for unsubscribed stream: %s", stream_name)
                    continue

                model_cls = determine_event_model(stream_name)
                if not model_cls:
                    logging.error("No event model registered for stream: %s", stream_name)
                    continue

                try:
                    event_model = model_cls.model_validate(payload)
                except Exception as exc:
                    logging.error("Failed to validate payload for %s: %s", stream_name, exc)
                    continue

                event_time = extract_event_time(event_model)
                event_type = cast(Optional[str], getattr(event_model, "e", None))

                if isinstance(event_model, DepthEvent):
                    symbol = cast(Optional[str], getattr(event_model, "s", None))
                    if symbol:
                        needs_snapshot = symbol not in pending_depth_fetch and not log_writer.has_depth_snapshot(symbol)
                        if needs_snapshot:
                            pending_depth_fetch.add(symbol)
                            try:
                                snapshot = await fetch_depth_snapshot(symbol)
                            except Exception as exc:
                                logging.error("Failed to fetch depth snapshot for %s: %s", symbol, exc)
                            else:
                                asks_json = json.dumps(snapshot.get("asks", []), separators=(",", ":"), ensure_ascii=False)
                                bids_json = json.dumps(snapshot.get("bids", []), separators=(",", ":"), ensure_ascii=False)
                                last_update_id = snapshot.get("lastUpdateId")
                                snapshot_time_value = snapshot.get("timestamp")
                                snapshot_time_int: Optional[int]
                                try:
                                    snapshot_time_int = int(snapshot_time_value) if snapshot_time_value is not None else None
                                except (TypeError, ValueError):
                                    snapshot_time_int = None
                                log_writer.upsert_depth_snapshot(
                                    symbol=symbol,
                                    asks=asks_json,
                                    bids=bids_json,
                                    last_update_id=str(last_update_id) if last_update_id is not None else None,
                                    snapshot_time=snapshot_time_int,
                                )
                                logging.info("Stored depth snapshot for %s", symbol)
                            finally:
                                pending_depth_fetch.discard(symbol)

                if isinstance(event_model, TradeEvent):
                    executed_at_ms = coerce_timestamp_ms(getattr(event_model, "T", None))
                    if executed_at_ms is None:
                        logging.debug("Skipping trade with missing timestamp: %s", payload)
                    else:
                        aggressor_side = "sell" if event_model.m else "buy"
                        trade_id = str(event_model.t)
                        raw_symbol = event_model.s
                        normalized_symbol = raw_symbol.split("_", 1)[0] if "_" in raw_symbol else raw_symbol
                        normalized_trade = NormalizedTrade(
                            exchange="backpack",
                            symbol=normalized_symbol,
                            trade_id=trade_id,
                            executed_at_ms=executed_at_ms,
                            price=str(event_model.p),
                            size=str(event_model.q),
                            aggressor_side=aggressor_side,
                            event_time_us=event_time,
                        )
                        normalized_writer.insert_trade(normalized_trade)

                event_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                log_writer.insert_event(
                    stream_name=stream_name,
                    params=serialize_params(current_subscription),
                    event_type=event_type,
                    event_time=event_time,
                    event_data=event_json,
                )
        finally:
            normalized_writer.close()
            log_writer.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backpack exchange websocket subscriber.")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)
    config_path = Path(args.config)
    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.error("Failed to load config %s: %s", config_path, exc)
        raise SystemExit(1)

    try:
        asyncio.run(run_client(config))
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")


if __name__ == "__main__":
    main()
