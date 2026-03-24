# A 股模拟交易逻辑深度 Review 与分析

本文档旨在针对 A 股交易的特殊规则（如 T+1、涨跌停板、订单数量限制等），对本项目中的 `vnpy_signal_strategy`（信号策略层）和 `vnpy_qmt_sim`（模拟柜台网关层）中的报单、撤单、拒单及部分成交等逻辑进行深度的 Review 与分析，并提出改进建议。

## 1. A 股实盘交易核心规则概述

在进行模拟交易系统设计时，必须充分考虑 A 股的以下核心交易规则：

1. **交易时间**：
   - 集合竞价：09:15 - 09:25
   - 连续竞价：09:30 - 11:30，13:00 - 14:57
   - 收盘集合竞价：14:57 - 15:00
2. **T+1 交收制度**：
   - **资金**：卖出股票后的资金当日可用（可用于买入其他股票），次日可取。
   - **持仓**：当日买入的股票（`volume`）当日不可卖出，必须等到下一个交易日转为可用持仓（`yd_volume`，即昨仓）后方可卖出。
3. **价格限制（涨跌停板）**：
   - 主板通常为上一交易日收盘价的 ±10%（ST 股为 ±5%），创业板和科创板为 ±20%。
   - 委托价格超出涨跌停限制的报单将被交易所视为废单（拒单）。
4. **报单数量限制**：
   - 买入：必须是 100 股（1 手）的整数倍。
   - 卖出：单笔申报可以包含零股，但如果卖出，必须将该股票的零股一次性卖出（或者按照 100 股的整数倍卖出，剩余零股最后一次性卖出）。

---

## 2. 报单生命周期与状态流转

在 VeighNa (vn.py) 框架中，一笔委托（Order）的典型状态流转如下：

```mermaid
stateDiagram-v2
    [*] --> SUBMITTING: 发起委托 (send_order)
    SUBMITTING --> NOTTRADED: 柜台接收 (交易所已确认)
    SUBMITTING --> REJECTED: 柜台/交易所拒单 (资金/仓位不足, 价格越界)
    
    NOTTRADED --> PARTTRADED: 部分成交
    PARTTRADED --> PARTTRADED: 再次部分成交
    
    NOTTRADED --> ALLTRADED: 全部成交
    PARTTRADED --> ALLTRADED: 剩余部分成交
    
    NOTTRADED --> CANCELLED: 用户撤单成功
    PARTTRADED --> CANCELLED: 剩余部分撤单成功
    
    REJECTED --> [*]
    ALLTRADED --> [*]
    CANCELLED --> [*]
```

---

## 3. `vnpy_signal_strategy` 策略层逻辑 Review

当前策略（如 `MySQLSignalStrategy`）负责从数据库轮询信号并进行发单。

### 3.1 报单逻辑 (Order Placement)
策略读取到 `BUY` 或 `SELL` 信号后，会计算目标数量并调用 `send_order`。

```mermaid
sequenceDiagram
    participant DB as MySQL 数据库
    participant Stg as 信号策略 (MySQLSignalStrategy)
    participant Engine as 策略引擎 (SignalEngine)
    participant SimGW as 模拟网关 (QmtSimGateway)

    loop 定时轮询 (Poll)
        Stg->>DB: 查询未处理信号
        DB-->>Stg: 返回信号 (代码, 比例, 方向, 价格)
        
        alt 方向 = 买入 (BUY)
            Stg->>Stg: 计算委托数量 = (总资金 * 比例) / 价格
            Stg->>Stg: 向下取整到 100 的整数倍 (A股规则)
        else 方向 = 卖出 (SELL)
            Stg->>Engine: 查询当前持仓 (get_position)
            Engine-->>Stg: 返回 PositionData
            Stg->>Stg: 计算委托数量 = 持仓总量 * 比例
        end
        
        Stg->>Engine: send_order(委托)
        Engine->>SimGW: send_order(委托)
    end
```

**✅ 已实现的优化**：
- **资金不足延时重挂**：策略引入 `AutoResubmitMixinPlus`，识别买单“可用资金不足/260200”拒单后采用退避延时重试，避免拒单死循环。

---

## 4. `vnpy_qmt_sim` 模拟柜台逻辑 Review

`SimulationCounter` 负责模拟真实的 QMT 柜台行为。

### 4.1 模拟报单与撮合逻辑

模拟柜台收到报单后，需要进行合法性校验，并模拟撮合过程。

