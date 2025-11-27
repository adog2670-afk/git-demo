# 老鹰捉小鸡策略 (Eagle Chicken Strategy)

## 概述

老鹰捉小鸡策略是一个双边做市策略，通过在一个交易所做市（Maker）并在其他交易所对冲（Taker）来获取价差收益。

## 策略流程

### 核心阶段

1. **Maker开仓阶段** - 在指定交易所挂限价单
2. **Taker对冲阶段** - 成交后立即在其他交易所市价对冲
3. **持仓阶段** - 持有仓位一段随机时间
4. **Maker平仓阶段** - 挂限价单平仓
5. **Taker平仓对冲阶段** - 对冲平仓仓位

### 关键特性

- **多账户管理**: 支持1个Maker账户 + 多个Taker账户
- **部分成交处理**: 支持订单部分成交的即时对冲
- **超时机制**: 3秒订单超时，自动取消并重新挂单
- **仓位跟踪**: 实时跟踪所有账户仓位，确保一致性
- **优雅退出**: 支持信号处理，退出时自动清理仓位
- **定期清理**: T轮后进行全量平仓清理

## 安装和配置

### 1. 依赖安装

```bash
pip install pyyaml asyncio
```

### 2. 配置文件

复制并修改配置模板：

```bash
cp config/eagle_chicken_config.yaml config/my_config.yaml
```

配置文件结构：

```yaml
# 基础交易配置
ticker: "SOL-PERP"
quantity: 0.1
tick_size: 0.01
maker_tick_offset: 1

# 账户配置
accounts:
  - name: "maker_backpack"
    exchange: "backpack"
    credentials:
      BACKPACK_PUBLIC_KEY: "your_backpack_public_key"
      BACKPACK_SECRET_KEY: "your_backpack_secret_key"
    symbols:
      SOL-PERP: "SOL_USDC"
  
  - name: "taker_lighter_1"
    exchange: "lighter"
    credentials:
      API_KEY_PRIVATE_KEY: "your_lighter_private_key"
      LIGHTER_ACCOUNT_INDEX: "0"
      LIGHTER_API_KEY_INDEX: "0"
    symbols:
      SOL-PERP: "SOL-PERP"

# 账户角色
maker_account: "maker_backpack"
taker_accounts: ["taker_lighter_1"]

# 时间配置
min_holding_time: 1000  # 毫秒
max_holding_time: 3000  # 毫秒
cleanup_rounds: 10      # 轮次

# 超时配置
order_timeout: 3        # 秒
cancel_timeout: 5       # 秒
```

### 3. API密钥配置

在配置文件中设置各交易所的API密钥：

- **Backpack**: `BACKPACK_PUBLIC_KEY`, `BACKPACK_SECRET_KEY`
- **Lighter**: `API_KEY_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`

## 运行策略

### 启动命令

```bash
python run_eagle_chicken.py config/my_config.yaml
```

### 输出示例

```
============================================================
老鹰捉小鸡策略 - Eagle Chicken Strategy
============================================================
配置文件: config/my_config.yaml
正在启动策略...

交易对: SOL-PERP
交易数量: 0.1
Maker账户: maker_backpack
Taker账户: taker_lighter_1, taker_lighter_2
持仓时间: 1000-3000ms
清理轮次: 10

策略初始化完成，开始交易...
使用 Ctrl+C 优雅退出
------------------------------------------------------------
[INFO] 初始化老鹰捉小鸡策略...
[INFO] 已连接到 backpack 交易所 (账户: maker_backpack)
[INFO] 已连接到 lighter 交易所 (账户: taker_lighter_1)
[INFO] 策略初始化完成
[INFO] 开始运行老鹰捉小鸡策略
[INFO] Maker开仓单已下: buy 0.1 @ 142.50
```

### 优雅退出

使用 `Ctrl+C` 退出策略，系统会：

1. 取消所有活跃订单
2. 平仓所有持仓
3. 断开交易所连接
4. 保存交易记录

## 监控和日志

### 日志级别

- **INFO**: 正常交易信息
- **WARNING**: 警告信息（超时、清理等）
- **ERROR**: 错误信息

### 关键监控指标

- 仓位一致性检查
- 订单成交状态
- 对冲执行情况
- 轮次完成统计

## 风险控制

### 内置风险控制

1. **仓位一致性检查**: 实时检查Maker和Taker仓位是否匹配
2. **超时保护**: 订单超时自动取消，避免长时间暴露
3. **定期清理**: 定期全量平仓，重置仓位状态
4. **错误状态处理**: 遇到错误自动停止策略

### 建议的外部风控

1. **资金管理**: 合理设置交易数量，不超过账户资金的一定比例
2. **市场监控**: 关注市场波动，在极端行情下暂停策略
3. **网络监控**: 确保网络连接稳定，避免连接中断导致的风险
4. **API限制**: 注意交易所API调用频率限制

## 故障排除

### 常见问题

1. **连接失败**
   - 检查API密钥是否正确
   - 确认网络连接正常
   - 验证交易所服务状态

2. **订单失败**
   - 检查账户余额是否充足
   - 确认交易对是否正确
   - 验证价格和数量参数

3. **仓位不一致**
   - 检查网络延迟问题
   - 确认所有交易所连接正常
   - 手动检查各账户实际仓位

### 调试模式

修改配置文件中的日志级别为 `DEBUG` 获取更详细的日志信息。

## 性能优化

### 建议配置

- **网络**: 使用低延迟网络连接
- **服务器**: 选择靠近交易所的服务器位置
- **并发**: 合理设置异步任务数量
- **频率**: 根据交易所限制调整请求频率

## 免责声明

本策略仅供学习和研究使用。实际交易存在风险，可能导致资金损失。使用前请：

1. 充分理解策略逻辑和风险
2. 在测试环境充分验证
3. 从小资金开始测试
4. 持续监控策略表现

**投资有风险，入市需谨慎！**