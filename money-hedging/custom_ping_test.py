import asyncio
import time
import json
from custom_websocket import CustomWebSocketClient


async def test_custom_websocket():
    """测试自定义 WebSocket 实现的延迟"""
    url = "wss://ws.backpack.exchange"
    
    print("=== 自定义 WebSocket 客户端测试 ===")
    print(f"连接到: {url}")
    
    # 创建自定义 WebSocket 客户端
    ws = CustomWebSocketClient(url)
    
    # 测试连接和握手
    print("\n1. 测试连接和握手...")
    start_time = time.perf_counter()
    
    if not await ws.connect():
        print("❌ 连接失败")
        return
    
    handshake_time = (time.perf_counter() - start_time) * 1000
    print(f"✅ 握手成功，耗时: {handshake_time:.1f} ms")
    
    # 测试 Ping/Pong 延迟
    print("\n2. 测试 Ping/Pong 延迟...")
    ping_times = []
    
    for i in range(5):
        try:
            # 使用不同的 payload 来确保响应匹配
            payload = f"ping_{i+1}_{int(time.time()*1000)}".encode('utf-8')
            ping_time = await ws.ping(payload)
            ping_times.append(ping_time)
            print(f"Ping #{i+1}: {ping_time:.1f} ms")
            
            # 避免过于频繁的请求
            await asyncio.sleep(0.2)
            
        except Exception as e:
            print(f"Ping #{i+1} 失败: {e}")
    
    if ping_times:
        avg_ping = sum(ping_times) / len(ping_times)
        min_ping = min(ping_times)
        max_ping = max(ping_times)
        print(f"\n📊 Ping 统计:")
        print(f"   平均延迟: {avg_ping:.1f} ms")
        print(f"   最小延迟: {min_ping:.1f} ms")
        print(f"   最大延迟: {max_ping:.1f} ms")
    
    # 测试消息发送和接收
    print("\n3. 测试消息收发...")
    try:
        # 发送订阅消息
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": ["ticker.SOL_USDC"],
            "id": 1
        }
        
        send_start = time.perf_counter()
        await ws.send_text(json.dumps(subscribe_msg))
        
        # 接收响应
        response = await asyncio.wait_for(ws.recv(), timeout=3.0)
        recv_time = (time.perf_counter() - send_start) * 1000
        
        if response:
            print(f"✅ 消息往返成功，耗时: {recv_time:.1f} ms")
            print(f"   响应: {str(response)[:100]}...")
        else:
            print("❌ 未收到响应")
            
    except asyncio.TimeoutError:
        print("❌ 消息接收超时")
    except Exception as e:
        print(f"❌ 消息收发失败: {e}")
    
    # 测试连续 ping 的稳定性
    print("\n4. 测试连续 ping 稳定性...")
    stable_pings = []
    
    for i in range(10):
        try:
            payload = f"stable_{i}".encode('utf-8')
            ping_time = await ws.ping(payload)
            stable_pings.append(ping_time)
            print(f"稳定性测试 #{i+1}: {ping_time:.1f} ms")
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"稳定性测试 #{i+1} 失败: {e}")
            break
    
    if stable_pings:
        # 计算延迟的标准差来评估稳定性
        import statistics
        avg = statistics.mean(stable_pings)
        stdev = statistics.stdev(stable_pings) if len(stable_pings) > 1 else 0
        print(f"\n📈 稳定性统计:")
        print(f"   平均延迟: {avg:.1f} ms")
        print(f"   标准差: {stdev:.1f} ms")
        print(f"   变异系数: {(stdev/avg*100):.1f}%")
    
    # 关闭连接
    print("\n5. 关闭连接...")
    await ws.close()
    print("✅ 连接已关闭")


async def compare_with_websockets_library():
    """与标准 websockets 库进行对比测试"""
    print("\n" + "="*50)
    print("对比测试：自定义实现 vs websockets 库")
    print("="*50)
    
    # 测试自定义实现
    print("\n🔧 自定义 WebSocket 实现:")
    custom_times = []
    
    ws_custom = CustomWebSocketClient("wss://ws.backpack.exchange")
    if await ws_custom.connect():
        for i in range(3):
            try:
                ping_time = await ws_custom.ping(f"custom_{i}".encode())
                custom_times.append(ping_time)
                print(f"  Ping #{i+1}: {ping_time:.1f} ms")
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"  Ping #{i+1} 失败: {e}")
        await ws_custom.close()
    
    # 测试标准库
    print("\n📚 标准 websockets 库:")
    import websockets
    standard_times = []
    
    try:
        async with websockets.connect("wss://ws.backpack.exchange", ping_interval=None) as ws:
            for i in range(3):
                try:
                    start = time.perf_counter()
                    pong_waiter = await ws.ping()
                    await pong_waiter
                    ping_time = (time.perf_counter() - start) * 1000
                    standard_times.append(ping_time)
                    print(f"  Ping #{i+1}: {ping_time:.1f} ms")
                    await asyncio.sleep(0.1)
                except Exception as e:
                    print(f"  Ping #{i+1} 失败: {e}")
    except Exception as e:
        print(f"  标准库连接失败: {e}")
    
    # 对比结果
    if custom_times and standard_times:
        custom_avg = sum(custom_times) / len(custom_times)
        standard_avg = sum(standard_times) / len(standard_times)
        
        print(f"\n📊 对比结果:")
        print(f"  自定义实现平均延迟: {custom_avg:.1f} ms")
        print(f"  标准库平均延迟: {standard_avg:.1f} ms")
        print(f"  差异: {abs(custom_avg - standard_avg):.1f} ms")
        
        if custom_avg < standard_avg:
            print(f"  🏆 自定义实现更快 {standard_avg - custom_avg:.1f} ms")
        else:
            print(f"  📚 标准库更快 {custom_avg - standard_avg:.1f} ms")


async def main():
    """主测试函数"""
    try:
        await test_custom_websocket()
        await compare_with_websockets_library()
    except KeyboardInterrupt:
        print("\n测试被用户中断")
    except Exception as e:
        print(f"\n测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())