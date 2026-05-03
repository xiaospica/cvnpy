from typing import Dict, List, Any, Optional, TYPE_CHECKING
from datetime import date, datetime, timedelta
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    OrderRequest,
    CancelRequest,
    OrderData,
    TradeData,
    PositionData,
    AccountData,
    LogData
)
from vnpy.trader.constant import (
    Direction,
    Status,
    Offset
)

if TYPE_CHECKING:
    from .persistence import QmtSimPersistence

class SimulationCounter:
    """模拟柜台"""

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.orders: Dict[str, OrderData] = {}
        self.trades: Dict[str, TradeData] = {}
        self.positions: Dict[str, PositionData] = {}
        self.accounts: Dict[str, AccountData] = {}
        
        self.order_count = 0
        self.trade_count = 0

        # 当日新买入跟踪 (vt_symbol → {volume, cost_basis})
        # cost_basis = 累计买入金额 (含手续费; 用于 settle 区分新买入/老持仓 mark)
        # settle_end_of_day 后清空。
        self._today_buy: Dict[str, Dict[str, float]] = {}

        self.accountid = "test_id"
        # 资金配置
        self.capital = 10_000_000.0
        self.frozen = 0.0
        self.order_frozen_cash: Dict[str, float] = {}
        self.order_reject_reason: Dict[str, str] = {}

        self.commission_rate = 0.0001
        self.min_commission = 5.0
        self.transfer_fee_rate = 0.00001
        self.stamp_duty_rate = 0.0005

        # self.commission_rate = 0.0
        # self.min_commission = 0.0
        # self.transfer_fee_rate = 0.0
        # self.stamp_duty_rate = 0.0
        
        # 异常配置
        self.reject_rate = 0.0  # 拒单率
        self.partial_rate = 0.0 # 部分成交率
        self.latency = 0 # 模拟延迟(ms)

        # 回放支持：策略层在 _replay_loop_iter 每天循环开头设此为逻辑日 datetime,
        # 循环结尾设 None。trade.datetime / order.datetime 用此值代替 datetime.now()，
        # 让前端"按日期"展示交易记录与回放真实时序对齐。
        self._replay_now: Optional[datetime] = None
        
        # 超时配置
        self.order_timeout = 30  # 订单超时秒数
        self.order_submit_time: Dict[str, datetime] = {}

        self.fill_delay_ms: int = 0
        self.reporting_delay_ms: int = 0
        self.reject_short_if_no_position: bool = True
        self.order_tasks: Dict[str, Dict[str, Any]] = {}

        # T+1 卖单的持仓冻结追踪：orderid -> (pos_key, frozen_amount)。
        # 成交/撤单/拒单时按剩余未成交量释放冻结。
        self.order_position_freeze: Dict[str, tuple[str, float]] = {}

        # 上次日终结算的日期，用于 gateway timer 检测自然日切换。
        self.last_settle_date: Optional[date] = None

        # SQLite 持久化层（可选）。由 gateway.connect 在启用时注入。
        self._persistence: Optional["QmtSimPersistence"] = None

    def attach_persistence(self, persistence: "QmtSimPersistence") -> None:
        self._persistence = persistence

    def _persist_account(self, account: AccountData) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.upsert_account(account)
        except Exception as exc:
            self.gateway.write_log(f"账户持久化失败: {exc}")

    def _persist_position(self, pos: PositionData) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.upsert_position(pos)
        except Exception as exc:
            self.gateway.write_log(f"持仓持久化失败: {exc}")

    def _persist_order(self, order: OrderData) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.upsert_order(order)
        except Exception as exc:
            self.gateway.write_log(f"订单持久化失败: {exc}")

    def _persist_trade(self, trade: TradeData) -> None:
        if self._persistence is None:
            return
        # 从对应 order 反查 reference 一并持久化，便于按策略名审计成交流水
        # （vnpy_ml_strategy 在 reference 里填 "{strategy_name}:{seq}"）
        reference = ""
        order = self.orders.get(trade.orderid)
        if order is not None:
            reference = getattr(order, "reference", "") or ""
        try:
            self._persistence.insert_trade(trade, reference=reference)
        except Exception as exc:
            self.gateway.write_log(f"成交持久化失败: {exc}")

    # 推送 + 持久化合并 helper。状态变更后调用，确保前端事件与 DB 状态一致。
    def _emit_order(self, order: OrderData) -> None:
        self.gateway.on_order(order)
        self._persist_order(order)

    def _emit_trade(self, trade: TradeData) -> None:
        self.gateway.on_trade(trade)
        self._persist_trade(trade)

    def _emit_position(self, pos: PositionData) -> None:
        self.gateway.on_position(pos)
        self._persist_position(pos)

    def _emit_account(self, account: AccountData) -> None:
        self.gateway.on_account(account)
        self._persist_account(account)

    def process_simulation(self, now: datetime) -> None:
        for orderid, task in list(self.order_tasks.items()):
            order = self.orders.get(orderid)
            if not order:
                self.order_tasks.pop(orderid, None)
                continue

            if order.status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
                self.order_tasks.pop(orderid, None)
                continue

            phase = str(task.get("phase") or "")
            if phase == "unreported":
                report_at = task.get("report_at")
                if isinstance(report_at, datetime) and now < report_at:
                    continue
                order.status = Status.NOTTRADED
                self._set_order_status_msg(order, str(task.get("status_msg") or ""))
                self._set_order_extra(order, {"qmt_status": "ORDER_REPORTED", "case_tag": task.get("case_tag")})
                self._emit_order(order)
                task["phase"] = "reported"
                continue

            if phase != "reported":
                continue

            case_tag = str(task.get("case_tag") or "")
            if case_tag.startswith("force_reject"):
                self._reject_order(order, str(task.get("status_msg") or "模拟强制拒单"))
                self.order_tasks.pop(orderid, None)
                continue

            if case_tag.startswith("no_fill"):
                continue

            if case_tag.startswith("partial_then_stall"):
                if not task.get("did_partial"):
                    partial_at = task.get("partial_at")
                    if isinstance(partial_at, datetime) and now < partial_at:
                        continue
                    ratio = float(task.get("partial_ratio") or 0.5)
                    remain = int(order.volume - order.traded)
                    if remain <= 0:
                        self.order_tasks.pop(orderid, None)
                        continue
                    trade_volume = max(int(remain * ratio), 1)
                    trade_volume = min(trade_volume, remain)
                    self._execute_trade(order, float(trade_volume))
                    task["did_partial"] = True
                continue

            fill_at = task.get("fill_at")
            if isinstance(fill_at, datetime) and now < fill_at:
                continue

            self.match_order(order)
            if order.status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
                self.order_tasks.pop(orderid, None)

    def _parse_case_tag(self, reference: str) -> str:
        if not reference:
            return ""
        marker = "|case="
        idx = reference.find(marker)
        if idx < 0:
            return ""
        tail = reference[idx + len(marker):]
        tag = tail.split("|", 1)[0].strip()
        return tag

    def _set_order_extra(self, order: OrderData, extra: Dict[str, Any]) -> None:
        try:
            old_extra = getattr(order, "extra", None)
            if isinstance(old_extra, dict):
                merged = {**old_extra, **extra}
                setattr(order, "extra", merged)
            else:
                setattr(order, "extra", dict(extra))
        except Exception:
            return

    def _set_order_status_msg(self, order: OrderData, msg: str) -> None:
        try:
            if msg:
                order.status_msg = msg
        except Exception:
            return

    def _resolve_trade_price(self, order: OrderData) -> float:
        """决定 trade.price：限价单按 order.price；市价单从 md.tick.last_price 取（即当日参考价）。

        前置条件：策略层在每个回放日开盘前调过 ``md.refresh_tick(vt, as_of_date=day)``
        把 tick.last_price 刷成当日 open（reference_kind=today_open 时）。返 0
        交给上层判断（理论上 refresh 后总有价；返 0 等于"撮合阻塞"，比之前硬编码 10.0
        污染权益曲线安全）。
        """
        if order.price and order.price > 0:
            return float(order.price)
        try:
            md = getattr(self.gateway, "md", None)
            if md is None:
                return 0.0
            tick = md.get_full_tick(order.vt_symbol)
            if tick and getattr(tick, "last_price", 0) and tick.last_price > 0:
                return float(tick.last_price)
        except Exception:
            return 0.0
        return 0.0

    def _reject_order(self, order: OrderData, status_msg: str) -> None:
        if order.direction == Direction.LONG:
            self.release_order_frozen_cash(order.orderid, push_event=False)
        order.status = Status.REJECTED
        self._set_order_status_msg(order, status_msg)
        self._set_order_extra(order, {"status_msg": status_msg, "qmt_status": "ORDER_JUNK"})
        self.order_submit_time.pop(order.orderid, None)
        self.order_reject_reason[order.orderid] = "case_reject"
        self._emit_order(order)
        self.push_account()
        try:
            self.gateway.write_log(f"模拟拒单：{order.vt_orderid} {status_msg}")
        except Exception:
            return

    def _execute_trade(self, order: OrderData, volume: float) -> None:
        remain = float(order.volume - order.traded)
        if volume <= 0 or remain <= 0:
            return
        if volume > remain:
            volume = remain

        trade_price = self._resolve_trade_price(order)
        if trade_price <= 0:
            self.gateway.write_log(
                f"_execute_trade: 无法解析成交价 {order.vt_orderid}，撮合阻塞"
            )
            return
        self.trade_count += 1
        trade = TradeData(
            symbol=order.symbol,
            exchange=order.exchange,
            orderid=order.orderid,
            tradeid=str(self.trade_count),
            direction=order.direction,
            offset=order.offset,
            price=trade_price,
            volume=volume,
            datetime=self._replay_now or datetime.now(),
            gateway_name=self.gateway.gateway_name,
        )
        self.trades[trade.tradeid] = trade

        order.traded += volume
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
            self.order_submit_time.pop(order.orderid, None)
        else:
            order.status = Status.PARTTRADED

        self._emit_order(order)
        self._emit_trade(trade)
        self.update_position(trade)
        self.update_account(trade)
        try:
            extra = getattr(order, "extra", None)
            case_tag = ""
            if isinstance(extra, dict):
                case_tag = str(extra.get("case_tag") or "")
            if case_tag:
                self.gateway.write_log(f"模拟成交触发: {order.vt_orderid} case={case_tag} traded={order.traded}/{order.volume} status={order.status}")
        except Exception:
            return

    def send_order(self, req: OrderRequest) -> str:
        self.order_count += 1
        orderid = str(self.order_count)
        case_tag = self._parse_case_tag(str(getattr(req, "reference", "") or ""))
        
        order = OrderData(
            symbol=req.symbol,
            exchange=req.exchange,
            orderid=orderid,
            type=req.type,
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=req.volume,
            traded=0,
            status=Status.SUBMITTING,
            datetime=datetime.now(),
            gateway_name=self.gateway.gateway_name,
            reference=getattr(req, "reference", "") or "",
        )
        self.orders[orderid] = order
        self.order_submit_time[orderid] = order.datetime
        self._set_order_extra(order, {"qmt_status": "ORDER_UNREPORTED", "case_tag": case_tag})

        vol_int = 0
        try:
            vol_int = int(float(order.volume))
        except Exception:
            vol_int = 0
        # A 股规则：买单需为 100 股整数倍；卖单允许零股一次性卖出（不强制 100 倍数）
        if vol_int <= 0 or (order.direction == Direction.LONG and vol_int % 100 != 0):
            order.status = Status.REJECTED
            msg = f"委托数量不合法: volume={order.volume}"
            self._set_order_status_msg(order, msg)
            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK"})
            self.order_reject_reason[orderid] = "invalid_volume"
            self.order_submit_time.pop(orderid, None)
            self._emit_order(order)
            self.gateway.write_log(f"拒单：{msg}")
            return order.vt_orderid

        if order.direction == Direction.SHORT and case_tag == "force_sell_no_position":
            order.status = Status.REJECTED
            self._set_order_status_msg(order, "持仓不足(用例强制)")
            self._set_order_extra(order, {"status_msg": "持仓不足(用例强制)", "qmt_status": "ORDER_JUNK"})
            self.order_reject_reason[orderid] = "force_sell_no_position"
            self.order_submit_time.pop(orderid, None)
            self._emit_order(order)
            self.gateway.write_log("拒单：持仓不足(用例强制)")
            return order.vt_orderid

        if float(order.price) > 0:
            try:
                md = getattr(self.gateway, "md", None)
                get_tick = getattr(md, "get_full_tick", None)
                if callable(get_tick):
                    tick = get_tick(order.vt_symbol)
                    if tick:
                        limit_up = float(getattr(tick, "limit_up", 0) or 0)
                        limit_down = float(getattr(tick, "limit_down", 0) or 0)
                        if limit_up > 0 and float(order.price) > limit_up:
                            order.status = Status.REJECTED
                            msg = f"价格超出涨停: price={order.price} limit_up={limit_up}"
                            self._set_order_status_msg(order, msg)
                            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK", "limit_up": limit_up, "limit_down": limit_down})
                            self.order_reject_reason[orderid] = "price_limit_up"
                            self.order_submit_time.pop(orderid, None)
                            self._emit_order(order)
                            self.gateway.write_log(msg)
                            return order.vt_orderid
                        if limit_down > 0 and float(order.price) < limit_down:
                            order.status = Status.REJECTED
                            msg = f"价格超出跌停: price={order.price} limit_down={limit_down}"
                            self._set_order_status_msg(order, msg)
                            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK", "limit_up": limit_up, "limit_down": limit_down})
                            self.order_reject_reason[orderid] = "price_limit_down"
                            self.order_submit_time.pop(orderid, None)
                            self._emit_order(order)
                            self.gateway.write_log(msg)
                            return order.vt_orderid
            except Exception:
                pass

        if order.direction == Direction.SHORT and self.reject_short_if_no_position:
            pos_key = f"{order.symbol}.{order.exchange.value}.{Direction.LONG.value}"
            pos = self.positions.get(pos_key)
            # A 股 T+1：可卖持仓 = 昨仓 - 已冻结。今仓不可卖。
            yd_volume = float(pos.yd_volume) if pos else 0.0
            frozen_volume = float(pos.frozen) if pos else 0.0
            available_yd = max(yd_volume - frozen_volume, 0.0)
            if float(order.volume) > available_yd:
                order.status = Status.REJECTED
                msg = f"可用持仓不足(T+1): yd={yd_volume} frozen={frozen_volume} volume={order.volume}"
                self._set_order_status_msg(order, msg)
                self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK"})
                self.order_reject_reason[orderid] = "insufficient_position"
                self.order_submit_time.pop(orderid, None)
                self._emit_order(order)
                self.gateway.write_log(f"拒单：{msg}")
                return order.vt_orderid
            pos.frozen = frozen_volume + float(order.volume)
            self.order_position_freeze[orderid] = (pos_key, float(order.volume))

        if order.direction == Direction.LONG:
            estimate_price = self._resolve_trade_price(order)
            if estimate_price <= 0:
                # 没有 tick 信息无法估计冻结资金，按 0 处理直接拒单
                msg = f"无法估计成交价：{order.vt_symbol}（tick 缺失）"
                order.status = Status.REJECTED
                self._set_order_status_msg(order, msg)
                self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK"})
                self.order_reject_reason[orderid] = "no_tick_price"
                self.order_submit_time.pop(orderid, None)
                self._emit_order(order)
                self.gateway.write_log(f"拒单：{msg}")
                return order.vt_orderid
            estimate_amount = estimate_price * order.volume
            estimate_fee = self.calculate_fee(
                trade_amount=estimate_amount,
                direction=order.direction
            )
            need_frozen = estimate_amount + estimate_fee
            available_cash = self.capital - self.frozen
            if need_frozen > available_cash:
                order.status = Status.REJECTED
                order.status_msg = "260200:可用资金不足"
                self._set_order_extra(order, {"status_msg": "260200:可用资金不足", "qmt_status": "ORDER_JUNK"})
                self.order_reject_reason[orderid] = "insufficient_funds"
                self.order_submit_time.pop(orderid, None)
                self._emit_order(order)
                self.gateway.write_log(
                    f"拒单：可用资金不足，可用={available_cash:.2f}，需冻结={need_frozen:.2f}"
                )
                return order.vt_orderid

            self.frozen += need_frozen
            self.order_frozen_cash[orderid] = need_frozen
            self.push_account()

        self._emit_order(order)
        
        if order.status != Status.REJECTED:
            needs_scheduling = bool(case_tag) or self.fill_delay_ms > 0 or self.reporting_delay_ms > 0
            if needs_scheduling:
                base_dt = order.datetime
                report_at = base_dt + timedelta(milliseconds=int(self.reporting_delay_ms))
                timeout_override = None

                fill_at = None
                partial_at = None
                partial_ratio = 0.5
                status_msg = ""

                if case_tag.startswith("no_fill"):
                    if "_" in case_tag:
                        tail = case_tag.split("_")[-1].rstrip("s")
                        if tail.isdigit():
                            timeout_override = int(tail)
                    fill_at = None
                elif case_tag.startswith("delayed_fill_"):
                    secs_str = case_tag.replace("delayed_fill_", "").rstrip("s")
                    secs = int(secs_str) if secs_str.isdigit() else 5
                    fill_at = report_at + timedelta(seconds=secs)
                elif case_tag.startswith("partial_then_stall"):
                    secs = 1
                    if "_" in case_tag:
                        tail = case_tag.split("_")[-1].rstrip("s")
                        if tail.isdigit():
                            secs = int(tail)
                    partial_at = report_at + timedelta(seconds=secs)
                elif case_tag.startswith("force_reject"):
                    status_msg = "模拟强制拒单"
                else:
                    if self.fill_delay_ms > 0:
                        fill_at = report_at + timedelta(milliseconds=int(self.fill_delay_ms))

                if timeout_override and timeout_override > 0:
                    self._set_order_extra(order, {"timeout_seconds": timeout_override})

                self.order_tasks[orderid] = {
                    "case_tag": case_tag,
                    "phase": "unreported",
                    "report_at": report_at,
                    "fill_at": fill_at,
                    "partial_at": partial_at,
                    "partial_ratio": partial_ratio,
                    "status_msg": status_msg,
                }
            else:
                self.match_order(order)
        
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest):
        order = self.orders.get(req.orderid)
        if not order:
            return
            
        if order.status in [Status.ALLTRADED, Status.CANCELLED, Status.REJECTED]:
            return

        self.release_order_frozen_cash(order.orderid)
        self.release_order_position_freeze(order.orderid)
        order.status = Status.CANCELLED
        self.order_submit_time.pop(order.orderid, None)
        self._emit_order(order)

    def match_order(self, order: OrderData):
        """模拟撮合逻辑"""
        # 简单的立即成交逻辑
        if order.status == Status.SUBMITTING:
            order.status = Status.NOTTRADED
            self._emit_order(order)

        # 拒单模拟
        if self.reject_rate > 0:
            import random
            if random.random() < self.reject_rate:
                self.release_order_frozen_cash(order.orderid)
                order.status = Status.REJECTED
                order.status_msg = "模拟随机拒单"
                self.order_reject_reason[order.orderid] = "random_reject"
                self.order_submit_time.pop(order.orderid, None)
                self._emit_order(order)
                # 拒单后不生成成交，不更新持仓/账户
                return

        # 全额成交
        trade_volume = order.volume - order.traded
        if trade_volume <= 0:
            return

        # 部分成交模拟
        if self.partial_rate > 0:
            import random
            if random.random() < self.partial_rate:
                trade_volume = trade_volume // 2
                if trade_volume == 0:
                    trade_volume = 1

        trade_price = self._resolve_trade_price(order)
        if trade_price <= 0:
            self.gateway.write_log(
                f"match_order: 无法解析成交价 {order.vt_orderid}，撮合阻塞"
            )
            return
        self.trade_count += 1
        trade = TradeData(
            symbol=order.symbol,
            exchange=order.exchange,
            orderid=order.orderid,
            tradeid=str(self.trade_count),
            direction=order.direction,
            offset=order.offset,
            price=trade_price,
            volume=trade_volume,
            datetime=self._replay_now or order.datetime,
            gateway_name=self.gateway.gateway_name
        )
        self.trades[trade.tradeid] = trade
        
        order.traded += trade_volume
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
            self.order_submit_time.pop(order.orderid, None)
        else:
            order.status = Status.PARTTRADED
            
        self._emit_order(order)
        self._emit_trade(trade)

        try:
            extra = getattr(order, "extra", None)
            case_tag = ""
            if isinstance(extra, dict):
                case_tag = str(extra.get("case_tag") or "")
            if case_tag:
                self.gateway.write_log(f"模拟成交触发: {order.vt_orderid} case={case_tag} traded={order.traded}/{order.volume} status={order.status}")
        except Exception:
            pass

        self.update_position(trade)
        self.update_account(trade)

    def calculate_fee(self, trade_amount: float, direction: Direction) -> float:
        commission = max(trade_amount * self.commission_rate, self.min_commission)
        transfer_fee = trade_amount * self.transfer_fee_rate
        stamp_duty = trade_amount * self.stamp_duty_rate if direction == Direction.SHORT else 0.0
        return commission + transfer_fee + stamp_duty

    def settle_end_of_day(self, settle_date: date) -> None:
        """日终结算：T+1 持仓结转 + mark-to-market.

        关键 — 区分今日新买入 vs 老持仓的 mark 口径，与 qlib backtest deal_price=close 对齐：

        老持仓 (T-1 EOD 已 settle, pos.price = T-1 close):
            mark close: pos.price *= (1 + pct_chg/100) = close_T / pre_close_T = close / pre_close
            （即 pct_chg 含 pre_close → close 全段，符合"昨收→今收"语义）

        今日新买入 (买入价 = today_open):
            mark close: 只 mark today_open → close 这一段，不能用 pct_chg (它含隔夜 pre_close→open
            那一段，今日新买入根本没经历过)。即:
              new_value = today_buy_cost × close / open
            混合持仓 (老 yd + 今日新买入):
              new_pos_value = old_value + new_value
                            = old_volume × old_price × (1+pct/100) + today_buy_cost × close/open

        重复调用同一日期幂等 (last_settle_date 守门)。

        修复前 bug: 全部按 pct_chg 累乘 → 当日新买入的 mark 多算了"open 之前隔夜跳空"
        那一段，导致与 qlib backtest weight 偏差最高 13%（详见 vnpy commit 525864e）。
        """
        if self.last_settle_date is not None and settle_date <= self.last_settle_date:
            return

        md = getattr(self.gateway, "md", None)
        get_quote = getattr(md, "get_quote", None) if md else None

        for pos_key, pos in self.positions.items():
            if pos.volume <= 0:
                pos.yd_volume = pos.volume
                continue
            if callable(get_quote):
                quote = get_quote(pos.vt_symbol)
                if quote is not None and pos.price > 0:
                    today_buy = self._today_buy.get(pos_key, {"volume": 0.0, "cost": 0.0})
                    today_vol = float(today_buy.get("volume", 0))
                    today_cost = float(today_buy.get("cost", 0))
                    yd_vol = float(pos.yd_volume or 0)
                    pct = float(quote.pct_chg) / 100.0
                    # BarQuote 字段名是 open_price / close_price (不是 open / close).
                    # 早期 bug: 用 getattr(quote, "open", 0) 拿不到值 → open_p=close_p=0
                    # → "今日新买入" / "混合" 分支判定失败, 误走 pct_chg 累乘 →
                    # 节后第一日新买的股按 pct_chg(含隔夜跳空)mark, weight 高估 ~10%.
                    open_p = float(getattr(quote, "open_price", 0) or 0)
                    close_p = float(getattr(quote, "close_price", 0) or 0)

                    # 老持仓 mark: pre_close → close
                    # 老持仓 EOD 总市值 = yd_vol × old_price × (1+pct)
                    # 这里 old_price 是 yd_vol 部分的 cost。但 pos.price 在 update_position
                    # 时被覆盖为 trade.price (今日 open)。所以"老持仓"的成本无法直接拿到 —
                    # 但**T-1 settle 后 pos.price 已是 T-1 close**, 今日没买就是 yd_vol 全部，
                    # pos.price 还是 T-1 close。今日有买入则 pos.price 被覆盖为 today_open。
                    #
                    # 解决：用"老持仓总成本 = (pos.volume × pos.price - today_cost)" 反推
                    # (因为 update_position 覆盖 pos.price = trade.price 后, 老 yd 的成本
                    # 被丢失。但若今日没买入, today_cost=0 today_vol=0, 整体退化到原行为)
                    if today_vol > 0 and yd_vol > 0 and open_p > 0 and close_p > 0:
                        # 混合持仓: 老 yd 用 pct_chg, 今日新买入用 close/open
                        # 老成本 ≈ pos.price × yd_vol (近似: pos.price 是今日 trade.price
                        # = today_open; T-1 EOD 老成本 = T-1 close × yd_vol; 由于 pos.price
                        # 被覆盖, 用 today_open 代替 T-1 close 会有误差; 实际等同把老持仓也
                        # 当做今日新买的 — 这是当前模型限制)
                        # 简化处理: 老+新分别 mark 后求加权 pos.price
                        old_value = (yd_vol * pos.price) * (1.0 + pct)
                        new_value = today_cost * close_p / open_p
                        pos.price = (old_value + new_value) / pos.volume
                    elif today_vol > 0 and open_p > 0 and close_p > 0:
                        # 全部今日新买入: cost *= close/open
                        pos.price = pos.price * close_p / open_p
                    else:
                        # 全部老持仓: pct_chg 累乘
                        pos.price = pos.price * (1.0 + pct)
            pos.yd_volume = pos.volume
            self._emit_position(pos)

        # 清空今日新买入跟踪 (settle 后所有今日买入转为老 yd)
        self._today_buy.clear()

        self.last_settle_date = settle_date
        try:
            self.gateway.write_log(f"日终结算完成: {settle_date}")
        except Exception:
            pass

    def release_order_position_freeze(self, orderid: str, traded_amount: float = 0.0) -> None:
        """释放卖单对持仓的冻结。traded_amount>0 表示成交回填（部分扣减），否则释放剩余。"""
        entry = self.order_position_freeze.get(orderid)
        if not entry:
            return
        pos_key, frozen_amount = entry
        pos = self.positions.get(pos_key)
        if not pos:
            self.order_position_freeze.pop(orderid, None)
            return
        if traded_amount > 0:
            unfreeze = min(traded_amount, frozen_amount)
        else:
            unfreeze = frozen_amount
        pos.frozen = max(float(pos.frozen) - unfreeze, 0.0)
        remain = frozen_amount - unfreeze
        if remain > 0:
            self.order_position_freeze[orderid] = (pos_key, remain)
        else:
            self.order_position_freeze.pop(orderid, None)

    def release_order_frozen_cash(
        self,
        orderid: str,
        release_amount: float = 0.0,
        push_event: bool = True
    ) -> None:
        frozen_cash = self.order_frozen_cash.get(orderid, 0.0)
        if frozen_cash <= 0:
            return

        amount = release_amount if release_amount > 0 else frozen_cash
        amount = min(amount, frozen_cash)
        self.frozen -= amount
        if self.frozen < 0:
            self.frozen = 0.0

        remain = frozen_cash - amount
        if remain > 0:
            self.order_frozen_cash[orderid] = remain
        else:
            self.order_frozen_cash.pop(orderid, None)
            self.order_submit_time.pop(orderid, None)
            self.order_reject_reason.pop(orderid, None)

        if push_event:
            self.push_account()

    def push_account(self) -> None:
        account = AccountData(
            accountid=self.accountid,
            balance=self.capital,
            frozen=self.frozen,
            gateway_name=self.gateway.gateway_name
        )
        self.accounts[account.accountid] = account
        self._emit_account(account)

    def update_position(self, trade: TradeData):
        vt_symbol = f"{trade.symbol}.{trade.exchange.value}"

        # A股通常只看多头持仓
        pos_long_id = f"{vt_symbol}.{Direction.LONG.value}"
        pos = self.positions.get(pos_long_id)

        if not pos:
            pos = PositionData(
                symbol=trade.symbol,
                exchange=trade.exchange,
                direction=Direction.LONG,
                volume=0,
                gateway_name=self.gateway.gateway_name
            )
            self.positions[pos_long_id] = pos
            print(f'创建新持仓{pos.vt_positionid}')
        if trade.direction == Direction.LONG:
            # 买入：增加总持仓，但 yd_volume（可卖昨仓）不变 → T+1 当日不可卖。
            pos.volume += trade.volume
            # 跟踪当日新买入 (用于 settle 区分新买入 vs 老持仓 mark)
            tb = self._today_buy.setdefault(pos_long_id, {"volume": 0.0, "cost": 0.0})
            tb["volume"] += float(trade.volume)
            tb["cost"] += float(trade.price) * float(trade.volume)
        else:
            # 卖出：扣减总持仓 + 昨仓 + 持仓冻结。
            pos.volume -= trade.volume
            pos.yd_volume = max(float(pos.yd_volume) - float(trade.volume), 0.0)
            self.release_order_position_freeze(trade.orderid, traded_amount=float(trade.volume))

        if pos.volume < 0:
            pos.volume = 0
        if pos.yd_volume > pos.volume:
            pos.yd_volume = pos.volume  # 防御：极端撮合中保证不超持

        # pos.price 保持"最近成交价"语义（开发者原 TODO 标注），由 settle_end_of_day
        # 在每日盘后按 pct_chg 累乘做 mark-to-market。
        pos.price = trade.price

        self._emit_position(pos)

    def update_account(self, trade: TradeData):
        trade_amount = trade.price * trade.volume
        trade_fee = self.calculate_fee(trade_amount, trade.direction)

        if trade.direction == Direction.LONG:
            self.capital -= (trade_amount + trade_fee)

            order = self.orders.get(trade.orderid)
            release_price = trade.price
            if order:
                release_price = self._resolve_trade_price(order)
                if release_price <= 0:
                    # 兜底回 trade.price（撮合那一刻已用过的价）
                    release_price = trade.price

            release_amount = release_price * trade.volume + self.calculate_fee(
                trade_amount=release_price * trade.volume,
                direction=Direction.LONG
            )
            self.release_order_frozen_cash(trade.orderid, release_amount, push_event=False)
        else:
            self.capital += (trade_amount - trade_fee)

        if self.capital < 0:
            self.capital = 0.0

        self.push_account()


