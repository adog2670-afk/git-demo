import asyncio
import time
import json

import websockets

# URL = "wss://ws.backpack.exchange"
URL = "wss://mainnet.zklighter.elliot.ai/stream"


async def main():
    t0 = time.perf_counter()
    async with websockets.connect(URL, ping_interval=None) as ws:
        t1 = time.perf_counter()
        print(f"WebSocket握手耗时: {(t1 - t0) * 1000:.1f} ms")

        # 方法1: 使用 WebSocket 内置的 ping/pong
        print("\n=== WebSocket 内置 Ping/Pong 测试 ===")
        for i in range(5):
            t_start = time.perf_counter()
            pong_waiter = await ws.ping()
            await pong_waiter
            t_end = time.perf_counter()
            print(f"内置 Ping/Pong #{i + 1}: {(t_end - t_start) * 1000:.1f} ms")
            await asyncio.sleep(0.1)  # 避免过于频繁

        # 方法2: 发送实际消息测试往返延迟
        print("\n=== 消息往返延迟测试 ===")

        # 先发送一个订阅消息来建立通信
        subscribe_msg = {"method": "SUBSCRIBE", "params": ["ticker.SOL_USDC"], "id": 1}
        await ws.send(json.dumps(subscribe_msg))

        # 等待订阅确认
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=2.0)
            print(f"订阅响应: {response[:100]}...")
        except asyncio.TimeoutError:
            print("订阅超时，继续测试...")

        # 测试消息往返时间
        for i in range(5):
            test_msg = {
                "method": "PING",
                "id": f"ping_test_{i + 1}_{int(time.time() * 1000)}",
            }

            t_start = time.perf_counter()
            await ws.send(json.dumps(test_msg))

            # 等待响应（可能是 pong 或其他消息）
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=1.0)
                t_end = time.perf_counter()
                print(f"消息往返 #{i + 1}: {(t_end - t_start) * 1000:.1f} ms")
            except asyncio.TimeoutError:
                print(f"消息往返 #{i + 1}: 超时 (>1000 ms)")

            await asyncio.sleep(0.2)  # 避免过于频繁

        await ws.close()


asyncio.run(main())
