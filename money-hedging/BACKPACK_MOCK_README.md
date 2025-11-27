# Backpack Mock Exchange 使用说明

## 概述

`backpack_mock.py` 是一个模拟的Backpack交易所实现，用于在不进行真实交易的情况下测试交易策略。

## 核心功能

### 1. 立即成交判断
- **买单**: 如果价格 >= 卖一价 (best_ask)，立即成交
- **卖单**: 如果价格 <= 买一价 (best_bid)，立即成交

### 2. 虚拟挂单
如果不能立即成交，会创建虚拟挂单：
- **买单**: 价格 = best_ask - tick_size (低于最优卖价)
- **卖单**: 价格 = best_bid + tick_size (高于最优买价)

### 3. 成交监听
- 订阅真实的交易数据流 `trades.{symbol}`
- **买单监听**: 当有任何成交价格 <= 买单价格时，认为买单被吃掉
- **卖单监听**: 当有任何成交价格 >= 卖单价格时，认为卖单被吃掉

## 使用方法

### 1. 修改配置文件
在你的策略配置文件中，将maker账户的exchange从 `backpack` 改为 `backpack_mock`:

```yaml
accounts:
  - name: "maker_account"
    exchange: "backpack_mock"  # 改为这里
    credentials: {}  # mock不需要真实凭证
    # ... 其他配置

maker_account: "maker_account"
taker_accounts:
  - "taker_account"  # 仍然使用真实的lighter或其他交易所
```

### 2. 运行策略
无需修改其他代码，直接运行原有的策略：
```bash
python eagle_chicken_stratge.py config.yaml
```

## 实现细节

### WebSocket连接
- 订阅 `trades.{symbol}` 获取真实成交数据
- 不需要认证，使用公开API

### 订单管理
- 虚拟订单存储在内存中: `self.virtual_orders: Dict[str, VirtualOrder]`
- 订单ID使用UUID生成
- 支持取消虚拟订单

### 回调机制
- 当虚拟订单被成交时，会触发与真实交易所相同的回调
- 回调格式与原始backpack.py保持一致

## 日志输出
Mock交易所会输出特殊标记的日志：
- `[MOCK-OPEN]`: 模拟开单
- `[MOCK-MARKET]`: 模拟市价单
- `[MOCK-CANCEL]`: 模拟撤单

## 优势
1. **零交易成本**: 不进行真实交易，避免手续费和滑点
2. **真实市场数据**: 基于真实的订单簿和成交数据
3. **策略测试**: 可以安全地测试策略逻辑
4. **快速迭代**: 无需担心资金风险，快速调试

## 注意事项
1. 只适用于maker账户，taker账户仍需使用真实交易所
2. 虚拟订单不会影响真实市场
3. 重启程序会清空所有虚拟订单
4. 网络断开可能导致虚拟订单状态不准确

## 故障排除

### 找不到合约ID
如果遇到 "Failed to get contract ID for ticker" 错误：
1. 检查配置文件中的ticker是否正确
2. 查看日志中的 "Available PERP markets" 列表
3. 确保ticker格式正确（如 "SOL" 或 "SOL_PERP"）

### 常见ticker格式
- Backpack: "SOL", "BTC", "ETH" (base symbol)
- 其他交易所可能需要完整符号: "SOL_PERP", "BTC-PERP"

### 调试模式
在配置中设置日志级别为DEBUG可以看到详细的市场搜索过程：
```yaml
logging:
  level: "DEBUG"
  console: true
```

## 示例配置
```yaml
ticker: "SOL"
quantity: "0.1"
maker_tick_offset: 1
no_chicken_mode: false
hedging_ratio: "100"  # 100倍对冲，主要交易lighter

accounts:
  - name: "backpack_maker"
    exchange: "backpack_mock"
    credentials: {}
    ticker: "SOL_PERP"

  - name: "lighter_taker"
    exchange: "lighter"
    credentials:
      LIGHTER_API_KEY: "your_api_key"
      LIGHTER_SECRET_KEY: "your_secret_key"
    ticker: "SOL-PERP"

maker_account: "backpack_maker"
taker_accounts:
  - "lighter_taker"
```