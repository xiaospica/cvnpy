from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from vnpy.trader.constant import Direction

from vnpy_signal_strategy_plus.base import CHINA_TZ, EngineType
from vnpy_signal_strategy_plus.mysql_signal_strategy import MySQLSignalStrategyPlus, Stock


class LiveOrderTestStrategyPlus(MySQLSignalStrategyPlus):
    strategy_name = "live_order_test"
    is_live_test_strategy: bool = True

    resubmit_limit: int = 2
    resubmit_interval: int = 2

    test_symbol: str = "510300.SH"
    test_pct: float = 0.01

    def __init__(self, signal_engine: Any):
        super().__init__(signal_engine)
        self._test_run_id: str | None = None
        self._test_suite: str | None = None
        self._current_test_signal_type: str = ""

    def get_test_remark_base(self) -> datetime:
        base_dt = datetime.now(CHINA_TZ)

        if self.engine_type != EngineType.LIVE.value and self.current_dt:
            base_dt = self.current_dt

        if isinstance(base_dt, datetime) and base_dt.tzinfo:
            base_dt = base_dt.replace(tzinfo=None)

        base_date = base_dt.date()
        return datetime.combine(base_date, datetime.min.time()) + timedelta(seconds=1)

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
                for i, s in enumerate(signals):
                    remark = remark_base + timedelta(milliseconds=i * 20)
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
        return super().process_signal(signal)

    def _build_signals_for_suite(self, suite: str, run_id: str) -> list[dict]:
        sym = self.test_symbol
        pct = float(self.test_pct)

        smoke = [
            {"code": sym, "pct": pct, "type": "buy smoke", "price": 4.497, "label": f"buy_smoke_aggressive_{run_id}"},
            {"code": sym, "pct": pct, "type": "sell smoke", "price": 4.497, "label": f"sell_smoke_tplus1_{run_id}"},
            {
                "code": sym,
                "pct": pct,
                "type": "buy smoke passive deep",
                "price": 4.497,
                "label": f"buy_smoke_passive_deep_timeout_{run_id} passive deep",
            },
        ]

        basic = [
            {"code": sym, "pct": pct, "type": "buy basic passive", "price": 4.497, "label": f"buy_basic_passive_{run_id} passive"},
            {"code": sym, "pct": pct, "type": "sell basic passive", "price": 4.497, "label": f"sell_basic_passive_{run_id} passive"},
            {"code": sym, "pct": pct, "type": "buy basic reject_up", "price": 4.497, "label": f"buy_basic_reject_up_{run_id} reject_up"},
            {
                "code": sym,
                "pct": 1.0,
                "type": "buy cash_lock deep",
                "price": 4.497,
                "label": f"buy_resubmit_after_cash_insufficient_{run_id} step1 lock cash deep",
            },
            {
                "code": sym,
                "pct": 0.2,
                "type": "buy cash_insufficient",
                "price": 4.497,
                "label": f"buy_resubmit_after_cash_insufficient_{run_id} step2 expect reject 260200",
            },
        ]

        full = [
            {"code": sym, "pct": pct, "type": "buy full passive", "price": 4.497, "label": f"buy_full_passive_{run_id} passive"},
            {"code": sym, "pct": pct, "type": "buy full deep", "price": 4.497, "label": f"buy_full_deep_{run_id} deep"},
            {"code": sym, "pct": pct, "type": "sell full deep", "price": 4.497, "label": f"sell_full_deep_{run_id} deep"},
            {"code": sym, "pct": pct, "type": "sell full reject_down", "price": 4.497, "label": f"sell_full_reject_down_{run_id} reject_down"},
        ]

        if suite == "smoke":
            return smoke
        if suite == "basic":
            return basic
        if suite == "full":
            return full
        return smoke + basic + full
