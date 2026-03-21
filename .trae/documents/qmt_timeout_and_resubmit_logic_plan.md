# QmtSim 超时撤单归属与部分成交处理计划

## 目标

1. 将 `check_order_timeout` 的“定时触发”从模拟柜台迁移到 `QmtSimGateway` 的事件循环中执行，更贴近真实柜台行为（柜台不主动超时撤单，撤单策略应由客户端/网关侧驱动）。
2. 修正当前 `check_order_timeout` 的状态筛选逻辑，避免把 `REJECTED/CANCELLED` 等终态订单重复处理，确保 **部分成交** 超时撤单仅影响“未成交部分”，并为后续“自动撤单重挂”提供准确输入（剩余量）。

---

## 需求解读与结论（对应你的两个问题）

### 问题 1：功能放哪更合理？

- **更合理的归属**：把“定时检查 + 超时撤单动作”放在 `QmtSimGateway`（或更上层的交易管理/策略层）执行，而不是模拟柜台撮合核心。
- **触发方式**：在 `QmtSimGateway.connect()` 中注册 `EVENT_TIMER` 回调，由事件引擎每秒触发一次；回调里再按节流（例如每 N 秒）执行一次超时扫描。
- **柜台层（SimulationCounter）职责**：只提供“查询当前订单集合/时间戳/冻结资金释放/撤单执行”的能力；不主动自行跑定时任务。

### 问题 2：当前 “除 ALLTRADED 外全部撤单” 的风险点

当前实现（你最新代码）里：`order.status not in [Status.ALLTRADED]` 会把以下状态都纳入撤单：
- `REJECTED`：已终态，不应再超时撤单；若 sell 单无冻结，会留下 `order_submit_time`，后续会被错误撤单，导致订单事件流异常（REJECTED → CANCELLED）。
- `CANCELLED`：已终态，不应再次处理。
- `PARTTRADED`：**可以**纳入“超时撤单”，但撤单动作应只影响剩余未成交部分：订单 `traded` 保留、持仓保留、只释放“剩余冻结现金”，然后把订单状态置为 `CANCELLED`。

因此应将超时处理状态严格限定为：
- `SUBMITTING`、`NOTTRADED`、`PARTTRADED`
并且增加条件：
- `order.traded < order.volume`

---

## 实施步骤（按文件）

### 步骤 A：把定时触发放到 `QmtSimGateway`

文件：`vnpy_qmt_sim/gateway.py`

1. 在 `QmtSimGateway` 增加成员：
   - `_timer_count: int`（用于节流，比如每 5 秒执行一次超时检查）
   - `order_timeout: int`（从 setting 读取，默认 30）
2. 在 `connect()` 内注册：
   - `self.event_engine.register(EVENT_TIMER, self.process_timer_event)`
3. 实现 `process_timer_event(event)`：
   - 计数器累加并节流
   - 调用 `self.td.counter.check_order_timeout(...)`（或新的 `self.td.process_order_timeout()`）执行扫描与撤单
4. 在 `close()` 中注销回调：
   - `self.event_engine.unregister(EVENT_TIMER, self.process_timer_event)`

说明：这是“客户端/网关侧超时撤单”的正确位置；真实柜台不负责该逻辑。

### 步骤 B：修正 `check_order_timeout` 的状态筛选与时间戳清理

文件：`vnpy_qmt_sim/td.py`

1. `check_order_timeout()` 的筛选条件修改为：
   - 仅处理 `SUBMITTING/NOTTRADED/PARTTRADED`
   - 并且 `order.traded < order.volume`
2. 对于被撤单的订单：
   - 调用 `release_order_frozen_cash(orderid)` 释放剩余冻结（买单会有剩余冻结；卖单一般无冻结）
   - 置 `order.status = Status.CANCELLED`
   - `order_submit_time.pop(orderid, None)`
   - `gateway.on_order(order)` 推送事件
3. 补齐“终态订单的 submit_time 清理”：
   - 资金不足拒单（`send_order` 内设置 `REJECTED`）时：立即 `order_submit_time.pop(orderid, None)`
   - `match_order` 随机拒单（`REJECTED`）时：同样 pop
   - `ALLTRADED` 时：完成成交后 pop（避免字典残留）

### 步骤 C：部分成交撤单后的“只重挂未成交部分”

文件：不一定要改核心（主要是策略/上层逻辑）

1. 超时撤单把订单状态推为 `CANCELLED`，但 `order.traded` 保留。
2. 自动重挂逻辑（如果你启用）应使用：
   - `remain = order.volume - order.traded`
   - 只对 `remain > 0` 的订单重挂

---

## 回归验证清单

1. **拒单（资金不足 / 随机拒单）**：
   - 订单状态停留在 `REJECTED`，不会在超时后变成 `CANCELLED`
   - 不产生成交、不更新持仓、不改变 balance/frozen
2. **未成交超时撤单**：
   - `NOTTRADED → CANCELLED`，冻结释放正确
3. **部分成交超时撤单**：
   - `PARTTRADED → CANCELLED`，已成交部分持仓保留
   - 买单剩余冻结释放，余额不发生二次扣减
4. **全成交通常不会进入超时**：
   - `ALLTRADED` 订单不会被扫描到（submit_time 清理）

---

## 输出交付

- 修改：`vnpy_qmt_sim/gateway.py`、`vnpy_qmt_sim/td.py`
- 新增/更新：超时参数从 gateway setting 透传（可选）
- 保持：策略层可按需实现“撤单→重挂”，重挂量使用 `volume-traded`