```mermaid
flowchart TD
    A[收到发单请求 send_order] --> B{校验报单属性}
    
    B -->|买单| C{校验可用资金}
    C -->|不足| D[状态: REJECTED\n拒单: 资金不足]
    C -->|充足| E[冻结资金\n可用资金 -= 委托金额]
    
    B -->|卖单| F{校验可用持仓}
    F -->|可用昨仓 < 委托量| G[状态: REJECTED\n拒单: 可用持仓不足]
    F -->|可用昨仓 >= 委托量| H[冻结持仓\nfrozen += 委托量]
    
    D --> End[推送 EVENT_ORDER]
    G --> End
    
    E --> I[状态: SUBMITTING --> NOTTRADED]
    H --> I
    
    I --> J{模拟撮合机制\n当前为市价/限价立即成交}
    J -->|模拟拒单\n随机或限价不满足| K[解冻资金/持仓\n状态: REJECTED]
    J -->|模拟部分成交| L[生成 TradeData\n解冻部分资金/持仓\n增加持有\n状态: PARTTRADED]
    J -->|模拟全部成交| M[生成 TradeData\n解冻全部资金/持仓\n增加持有\n状态: ALLTRADED]
    
    K --> End
    L --> End2[推送 EVENT_TRADE\n推送 EVENT_ORDER]
    M --> End2
```

### 4.2 撤单逻辑 (Cancel Order)

撤单是解除冻结资金或持仓的关键步骤。

```mermaid
sequenceDiagram
    participant Stg as 策略 (Strategy)
    participant GW as 模拟网关 (Gateway)
    participant Sim as 模拟柜台 (SimCounter)
    participant Event as 事件引擎 (EventEngine)

    Stg->>GW: cancel_order(vt_orderid)
    GW->>Sim: cancel_order(vt_orderid)
    
    Sim->>Sim: 查找 Order
    alt 订单不存在 或 状态为已结束(ALLTRADED/REJECTED/CANCELLED)
        Sim-->>GW: 忽略或返回错误
    else 订单存活 (NOTTRADED / PARTTRADED)
        Sim->>Sim: 修改状态为 CANCELLED
        
        alt 订单方向 = BUY
            Sim->>Sim: 账户可用资金 += (未成交数量 * 委托价格)
        else 订单方向 = SELL
            Sim->>Sim: 持仓冻结数量 (frozen) -= 未成交数量
        end
        
        Sim->>GW: on_order(CANCELLED)
        GW->>Event: EVENT_ORDER (撤单回报)
        Sim->>GW: on_account / on_position
        GW->>Event: EVENT_ACCOUNT / EVENT_POSITION (资产更新)
    end
```

### 4.3 核心问题：T+0 与 T+1 模拟的差距

目前 `SimulationCounter` 在处理成交时，采用的是 **T+0** 逻辑。这意味着买入成交后，立即增加了 `volume`，而卖出时仅校验 `volume`。

**要完全模拟 A 股，需在 `SimulationCounter` 层面实现以下 T+1 逻辑**：

1. **持仓数据结构扩展**：
   明确区分 `volume`（总持仓）、`yd_volume`（昨仓，即当前可卖持仓）、`frozen`（挂单冻结持仓）。
2. **买入成交 (Buy Fill)**：
   - 增加总持仓：`volume += traded_volume`。
   - **不增加** `yd_volume`。
3. **卖出委托 (Sell Order)**：
   - 校验条件：`order_volume <= (yd_volume - frozen)`。
   - 委托成功后，冻结增加：`frozen += order_volume`。
4. **卖出成交 (Sell Fill)**：
   - 扣减总持仓与昨仓：`volume -= traded_volume`, `yd_volume -= traded_volume`。
   - 扣减冻结：`frozen -= traded_volume`。
5. **日终结算 (End of Day Settlement)**：
   - 模拟网关需提供一个日切接口，在每个交易日结束（或启动时），将当日的 `volume` 结转为次日的 `yd_volume`：
   - `yd_volume = volume`

---

## 5. 总结

当前的 `vnpy_signal_strategy` 和 `vnpy_qmt_sim` 已经具备了完整的异步事件驱动架构，能够正确处理委托、成交、拒单等基本流转。

然而，针对 A 股实盘的严苛要求，当前的模拟层还需在以下几点进行加固：
1. **策略层**：计算卖出数量时，应优先查询并使用 `yd_volume - frozen` 作为最大可卖数量。
2. **模拟柜台层**：引入 T+1 的持仓隔离机制与日终结转机制，完善资金与持仓的“冻结->解冻/扣减”生命周期，以真实反映 A 股的清算规则。
