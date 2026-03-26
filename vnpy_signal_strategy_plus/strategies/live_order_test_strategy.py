from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy_signal_strategy_plus.utils import convert_code_to_vnpy_type

from vnpy_signal_strategy_plus.base import CHINA_TZ, EngineType
from vnpy_signal_strategy_plus.mysql_signal_strategy import MySQLSignalStrategyPlus, Stock


class LiveOrderTestStrategyPlus(MySQLSignalStrategyPlus):
    strategy_name = "live_order_test"
    is_live_test_strategy: bool = True
    support_clear_position: bool = True

    resubmit_limit: int = 2
    resubmit_interval: int = 2

    test_symbol: str = "510300.SH"
    test_pct: float = 0.01

    def __init__(self, signal_engine: Any):
        super().__init__(signal_engine)
        self._test_run_id: str | None = None
        self._test_suite: str | None = None
        self._current_test_signal_type: str = ""
        self.live_test_order_tag: str = ""
        self._clear_active: bool = False
        self._clear_orders: dict[str, dict[str, Any]] = {}
        self._clear_target: dict[str, float] = {}
        self._clear_traded: dict[str, float] = {}
        self._clear_done_symbols: set[str] = set()

    def get_order_reference(self) -> str:
        """
        重写 reference 标识生成逻辑，根据上下文动态注入 case tag。
        """
        base_ref = super().get_order_reference()

        # 如果是由 AutoResubmitMixinPlus 触发的重挂发单，不携带 case tag，
        # 以免模拟网关对重挂单再次执行异常撮合逻辑（比如再次强制拒单）
        if getattr(self, "_is_resubmitting", False):
            return base_ref

        # 正常下单时，如果当前测试信号包含 tag，则注入
        tag = str(getattr(self, "live_test_order_tag", "") or "").strip()
        if tag:
            reference = f"{base_ref}|case={tag}"
            return reference[:128]

        return base_ref

    def on_order(self, order: Any) -> None:
        """重写on_order增加测试专用打印和清仓统计逻辑"""
        # 测试专用的详细订单日志打印
        status_msg = ""
        try:
            extra = getattr(order, "extra", None)
            if isinstance(extra, dict):
                status_msg = str(extra.get("status_msg") or "")
        except Exception:
            status_msg = ""
        if not status_msg:
            status_msg = str(getattr(order, "status_msg", "") or "")

        self.write_log(
            f"[ORDER] {order.vt_orderid} {order.vt_symbol} status={order.status.value} traded={order.traded}/{order.volume} "
            f"price={order.price} dir={order.direction.value} offset={order.offset.value} ref={getattr(order, 'reference', '')} msg={status_msg}"
        )

        if order.status in {Status.REJECTED} and status_msg and "260200" not in status_msg:
            self.write_log(f"[REJECT] {order.vt_orderid} {order.vt_symbol} msg={status_msg}")

        super().on_order(order)

        if not self._clear_active:
            return

        info = self._clear_orders.get(order.vt_orderid)
        if not info:
            return

        if order.status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
            info["done"] = True
            info["status"] = order.status.value

        if self._clear_orders and all(bool(v.get("done")) for v in self._clear_orders.values()):
            self._finalize_clear_positions()

    def on_trade(self, trade: Any) -> None:
        """重写on_trade增加测试专用打印和清仓统计逻辑"""
        self.write_log(
            f"[TRADE] {trade.vt_tradeid} order={trade.vt_orderid} {trade.vt_symbol} {trade.direction.value} "
            f"price={trade.price} volume={trade.volume}"
        )
        super().on_trade(trade)

        if not self._clear_active:
            return

        info = self._clear_orders.get(trade.vt_orderid)
        if not info:
            return

        if trade.direction != Direction.SHORT:
            return

        vt_symbol = str(getattr(trade, "vt_symbol", "") or "")
        if not vt_symbol:
            return

        traded = float(self._clear_traded.get(vt_symbol, 0.0)) + float(trade.volume or 0)
        self._clear_traded[vt_symbol] = traded
        target = float(self._clear_target.get(vt_symbol, 0.0))
        if target > 0 and traded >= target and vt_symbol not in self._clear_done_symbols:
            self._clear_done_symbols.add(vt_symbol)
            self.write_log(f"[LIVE_TEST] 清仓单标的完成: {vt_symbol} traded={traded} target={target}")

    def _finalize_clear_positions(self) -> None:
        symbols = set(self._clear_target.keys())
        remain_parts: list[str] = []
        for pos in self.signal_engine.main_engine.get_all_positions():
            if pos.vt_symbol not in symbols:
                continue
            expected_gateway = self.get_gateway_name(pos.vt_symbol)
            if expected_gateway and expected_gateway != pos.gateway_name:
                continue
            if pos.volume > 0:
                remain_parts.append(f"{pos.vt_symbol} volume={pos.volume} frozen={pos.frozen}")

        if not remain_parts:
            self.write_log(f"[LIVE_TEST] 清仓完成 symbols={len(symbols)}")
        else:
            detail = "; ".join(remain_parts[:20])
            self.write_log(f"[LIVE_TEST] 清仓未完成 symbols={len(symbols)} remain={detail}")

        self._clear_active = False

    def _extract_case_tag(self, signal_type: str) -> str:
        s = (signal_type or "").lower()
        tokens = s.replace("|", " ").replace(",", " ").split()
        for t in tokens:
            if t.startswith("no_fill"):
                return t
            if t.startswith("delayed_fill"):
                return t
            if t.startswith("partial_then_stall"):
                return t
            if t.startswith("force_reject"):
                return t
            if t.startswith("force_sell_no_position"):
                return t
            if t.startswith("reject_up"):
                return t
            if t.startswith("reject_down"):
                return t
        return ""

    def get_test_remark_base(self) -> datetime:
        base_dt = datetime.now(CHINA_TZ)

        if self.engine_type != EngineType.LIVE.value and self.current_dt:
            base_dt = self.current_dt

        if isinstance(base_dt, datetime) and base_dt.tzinfo:
            base_dt = base_dt.replace(tzinfo=None)

        if self.engine_type == EngineType.LIVE.value:
            return base_dt

        base_date = base_dt.date()
        return datetime.combine(base_date, datetime.min.time()) + timedelta(seconds=1)

    def clear_all_positions(self) -> None:
        """
        一键清仓
        """
        self.write_log("开始执行一键清仓")
        self._clear_active = False
        self._clear_orders = {}
        self._clear_target = {}
        self._clear_traded = {}
        self._clear_done_symbols = set()
        positions = self.signal_engine.main_engine.get_all_positions()
        count = 0
        for pos in positions:
            available = pos.volume - pos.frozen
            if available > 0 and pos.direction == Direction.LONG:
                expected_gateway = self.get_gateway_name(pos.vt_symbol)
                if not expected_gateway or expected_gateway != pos.gateway_name:
                    continue

                tick = self.get_active_tick(pos.vt_symbol)
                if tick and tick.limit_down:
                    price = float(tick.limit_down)
                else:
                    price = self.get_order_price(pos.vt_symbol, Direction.SHORT, fallback_price=0.0)
                
                order_type = OrderType.LIMIT if price > 0 else OrderType.MARKET
                
                vt_orderids = self.send_order(
                    vt_symbol=pos.vt_symbol,
                    direction=Direction.SHORT,
                    offset=Offset.CLOSE,
                    price=float(price),
                    volume=float(available),
                    order_type=order_type,
                )
                if vt_orderids:
                    self.write_log(f"[LIVE_TEST] 清仓下发卖单成功: {pos.vt_symbol} 数量: {available}")
                    count += 1
                    self._clear_target[pos.vt_symbol] = float(self._clear_target.get(pos.vt_symbol, 0.0)) + float(available)
                    for vt_orderid in vt_orderids:
                        self._clear_orders[str(vt_orderid)] = {"vt_symbol": pos.vt_symbol, "target": float(available), "done": False}
                else:
                    self.write_log(f"[LIVE_TEST] 清仓下发卖单失败: {pos.vt_symbol}")
            else:
                self.write_log(f'可用数量异常available: {pos.vt_symbol, available}')
        
        if count == 0:
            self.write_log("当前无可用持仓，无需清仓")
            return

        self._clear_active = True
        self.write_log(f"[LIVE_TEST] 清仓任务已创建 symbols={len(self._clear_target)} orders={len(self._clear_orders)}")

    def run_live_test_suite(self, suite: str) -> None:
        try:
            if not self.Session:
                self.write_log("数据库未连接，无法执行自动化测试")
                return

            suite = (suite or "all").lower()
            if suite in {"全部", "all"}:
                suite = "all"
            elif suite in {"冒烟", "smoke"}:
                suite = "smoke"
            elif suite in {"基础", "basic"}:
                suite = "basic"
            elif suite in {"全量", "full"}:
                suite = "full"
            else:
                suite = "all"

            run_id = datetime.now(CHINA_TZ).strftime("%Y%m%d_%H%M%S")
            self._test_run_id = run_id
            self._test_suite = suite

            signals = self._build_signals_for_suite(suite, run_id)
            if not signals:
                self.write_log(f"未生成测试信号: suite={suite}")
                return

            session = self.Session()
            try:
                objs: list[Stock] = []
                labels: list[str] = []
                remark_base = self.get_test_remark_base()
                remark_step = timedelta(milliseconds=20)
                if self.engine_type == EngineType.LIVE.value:
                    remark_step = timedelta(seconds=3)
                for i, s in enumerate(signals):
                    remark = remark_base + remark_step * i
                    db_type = str(s["type"])
                    if len(db_type) > 32:
                        db_type = db_type[:32]
                    obj = Stock(
                        code=s["code"],
                        pct=float(s["pct"]),
                        type=db_type,
                        price=float(s.get("price", 0) or 0),
                        stg=self.strategy_name,
                        remark=remark,
                        processed=False,
                    )
                    session.add(obj)
                    objs.append(obj)
                    labels.append(str(s.get("label") or ""))

                session.commit()

                self.write_log(f"[LIVE_TEST] 写入测试信号成功 run_id={run_id} suite={suite} count={len(objs)}")
                for obj, label in zip(objs, labels):
                    self.write_log(
                        f"[LIVE_TEST] signal_id={obj.id} code={obj.code} pct={obj.pct} type={obj.type} price={obj.price} label={label}"
                    )
            except Exception as e:
                session.rollback()
                import traceback

                self.write_log(f"[LIVE_TEST] 写入测试信号失败: {e}\n{traceback.format_exc()}")
            finally:
                session.close()
        except Exception as e:
            import traceback

            self.write_log(f"[LIVE_TEST] 运行测试套件失败: {e}\n{traceback.format_exc()}")

    def get_order_price(self, vt_symbol: str, direction: Direction, fallback_price: float) -> float:
        tick = self.get_active_tick(vt_symbol)
        contract = self.signal_engine.main_engine.get_contract(vt_symbol)
        pricetick = contract.pricetick if contract else None

        signal_type = (self._current_test_signal_type or "").lower()

        if not tick:
            return super().get_order_price(vt_symbol, direction, fallback_price)

        if "aggressive" in signal_type:
            return super().get_order_price(vt_symbol, direction, fallback_price)

        if "reject_up" in signal_type:
            if tick.limit_up and pricetick:
                return float(tick.limit_up) + float(pricetick)
            if tick.limit_up:
                return float(tick.limit_up) * 1.01

        if "reject_down" in signal_type:
            if tick.limit_down and pricetick:
                return float(tick.limit_down) - float(pricetick)
            if tick.limit_down:
                return float(tick.limit_down) * 0.99

        if direction == Direction.LONG:
            if "deep" in signal_type:
                base = float(tick.bid_price_5 or tick.bid_price_1 or tick.last_price or 0)
                if pricetick:
                    base = base - 10 * float(pricetick)
                if tick.limit_down:
                    base = max(base, float(tick.limit_down))
                return base
            if "passive" in signal_type:
                return float(tick.bid_price_1 or tick.last_price or fallback_price)
        else:
            if "deep" in signal_type:
                base = float(tick.ask_price_5 or tick.ask_price_1 or tick.last_price or 0)
                if pricetick:
                    base = base + 10 * float(pricetick)
                if tick.limit_up:
                    base = min(base, float(tick.limit_up))
                return base
            if "passive" in signal_type:
                return float(tick.ask_price_1 or tick.last_price or fallback_price)

        return super().get_order_price(vt_symbol, direction, fallback_price)

    def process_signal(self, signal: Stock):
        self._current_test_signal_type = str(signal.type or "")
        self.live_test_order_tag = self._extract_case_tag(self._current_test_signal_type)

        signal_type = (self._current_test_signal_type or "").lower()
        if "reject_up" in signal_type or "reject_down" in signal_type:
            try:
                vt_symbol = convert_code_to_vnpy_type(signal.code)
                gateway_name = self.get_gateway_name(vt_symbol)
                if gateway_name:
                    gateway = self.signal_engine.main_engine.get_gateway(gateway_name)
                    contract = self.signal_engine.main_engine.get_contract(vt_symbol)
                    pricetick = float(contract.pricetick) if contract else None
                    md = getattr(gateway, "md", None)
                    if md and hasattr(md, "set_synthetic_tick"):
                        md.set_synthetic_tick(vt_symbol, last_price=float(signal.price or 0), pricetick=pricetick)
            except Exception:
                pass

        if self.live_test_order_tag == "force_sell_no_position":
            vt_symbol = convert_code_to_vnpy_type(signal.code)
            gateway_name = self.get_gateway_name(vt_symbol)
            if not gateway_name:
                self.write_log(f"[LIVE_TEST] force_sell_no_position 无法获取网关: {vt_symbol}")
                return True

            fallback_price = float(signal.price or 0)
            price = self.get_order_price(vt_symbol, Direction.SHORT, fallback_price)
            order_type = OrderType.LIMIT
            if price <= 0:
                order_type = OrderType.MARKET
                price = 0

            vt_orderids = self.send_order(
                vt_symbol=vt_symbol,
                direction=Direction.SHORT,
                offset=Offset.CLOSE,
                price=float(price),
                volume=100.0,
                order_type=order_type,
            )
            if vt_orderids:
                self.write_log(f"[LIVE_TEST] force_sell_no_position 已下发卖单: {vt_orderids}")
            else:
                self.write_log("[LIVE_TEST] force_sell_no_position 下单失败")
            return True

        if "invalid_volume" in signal_type:
            vt_symbol = convert_code_to_vnpy_type(signal.code)
            gateway_name = self.get_gateway_name(vt_symbol)
            if not gateway_name:
                self.write_log(f"[LIVE_TEST] invalid_volume 无法获取网关: {vt_symbol}")
                return True

            fallback_price = float(signal.price or 0)
            price = self.get_order_price(vt_symbol, Direction.LONG, fallback_price)
            order_type = OrderType.LIMIT
            if price <= 0:
                order_type = OrderType.MARKET
                price = 0

            vt_orderids = self.send_order(
                vt_symbol=vt_symbol,
                direction=Direction.LONG,
                offset=Offset.OPEN,
                price=float(price),
                volume=1.0,
                order_type=order_type,
            )
            if vt_orderids:
                self.write_log(f"[LIVE_TEST] invalid_volume 已下发买单: {vt_orderids}")
            else:
                self.write_log("[LIVE_TEST] invalid_volume 下单失败")
            return True

        return super().process_signal(signal)

    def _build_signals_for_suite(self, suite: str, run_id: str) -> list[dict]:
        sym = self.test_symbol
        pct = float(self.test_pct)

        smoke = [
            # 用例目的：验证信号写库->策略轮询->下单的最短链路（冒烟）。
            # 触发原理：普通 buy 信号，不注入特殊 case，网关按默认逻辑处理，这里使用aggressive定价更容易成交。
            # 预期现象：日志出现“收到信号/Send new order/下单成功”，并能看到 order/trade/position 更新。
            {"code": sym, "pct": pct, "type": "buy_smoke aggressive", "price": 4.497, "label": f"buy_smoke_{run_id}"},

            # 用例目的：验证卖出链路（含 Offset=CLOSE 方向解析、持仓不足拦截等基础行为）。
            # 触发原理：普通 sell 信号，若无持仓则会被策略侧拦截（用于确认策略的保护逻辑存在），这里使用aggressive定价更容易成交。
            # 预期现象：若无持仓，日志会提示“未找到持仓”；若有持仓，能正常下发卖单并成交。
            {"code": sym, "pct": pct, "type": "sell_smoke aggressive", "price": 4.497, "label": f"sell_smoke_{run_id}"},
        ]

        basic = [
            # 用例目的：验证被动盘口价下单（更贴近实盘）。
            # 触发原理：type 中包含 passive，测试策略会返回买一/卖一作为委托价。
            # 预期现象：订单更容易成交或快速进入 NOTTRADED/ALLTRADED，日志可见定价输出。
            {"code": sym, "pct": pct, "type": "buy passive", "price": 4.497, "label": f"buy_passive_{run_id}"},

            # 用例目的：验证卖出被动价下单路径。
            # 触发原理：type 中包含 passive，卖单使用卖一价格。
            # 预期现象：若有持仓则成交/撤单等回报正常；无持仓则被策略侧拦截（保护行为）。
            {"code": sym, "pct": pct, "type": "sell passive", "price": 4.497, "label": f"sell_passive_{run_id}"},

            # 用例目的：验证“价格越界导致拒单（模拟涨停上方报价）”的处理。
            # 触发原理：type 中包含 reject_up，测试策略会把价格设置到涨停价之上（若 tick 有 limit_up）。
            # 预期现象：网关返回 REJECTED；策略侧不应进入资金不足的延时重挂分支。
            {"code": sym, "pct": pct, "type": "buy reject_up", "price": 4.497, "label": f"buy_reject_up_{run_id}"},

            # 用例目的：验证“长时间不成交 -> 超时撤单 -> 自动重挂”的关键实盘链路。
            # 触发原理：type 中包含 no_fill_60s，策略会把该 token 作为 case 标签注入 OrderRequest.reference；
            #          注意：实盘中无法直接模拟 no_fill_60s，因为实盘订单是否成交取决于市场盘口，无法强制不成交，实盘中主要依赖挂单价格偏离盘口来增加不成交概率。
            #          QMT_SIM 模拟网关根据 case 保持 NOTTRADED，直到超时撤单触发 CANCELLED，策略侧触发撤单重挂。
            # 预期现象：出现“订单超时自动撤单”->“触发撤单重挂”->“重挂已提交”。
            {"code": sym, "pct": pct, "type": "buy no_fill_60s passive", "price": 4.497, "label": f"buy_no_fill_60s_{run_id}"},

            # 用例目的：验证“非法委托数量”导致柜台拒单。
            # 触发原理：type 中包含 invalid_volume，策略强制下发非100整数倍（例如1股）订单。
            # 预期现象：柜台或网关返回 REJECTED（如“最高委托数量合法性校验失败”）。
            {"code": sym, "pct": pct, "type": "buy invalid_volume", "price": 4.497, "label": f"buy_invalid_volume_{run_id}"},

            # 用例目的：验证“卖出无持仓 -> 网关拒单（持仓不足）”的异常路径与策略处理。
            # 触发原理：type 中包含 force_sell_no_position，测试策略绕过父类持仓校验，强制下发卖单；
            #          注意：实盘中如果在策略层被拦截，就不会下发到网关，这里通过强制下发来测试网关的拒单行为。
            # 预期现象：日志出现“REJECTED 持仓不足”，且策略不应进入资金不足的延时重挂分支。
            {"code": sym, "pct": pct, "type": "sell force_sell_no_position", "price": 4.497, "label": f"sell_force_no_pos_{run_id}"},
        ]

        full = [
            # 用例目的：验证被动价全量链路（用于回归对比）。
            # 触发原理：passive 定价，不注入 case。
            # 预期现象：下单/成交/持仓更新正常。
            {"code": sym, "pct": pct, "type": "buy passive", "price": 4.497, "label": f"full_buy_passive_{run_id}"},

            # 用例目的：验证深度价下单在盘口变化时更容易“不成交->撤单重挂”的链路。
            # 触发原理：deep 定价，不注入 case；若盘口变化快可能进入 NOTTRADED 并被超时撤单。
            # 预期现象：可能出现撤单重挂日志（依赖超时参数与撮合参数）。
            {"code": sym, "pct": pct, "type": "buy deep", "price": 4.497, "label": f"full_buy_deep_{run_id}"},

            # 用例目的：验证“部分成交后长时间不继续成交 -> 超时撤单 -> 仅重挂剩余数量”。
            # 触发原理：type 中包含 partial_then_stall，QMT_SIM 在上报后只成交一部分并保持挂起，直到超时撤单。
            # 预期现象：先出现 PARTTRADED，随后出现“订单超时自动撤单”，策略重挂日志中“剩余量=原量-已成交”。
            {"code": sym, "pct": pct, "type": "buy partial_then_stall_5s", "price": 4.497, "label": f"buy_partial_then_stall_{run_id}"},

            # 用例目的：验证“延迟成交”的状态机（先 NOTTRADED，后 ALLTRADED）。
            # 触发原理：type 中包含 delayed_fill_5s，QMT_SIM 在上报后延迟撮合成交。
            # 预期现象：先看到 NOTTRADED，约 5s 后看到成交回报；若超时秒数小于 5s 则会先撤单。
            {"code": sym, "pct": pct, "type": "buy delayed_fill_5s passive", "price": 4.497, "label": f"buy_delayed_fill_5s_{run_id}"},

            # 用例目的：验证“卖出无持仓 -> 网关拒单（持仓不足）”的异常路径与策略处理。
            # 触发原理：type 中包含 force_sell_no_position，测试策略绕过父类持仓校验，强制下发 100 股卖单；
            #          QMT_SIM 开启“卖出持仓不足拒单”后应返回 REJECTED。
            # 预期现象：日志出现“REJECTED 持仓不足”，且策略不应进入资金不足的延时重挂分支。
            {"code": sym, "pct": pct, "type": "sell force_sell_no_position", "price": 4.497, "label": f"sell_force_no_pos_{run_id}"},

            # 用例目的：验证“强制拒单（非资金不足）”时策略不应错误重试。
            # 触发原理：type 中包含 force_reject，QMT_SIM 在上报后直接返回 REJECTED（模拟 ORDER_JUNK/UNKNOWN）。
            # 预期现象：出现 REJECTED，但策略侧不会进入资金不足延时重挂逻辑。
            {"code": sym, "pct": pct, "type": "buy force_reject passive", "price": 4.497, "label": f"buy_force_reject_{run_id}"},

            # 用例目的：验证“价格越界导致拒单（模拟跌停下方报价）”的处理。
            # 触发原理：type 中包含 reject_down，测试策略会把卖单价格设置到跌停价之下（若 tick 有 limit_down）。
            # 预期现象：网关返回 REJECTED；策略侧不应进入资金不足的延时重挂分支。
            {"code": sym, "pct": pct, "type": "sell reject_down", "price": 4.497, "label": f"sell_reject_down_{run_id}"},
        ]

        if suite == "smoke":
            return smoke
        if suite == "basic":
            return basic
        if suite == "full":
            if self.engine_type == EngineType.LIVE.value:
                return smoke + basic
            return full
        if self.engine_type == EngineType.LIVE.value:
            return smoke + basic
        return smoke + basic + full
