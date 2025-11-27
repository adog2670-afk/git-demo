# 老鹰捉小鸡策略设计文档

## 策略概述

老鹰捉小鸡策略是一个双边做市策略，通过maker方挂单获取流动性奖励，同时使用taker方进行风险对冲，实现低风险套利。

## 核心组件

### 1. 账户配置
- **账户池**: 多个账户的集合（可配置交易所：backpack或lighter），每个账户配置自己对应的名称，交易所，所需的鉴权信息（如public key, secret key, private key等）
- **Maker账户**: 从账户池中选择一个作为maker方
- **Taker账户池**: 从账户池中选择多个作为taker方
- **持仓时间范围**: maker开仓以后，并且taker对冲完成后，持仓时间（默认1000~3000毫秒，设置0则立即关仓），配置为两个值，分别表示最小持仓时间和最大持仓时间，之前随机
- **轮次配置**: T轮后进行全量平仓清理

### 2. 策略流程

#### 阶段1: Maker挂单
1. Maker方挂最优价格的限价单（maker单）
2. 设置3秒超时机制
3. 如果3秒内未成交，取消订单重新挂单
4. 支持部分成交处理，立即对冲已成交部分

#### 阶段2: Taker对冲
1. 收到maker单成交回调后，立即触发对冲
2. 从taker账户池中随机选择一个账户
3. 挂等量反向的市价单进行对冲
4. 支持部分成交的即时对冲

#### 阶段3: Maker平仓
1. Taker对冲单成交后，等待持仓时间随机值后，maker方开始平仓
2. 使用最优maker价格挂反向限价单
3. 同样支持3秒超时和部分成交处理，没成交的部分取消订单

#### 阶段4: Taker平仓对冲
1. Maker平仓单成交后，随机选择taker账户
2. 对平仓部分进行反向市价对冲
3. 完成一轮做市循环

#### 阶段5: 定期清理
1. 每T轮后检查maker方是否完全平仓
2. 如果有完全平仓，挂单全部平仓，直到净仓位为0，同时不要忘记任何一次成交都要有taker方的市价对冲
3. maker方完全平仓，对所有taker账户进行市价平仓
4. 重置轮次计数器

### 3. 订单确认机制
订单会通过WebSocket实时更新状态，确保订单的及时确认。
如果超时还未成交，在取消之前需要获取一次最新订单状态，确保没有成交，再取消订单。

### 4. SDK使用
基于exchanges/base.py里面的 BaseExchangeClient 类 实现订单的创建、查询、取消、修改等操作。
交易下单、监听、等待成交、取消订单等操作的部分完全模仿trading_bot.py来写

## 关键技术细节

### 1. 部分成交处理
```
核心原则：任何部分成交都要立即进行对应的对冲操作

Maker单部分成交处理：
- 监听WebSocket订单更新
- 收到PARTIALLY_FILLED状态时立即对冲已成交部分
- 3秒超时后取消剩余未成交部分
- 不再重新挂单，进入等待taker成交状态

Taker单部分成交处理：
- 市价单通常会立即全部成交，理论上不会存在部分成交情况
- 如有部分成交，就取消再挂一次

平仓单部分成交处理：
- 与开仓逻辑相同
- 部分成交立即对冲，超时取消剩余部分
```

### 2. 仓位跟踪机制
```
需要实时跟踪的关键数据：
- maker_open_position: maker方开仓仓位
- maker_close_position: maker方平仓仓位  
- taker_hedge_positions: 各taker账户的对冲仓位
- net_position: 总净仓位（理论上应该接近0）

仓位同步检查：
- 每次操作后验证仓位一致性
- maker_open_position - maker_close_position = sum(taker_hedge_positions)
- 发现不一致时报警并停止策略
```

