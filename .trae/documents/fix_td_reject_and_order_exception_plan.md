# 修复拒单后持仓/资金异常与订单异常处理机制计划

## 一、问题定位与目标

### 1) 拒单后持仓市值与账户余额异常
- 现象：拒单后 `position.volume*position.price` 与 `account.balance` 变化不符合预期
- 推测：在拒单流程中误调用了 `update_position` / `update_account`，或冻结资金未正确回滚

### 2) 订单异常分类与自动撤单重挂
- 需要梳理 vnpy 订单状态机，明确：
  - 未成交（NOTTRADED）
  - 部分成交（PARTTRADED） 
  - 长时间未成交（需自定义超时）
- 给出“自动撤单→重新挂单”的推荐实现位置与示例

---

## 二、实施步骤

### 步骤 1：修复拒单时持仓/资金误更新
1. 在 `match_order` 中，拒单分支（reject_rate、资金不足）**禁止**调用 `update_position/trade/account`
2. 确保拒单只回滚冻结资金并推送订单事件，不生成 TradeData
3. 验证：拒单后持仓总量、现金余额、冻结金额均应保持拒单前数值

### 步骤 2：统一冻结与回滚逻辑
1. 将“资金检查+冻结”提前到 `send_order` 末尾，确保冻结失败立即拒单
2. 拒单/撤单/部分成交时，统一调用 `release_order_frozen_cash` 回滚对应资金
3. 成交时按实际成交金额+费用释放冻结，并更新现金

### 步骤 3：订单异常分类与超时检测
1. 在 `SimulationCounter` 新增：
   - `order_submit_time: Dict[str, datetime]` 记录委托时间
   - `order_timeout: int = 30` 秒（可配置）
2. 在 `on_timer`（或模拟撮合循环）中扫描：
   - `status==SUBMITTING and now-submit>timeout` → 标记为 `CANCELLED` 并回滚冻结
   - `status==NOTTRADED and now-submit>timeout` → 同样自动撤单
3. 提供钩子 `should_auto_resubmit(order)` 允许策略层决定是否重挂

### 步骤 4：自动撤单重挂示例
1. 在策略层（或模拟网关）实现：
   ```python
   def on_order(self, order: OrderData):
       if order.status == Status.CANCELLED and self.should_resubmit(order):
           new_req = OrderRequest(
               symbol=order.symbol,
               exchange=order.exchange,
               direction=order.direction,
               offset=order.offset,
               price=self.adjust_price(order.price),  # 可调整价格
               volume=order.volume-order.traded,
               type=order.type
           )
           self.send_order(new_req)
   ```
2. 说明：真实交易需检查“当日有效/撤销”标志，模拟层可直接重挂

### 步骤 5：回归验证
1. 语法检查：`python -m py_compile vnpy_qmt_sim/td.py`
2. 单元场景：
   - 拒单（资金不足）→ 持仓/现金/冻结不变
   - 超时未成交 → 自动撤单，冻结释放
   - 部分成交后剩余超时 → 仅撤剩余，已成交部分保留
3. 日志打印关键路径，确保可追溯

---

## 三、交付清单

- 代码文件：
  - `vnpy_qmt_sim/td.py`（冻结、回滚、超时、拒单修正）
- 配置与钩子：
  - 新增 `order_timeout` 参数（可外部配置）
  - 新增 `should_auto_resubmit` 示例回调
- 文档：
  - 本计划（已勾选完成项）
  - 关键日志示例与测试命令

---

## 四、注意事项

- 保持与真实交易行为一致：撤单只减冻结、不减已成交；重挂需重新冻结
- 所有资金变动必须伴随 `on_account` 事件推送，确保 UI 同步
- 中文日志使用 `_()` 国际化，但模拟层可暂时用中文便于调试
- 不破坏现有接口：冻结字段仅内部使用，不暴露给策略层