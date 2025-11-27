import asyncio
import ssl
import socket
import base64
import hashlib
import struct
import os
import time
from urllib.parse import urlparse
from typing import Optional, Tuple, Union


class CustomWebSocketClient:
    """
    自定义 WebSocket 客户端实现，基于 TLS TCP 连接
    实现了完整的 WebSocket 协议，包括握手、帧编码/解码、Ping/Pong
    """
    
    # WebSocket 魔法字符串，用于握手
    WEBSOCKET_MAGIC_STRING = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    
    # WebSocket 操作码
    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xa
    
    def __init__(self, url: str):
        self.url = url
        self.parsed_url = urlparse(url)
        self.host = self.parsed_url.hostname
        self.port = self.parsed_url.port or (443 if self.parsed_url.scheme == 'wss' else 80)
        self.path = self.parsed_url.path or '/'
        if self.parsed_url.query:
            self.path += '?' + self.parsed_url.query
            
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        
    async def connect(self) -> bool:
        """建立 TLS TCP 连接并完成 WebSocket 握手"""
        try:
            # 创建 SSL 上下文
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            
            # 建立 TLS 连接
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port, ssl=ssl_context
            )
            
            # 执行 WebSocket 握手
            if await self._perform_handshake():
                self.connected = True
                return True
            else:
                await self.close()
                return False
                
        except Exception as e:
            print(f"连接失败: {e}")
            return False
    
    async def _perform_handshake(self) -> bool:
        """执行 WebSocket 握手协议"""
        # 生成随机的 Sec-WebSocket-Key
        key = base64.b64encode(os.urandom(16)).decode('utf-8')
        
        # 构建握手请求
        handshake_request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: CustomWebSocketClient/1.0\r\n"
            f"\r\n"
        )
        
        # 发送握手请求
        self.writer.write(handshake_request.encode('utf-8'))
        await self.writer.drain()
        
        # 读取响应
        response_data = b""
        while b"\r\n\r\n" not in response_data:
            chunk = await self.reader.read(1024)
            if not chunk:
                return False
            response_data += chunk
        
        response = response_data.decode('utf-8')
        lines = response.split('\r\n')
        
        # 检查状态码
        if not lines[0].startswith('HTTP/1.1 101'):
            print(f"握手失败，状态码: {lines[0]}")
            return False
        
        # 验证 Sec-WebSocket-Accept
        expected_accept = base64.b64encode(
            hashlib.sha1((key + self.WEBSOCKET_MAGIC_STRING).encode()).digest()
        ).decode('utf-8')
        
        for line in lines[1:]:
            if line.lower().startswith('sec-websocket-accept:'):
                actual_accept = line.split(':', 1)[1].strip()
                if actual_accept != expected_accept:
                    print(f"握手验证失败: 期望 {expected_accept}, 实际 {actual_accept}")
                    return False
                break
        else:
            print("握手响应中缺少 Sec-WebSocket-Accept")
            return False
        
        print("WebSocket 握手成功")
        return True
    
    def _create_frame(self, opcode: int, payload: bytes = b"", masked: bool = True) -> bytes:
        """创建 WebSocket 帧"""
        frame = bytearray()
        
        # 第一个字节: FIN=1, RSV=000, OPCODE
        frame.append(0x80 | opcode)
        
        # 负载长度和掩码位
        payload_len = len(payload)
        if payload_len < 126:
            frame.append((0x80 if masked else 0x00) | payload_len)
        elif payload_len < 65536:
            frame.append((0x80 if masked else 0x00) | 126)
            frame.extend(struct.pack('>H', payload_len))
        else:
            frame.append((0x80 if masked else 0x00) | 127)
            frame.extend(struct.pack('>Q', payload_len))
        
        # 掩码和负载
        if masked:
            mask = os.urandom(4)
            frame.extend(mask)
            # 应用掩码
            masked_payload = bytearray()
            for i, byte in enumerate(payload):
                masked_payload.append(byte ^ mask[i % 4])
            frame.extend(masked_payload)
        else:
            frame.extend(payload)
        
        return bytes(frame)
    
    async def _parse_frame(self) -> Optional[Tuple[int, bytes]]:
        """解析 WebSocket 帧"""
        try:
            # 读取前两个字节
            header = await self.reader.readexactly(2)
            if len(header) != 2:
                return None
            
            # 解析第一个字节
            fin = (header[0] & 0x80) != 0
            opcode = header[0] & 0x0f
            
            # 解析第二个字节
            masked = (header[1] & 0x80) != 0
            payload_len = header[1] & 0x7f
            
            # 读取扩展长度
            if payload_len == 126:
                len_data = await self.reader.readexactly(2)
                payload_len = struct.unpack('>H', len_data)[0]
            elif payload_len == 127:
                len_data = await self.reader.readexactly(8)
                payload_len = struct.unpack('>Q', len_data)[0]
            
            # 读取掩码（服务器发送的帧通常不带掩码）
            mask = None
            if masked:
                mask = await self.reader.readexactly(4)
            
            # 读取负载
            payload = b""
            if payload_len > 0:
                payload = await self.reader.readexactly(payload_len)
                
                # 应用掩码（如果有）
                if masked and mask:
                    unmasked_payload = bytearray()
                    for i, byte in enumerate(payload):
                        unmasked_payload.append(byte ^ mask[i % 4])
                    payload = bytes(unmasked_payload)
            
            return opcode, payload
            
        except Exception as e:
            print(f"解析帧失败: {e}")
            return None
    
    async def ping(self, payload: bytes = b"") -> float:
        """发送 Ping 帧并等待 Pong 响应，返回往返时间（毫秒）"""
        if not self.connected:
            raise RuntimeError("WebSocket 未连接")
        
        # 发送 Ping 帧
        ping_frame = self._create_frame(self.OPCODE_PING, payload)
        start_time = time.perf_counter()
        
        self.writer.write(ping_frame)
        await self.writer.drain()
        
        # 等待 Pong 响应
        while True:
            frame_data = await self._parse_frame()
            if frame_data is None:
                raise RuntimeError("连接已断开")
            
            opcode, response_payload = frame_data
            
            if opcode == self.OPCODE_PONG and response_payload == payload:
                end_time = time.perf_counter()
                return (end_time - start_time) * 1000  # 转换为毫秒
            elif opcode == self.OPCODE_CLOSE:
                raise RuntimeError("服务器关闭连接")
            # 忽略其他类型的帧，继续等待 Pong
    
    async def send_text(self, text: str):
        """发送文本消息"""
        if not self.connected:
            raise RuntimeError("WebSocket 未连接")
        
        frame = self._create_frame(self.OPCODE_TEXT, text.encode('utf-8'))
        self.writer.write(frame)
        await self.writer.drain()
    
    async def recv(self) -> Optional[Union[str, bytes]]:
        """接收消息"""
        if not self.connected:
            raise RuntimeError("WebSocket 未连接")
        
        frame_data = await self._parse_frame()
        if frame_data is None:
            return None
        
        opcode, payload = frame_data
        
        if opcode == self.OPCODE_TEXT:
            return payload.decode('utf-8')
        elif opcode == self.OPCODE_BINARY:
            return payload
        elif opcode == self.OPCODE_CLOSE:
            self.connected = False
            return None
        elif opcode == self.OPCODE_PING:
            # 自动回复 Pong
            pong_frame = self._create_frame(self.OPCODE_PONG, payload)
            self.writer.write(pong_frame)
            await self.writer.drain()
            return await self.recv()  # 继续接收下一个消息
        elif opcode == self.OPCODE_PONG:
            # Pong 帧通常由 ping() 方法处理
            return await self.recv()  # 继续接收下一个消息
        
        return None
    
    async def close(self):
        """关闭连接"""
        if self.writer:
            if self.connected:
                # 发送关闭帧
                close_frame = self._create_frame(self.OPCODE_CLOSE)
                self.writer.write(close_frame)
                await self.writer.drain()
            
            self.writer.close()
            await self.writer.wait_closed()
        
        self.connected = False
        self.reader = None
        self.writer = None