# AutoResubmit 与超时撤单职责重构计划

## 一、需求理解与设计结论

### 需求 1：是否删除 `on_timer_for_resubmit`
结论：**不建议删除，建议保留并简化职责**。

原因：
1. 仅在 `on_order_for_resubmit` 里立即重挂会对 `REJECTED(资金不足)` 形成“事件内连环重提”，短时间耗尽重试次数，且可能造成日志风暴。
2. `on_timer_for_resubmit` 能提供“节流 + 延迟重试”，特别适合“先卖后买”的资金释放场景（卖单成交后再重试买单）。
3. 当前 `MySQLSignalStrategy.on_timer()` 已接入该函数，直接删除会破坏现有调用链。

优化方向：
- `on_order_for_resubmit` 只负责“记录待重挂任务”。
- `on_timer_for_resubmit` 只负责“按间隔执行重挂”。
- 保持逻辑简洁，不引入网关层重试队列。

### 需求 2：新增函数必须有注释
结论：本次对新增/改造函数统一补充简洁 docstring，说明输入、行为、边界条件。

---

## 二、代码改造范围

1. `vnpy_qmt_sim/gateway.py`
2. `vnpy_signal_strategy/auto_resubmit.py`
3. `vnpy_signal_strategy/mysql_signal_strategy.py`

---

## 三、实施步骤

### 步骤 A：QmtSimGateway 保持“只做超时撤单”
目标：落实“超时检查属于网关，不属于柜台”。

实施：
1. 保留 `process_timer_event` + `check_order_timeout` 在 `QmtSimGateway`。
2. `check_order_timeout` 仅处理可撤单活动状态：`SUBMITTING/NOTTRADED/PARTTRADED`。
3. 撤单后只推送 `CANCELLED`，不负责重挂。
4. 为 `process_timer_event` 和 `check_order_timeout` 增加函数注释。

### 步骤 B：AutoResubmitMixin 统一处理 CANCELLED/REJECTED 重挂
目标：在策略层合并“超时撤单重挂”和“资金不足拒单重挂”。

实施：
1. `on_order_for_resubmit(order)`：
   - 判断是否满足重挂条件；
   - 将剩余量 `volume-traded` 放入待重挂队列；
   - 不在该函数内直接递归下单。
2. `on_timer_for_resubmit()`：
   - 按 `resubmit_interval` 节流；
   - 对队列中的任务尝试下单；
   - 下单成功后更新“新订单ID重试计数”；
   - 达到 `resubmit_limit` 后放弃。
3. `should_auto_resubmit` 补充规则：
   - 只处理 `CANCELLED/REJECTED`；
   - `traded < volume`；
   - 重试次数未达上限。
4. 为以上函数全部补充 docstring。

### 步骤 C：MySQLSignalStrategy 接口对齐
目标：保持策略调用链清晰、最小改动。

实施：
1. `on_order` 只调用 `on_order_for_resubmit`。
2. `on_timer` 只调用 `on_timer_for_resubmit`。
3. 补充必要函数注释（若缺失）。

---

## 四、验证计划

1. 语法验证：
   - `python -m py_compile vnpy_qmt_sim/gateway.py`
   - `python -m py_compile vnpy_signal_strategy/auto_resubmit.py`
   - `python -m py_compile vnpy_signal_strategy/mysql_signal_strategy.py`

2. 行为验证场景：
   - 场景 1：订单超时 -> 网关置 `CANCELLED` -> 策略加入重挂队列 -> 定时重挂。
   - 场景 2：买单资金不足 `REJECTED` -> 策略加入重挂队列 -> 卖单成交释放资金后，后续定时重挂成功。
   - 场景 3：达到 `resubmit_limit` 后停止重挂并记录日志。

3. 回归检查：
   - 不在网关中做自动重挂；
   - 不在柜台中做超时扫描；
   - `on_timer_for_resubmit` 仅作为策略层节流执行器。

---

## 五、交付结果

1. 架构职责清晰：
   - 网关：超时撤单；
   - 策略：重挂决策与执行。
2. 代码逻辑简洁：
   - 单一职责，无重复重试队列。
3. 文档性增强：
   - 新增函数补齐注释，便于维护与扩展。