### 3. 订单状态管理
```
订单生命周期状态：
- PENDING: 订单已提交，等待确认
- OPEN: 订单已确认，等待成交
- PARTIALLY_FILLED: 部分成交
- FILLED: 完全成交
- CANCELED: 已取消
- FAILED: 下单失败

状态转换处理：
- 使用WebSocket实时监听状态变化
- 每个状态变化都触发相应的业务逻辑
- 异常状态需要人工介入处理
```

### 4. 超时机制
```
3秒超时逻辑：
1. 下单后启动3秒计时器
2. 如果3秒内收到FILLED状态，正常流程继续
3. 如果3秒内收到PARTIALLY_FILLED，对已成交部分进行对冲，取消剩余部分
4. 如果3秒内完全未成交，取消整个订单
5. 取消操作也需要超时保护（最多等待5秒）

超时后的处理：
- 开仓超时：如有部分成交则取消未成交部分，否则重新挂单
- 平仓超时：如有部分成交则取消未成交部分，否则重新挂单
```

### 5. 随机账户选择
```
Taker账户池管理：
- 维护可用账户列表
- 每次对冲时使用随机算法选择账户

随机选择算法：
- 简单随机：random.choice(taker_accounts)
```

### 6. 价格获取机制
```
最优价格获取：
- 实时获取买一卖一价格（BBO）
- Maker单使用对向的最优价格-x个tick_size。比如买入时使用卖一价-x个tick_size，卖出时使用买一价+x个tick_size。确保下单能够成为Maker而不是直接成交。

价格更新频率：
- 每次下单前获取最新价格
- WebSocket价格推送实时更新
```

## 风险控制

### 1. 仓位风险
- 设置最大单笔交易量限制
- 设置总仓位上限
- 异常仓位自动平仓机制

### 2. 网络风险
- WebSocket断线重连机制
- API调用失败重试机制
- 订单状态不一致时的修复机制

### 3. 市场风险
- 价格剧烈波动时暂停策略
- 流动性不足时降低交易频率
- 异常价差时停止交易

## 其他关键细节
- 不使用.env文件配置，全部用yaml
- 交易对可能对于不同交易所不一样，所以需要一个列表来配置，下单的时候要根据账号对应的交易所来自动选择
- 要考虑让用户想停止脚本的时候，要怎么操作。是不是监控SIGINT信号，收到信号后，先取消所有订单，进行仓位清理，然后退出脚本。这期间要给出彩色命令行日志，以便用户知道脚本正在退出。

## 实现架构

### 1. 核心类设计
```python
class EagleChickenStrategy:
    """老鹰捉小鸡策略主类"""
    
class MakerManager:
    """Maker账户管理器"""
    
class TakerPoolManager:
    """Taker账户池管理器"""
    
class PositionTracker:
    """仓位跟踪器"""
    
class OrderManager:
    """订单管理器"""
```

### 2. 状态机设计
```python
class StrategyState(Enum):
    IDLE = "idle"                    # 空闲状态
    MAKER_OPENING = "maker_opening"  # Maker开仓中
    TAKER_HEDGING = "taker_hedging"  # Taker对冲中
    HOLDING = "holding"              # 持仓中
    MAKER_CLOSING = "maker_closing"  # Maker平仓中
    TAKER_CLOSING = "taker_closing"  # Taker平仓对冲中
    CLEANUP = "cleanup"              # 清理状态
    ERROR = "error"                  # 错误状态
```

### 3. 事件驱动架构
```python
class StrategyEvent:
    """策略事件基类"""
    
class OrderFilledEvent(StrategyEvent):
    """订单成交事件"""
    
class OrderCanceledEvent(StrategyEvent):
    """订单取消事件"""
    
class TimeoutEvent(StrategyEvent):
    """超时事件"""
```

## 监控和日志

### 1. 关键指标监控
- 成交率统计
- 盈亏统计
- 仓位偏差监控
- 延迟监控

### 2. 日志记录
- 订单生命周期日志
- 仓位变化日志
- 异常事件日志
- 性能指标日志
- 使用sqlite来记录所有已成交的订单