class QmtSimTd:
    """
    QMT模拟交易接口
    """

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.counter = SimulationCounter(gateway)

    def connect(self, setting: dict):
        acc_id = setting.get("账户", "test_id")
        # 关键顺序：先把 capital / partial_rate 等从 setting 装入 counter，再构造
        # AccountData 推到 OMS。否则 AccountData.balance = counter.capital(默认 10M) 错过
        # setting 的"模拟资金" → vnpy OMS 一直读到 stale 10M（_get_current_cash 误读）。
        self.counter.capital = setting.get("模拟资金", 10000000.0)
        self.counter.partial_rate = setting.get("部分成交率", 0.0)
        self.counter.reject_rate = setting.get("拒单率", 0.0)

        account = AccountData(
            accountid=acc_id,
            balance=self.counter.capital,
            frozen=self.counter.frozen,
            gateway_name=self.gateway_name
        )
        self.counter.accountid = account.accountid
        self.counter.accounts[acc_id] = account

        self.gateway.write_log("模拟交易接口连接成功")
        self.gateway.on_account(account)

    def send_order(self, req: OrderRequest) -> str:
        return self.counter.send_order(req)

    def cancel_order(self, req: CancelRequest):
        self.counter.cancel_order(req)

    def query_account(self):
        """查询账户"""
        for account in self.counter.accounts.values():
            self.gateway.on_account(account)

    def query_position(self):
        """查询持仓"""
        for position in self.counter.positions.values():
            self.gateway.on_position(position)
            
    def query_orders(self):
        """查询委托"""
        for order in self.counter.orders.values():
            self.gateway.on_order(order)
            
    def query_trades(self):
        """查询成交"""
        for trade in self.counter.trades.values():
            self.gateway.on_trade(trade)
