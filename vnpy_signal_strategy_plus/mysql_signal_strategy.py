# -*- coding: utf-8 -*-
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timedelta
import time
from threading import Thread
import json
from pathlib import Path

from vnpy_ctp import gateway
from vnpy.trader.object import (
    OrderRequest,
    SubscribeRequest,
    OrderData,
    TradeData,
    TickData,
    BarData
)

from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
from .template import SignalTemplatePlus
from .auto_resubmit import AutoResubmitMixinPlus
from .base import EngineType, CHINA_TZ
from .utils import choose_order_price, convert_code_to_vnpy_type

Base = declarative_base()

class Stock(Base):
    __tablename__ = 'stock_trade'

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False)
    pct = Column(Float, nullable=False)
    type = Column(String(32), nullable=False)  # 下单类型
    price = Column(Float, nullable=False)
    stg = Column(String(32), nullable=False)
    remark = Column(DateTime, nullable=False)  # 下单时间
    processed = Column(Boolean, default=False)

class MySQLSignalStrategyPlus(AutoResubmitMixinPlus, SignalTemplatePlus):
    """
    MySQL信号轮询策略
    """
    author = "VeighNa"

    parameters = [
        "db_host", "db_port", "db_user", 
        "db_password", "db_name", "poll_interval",        
        "engine_type", "start_date", "end_date", "gateway"
    ]
    variables = ["last_signal_id"]
    db_host = ""
    db_port = 3306
    db_user = "root"
    db_password = ""
    db_name = "mysql"
    poll_interval = 0.05
    engine_type = EngineType.LIVE
    start_date = "20250101 00:00:00"
    end_date = "20250201 00:00:00"
    gateway = ""

    def __init__(self, signal_engine):
        super().__init__(signal_engine)
        
        self.last_signal_id = 0
        self.engine = None
        self.Session = None
        self.current_dt = None
        self.engine_type: EngineType = EngineType.LIVE
        self.signal_sim_thread = None
        
        self.is_polling_avtive = True
        self.poll_thread = None
        self.id_processed = []

        self.load_external_setting()
    
    def load_external_setting(self):
        """Load setting from external json file"""
        config_path = Path(__file__).parent.joinpath("mysql_signal_setting.json")
        if not config_path.exists():
            self.write_log(f"未找到外部配置文件: {config_path}")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                setting = json.load(f)

            if self.strategy_name in setting:
                self.update_setting(setting[self.strategy_name])
                self.write_log(f"成功加载策略配置: {self.strategy_name}")
            elif "default" in setting:
                self.update_setting(setting["default"])
                self.write_log("成功加载默认策略配置: default")
            else:
                self.write_log(f"配置文件中未找到策略配置: {self.strategy_name}")
                return

            password_mask = "***" if self.db_password else ""
            self.write_log(
                f"数据库配置生效 host={self.db_host}, port={self.db_port}, user={self.db_user}, "
                f"password={password_mask}, db={self.db_name}, poll_interval={self.poll_interval}"
            )
        except Exception as e:
            self.write_log(f"加载外部配置失败: {e}")
        
    def on_init(self):
        self.write_log("策略初始化")
        self.connect_db()

    def on_start(self):
        self.end_date = datetime.strptime(self.end_date, "%Y%m%d %H:%M:%S")
        self.start_date = datetime.strptime(self.start_date, "%Y%m%d %H:%M:%S")
        if self.engine_type == EngineType.LIVE.value:
            self.write_log("实盘策略启动")
            self.current_dt = datetime.now(CHINA_TZ)
        elif self.engine_type == EngineType.BACKTESTING.value:
            self.write_log("模拟策略启动")
            self.current_dt = self.start_date
            self.current_dt = datetime.combine(self.current_dt, datetime.max.time())
            self.signal_sim_thread = Thread(target=self.signal_simulation, daemon=True)
            self.signal_sim_thread.start()
        else:
            self.write_log(f'unsupported engine type: {self.engine_type}')
        self.is_polling_avtive = True
        self.poll_thread = Thread(target=self.run_polling, daemon=True)
        self.poll_thread.start()

    def on_stop(self):
        self.write_log("策略停止")
        self.is_polling_avtive = False
        # if self.poll_thread:
        #     self.poll_thread.join()
        #     self.poll_thread = None

    def on_tick(self, tick: TickData) -> None:
        pass

    def on_bar(self, bar: BarData) -> None:
        self.write_log(f'收到K线: {bar.datetime} {bar.close}')

    def on_order(self, order: OrderData) -> None:
        """接收订单回报并交由重挂混入层处理。"""
        self.on_order_for_resubmit(order)
        # pass

    def on_timer(self):
        """定时驱动重挂队列执行。"""
        self.on_timer_for_resubmit()
        # pass

    def signal_simulation(self):
        """模拟信号处理"""
        if self.engine_type == EngineType.BACKTESTING.value:
            while self.current_dt < self.end_date:
                if not self.is_polling_avtive:
                    break
                time.sleep(self.poll_interval+0.5)
                self.current_dt += timedelta(days=1)
                self.write_log(f'模拟时间: {self.current_dt}')

    def query_trade_signals(self, session):
        """查询当天未处理的指定策略交易信号"""
        today_start = datetime.combine(self.current_dt, datetime.min.time())  # 当天开始时间
        # today_end = today_start + timedelta(days=1) - timedelta(seconds=1)  # 当天结束时间

        # signals = []
        # if self.engine_type == EngineType.BACKTESTING.value:
        #     signals = session.query(Stock).order_by(Stock.id.asc()).filter(
        #         Stock.stg == self.strategy_name,
        #         Stock.remark >= today_start,
        #         Stock.remark <= self.current_dt,
        #         # Stock.processed == False  # 查询未处理的信号
        #     ).limit(100).all()  # 每次最多处理 100 条信号
        # elif self.engine_type == EngineType.LIVE.value:
        #     signals = session.query(Stock).order_by(Stock.id.asc()).filter(
        #         Stock.stg == self.strategy_name,
        #         Stock.remark >= today_start,
        #         Stock.remark <= self.current_dt,
        #         Stock.processed == False  # 查询未处理的信号
        #     ).limit(100).all()  # 每次最多处理 100 条信号
        # else:
        #     self.write_log(f'unsupported engine type: {self.engine_type}')
        #     return []

        signals = session.query(Stock).order_by(Stock.id.asc()).filter(
            Stock.stg == self.strategy_name,
            Stock.remark >= today_start,
            Stock.remark <= self.current_dt,
            Stock.processed == False,
        ).limit(100).all()  # 每次最多处理 100 条信号

        return signals

    def run_polling(self):
        """独立线程轮询数据库"""
        while self.is_polling_avtive:
            if not self.Session:
                time.sleep(self.poll_interval)
                continue

            if self.engine_type == EngineType.LIVE.value:
                self.current_dt = datetime.now(CHINA_TZ)

            if self.engine_type == EngineType.BACKTESTING.value:
                self.write_log(f'当前时间: {self.current_dt}')

            try:
                session = self.Session()

                signals = self.query_trade_signals(session)
                
                for signal in signals:
                    if not self.is_polling_avtive:
                        break
                    if self.engine_type == EngineType.BACKTESTING.value and signal.id in self.id_processed:
                        self.write_log(f'信号 {signal.id} 已处理，跳过')
                        continue
                    processed = self.process_signal(signal)
                    if processed:
                        self.id_processed.append(signal.id)
                    
                    self.last_signal_id = max(self.last_signal_id, signal.id)
                    # Add a small delay to allow position update if in simulation or rapid trading
                    time.sleep(0.05)
                    
                    try:
                        if processed:
                            signal.processed = True
                            session.commit()
                        else:
                            session.rollback()
                    except Exception as e:
                        session.rollback()
                        self.write_log(f"更新信号processed状态失败: id={signal.id} {e}")

                session.close()
                self.put_event()
                
            except Exception as e:
                import traceback
                self.write_log(f"轮询出错: {e}\n{traceback.format_exc()}")
            
            time.sleep(self.poll_interval)

    def process_signal(self, signal: Stock) -> bool:
        """处理信号"""
        self.write_log(f"收到信号: {signal.id} {signal.code} {signal.pct} {signal.type} {signal.remark}")
        
        # Parse symbol  
        vt_symbol = convert_code_to_vnpy_type(signal.code)
        
        # Determine direction
        direction = Direction.LONG
        offset = Offset.OPEN
        
        signal_type = str(signal.type).lower()
        if "sell" in signal_type or "short" in signal_type or "SELL_LST" in signal_type:
            direction = Direction.SHORT
            offset = Offset.CLOSE # Usually sell means close for stock? Or short open?
                                  # For stock spot: Sell is Close (reduce position). 
                                  # But if it is short selling?
                                  # Let's assume stock spot trading: Buy=Open, Sell=Close.
            if "open" in signal_type:
                offset = Offset.OPEN # Short Open
        elif "buy" in signal_type or "long" in signal_type or "BUY_LST" in signal_type:
            direction = Direction.LONG
            offset = Offset.OPEN
            if "close" in signal_type:
                offset = Offset.CLOSE # Buy to close (cover short)?

        # pct calculation
        pct = float(signal.pct)
        fallback_price = float(signal.price or 0)

        gateway_name = self.get_gateway_name(vt_symbol)
        if not gateway_name:
            self.write_log(f"无法获取网关名称，无法处理信号: {vt_symbol}")
            return True

        if pct <= 1.0:
            calc_price = self.get_best_price(vt_symbol, direction, fallback_price)
            if calc_price <= 0:
                self.write_log(f"无法获取{vt_symbol}有效价格，无法计算百分比仓位")
                return True

            total_capital = self.get_account_asset(gateway_name)
            if total_capital <= 0:
                self.write_log("账户资金为0，无法计算百分比仓位")
                return True

            target_value = total_capital * pct
            vol_int = int(target_value / calc_price)
            vol_int = (vol_int // 100) * 100

            if vol_int <= 0:
                self.write_log(f"下单数量为0 (计算后: {vol_int})，忽略信号: {pct}")
                return True

            if direction == Direction.LONG:

                self.write_log(
                    f"百分比仓位计算(买入): 资金{total_capital} * 比例{signal.pct} / 价格{calc_price} = 数量{vol_int}"
                )
            else:

                vt_positionid = f"{gateway_name}.{vt_symbol}.{Direction.LONG.value}"
                pos = None
                for position in self.signal_engine.main_engine.get_all_positions():
                    if position.vt_positionid == vt_positionid:
                        pos = position
                        break

                if not pos:
                    self.write_log(f"未找到持仓: {vt_positionid}")
                    return True

                if vol_int > int(pos.volume):
                    vol_int = int(pos.volume)

                self.write_log(
                    f"百分比仓位计算(卖出): 持仓{pos.volume} * 比例{signal.pct} = 数量{vol_int}"
                )
        else:
            self.write_log(f'百分比异常！{pct}')
            return True

        price = self.get_order_price(vt_symbol, direction, fallback_price)
        order_type = OrderType.LIMIT
        if price <= 0:
            order_type = OrderType.MARKET
            price = 0

        vt_orderids = self.send_order(
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset, 
            price=price,
            volume=vol_int,
            order_type=order_type
        )
        
        if vt_orderids:
            self.write_log(f"下单成功: {vt_orderids}")
        else:
            self.write_log("下单失败")
        return True

    def get_gateway_name(self, vt_symbol: str) -> str | None:
        contract = self.signal_engine.main_engine.get_contract(vt_symbol)
        exchange_str = vt_symbol.split(".")[-1]
        gateway_name = ""
        if contract:
            gateway_name = contract.gateway_name
        elif self.gateway:
            gateway_name = self.gateway
        else:
            for gw in self.signal_engine.main_engine.gateways.values():
                if Exchange(exchange_str) in gw.exchanges:
                    gateway_name = gw.gateway_name
                    break

        if not gateway_name:
            self.write_log(f"无法确定网关，无法查询持仓: {vt_symbol}")
            return None
        return gateway_name

    def get_active_tick(self, vt_symbol: str) -> TickData | None:
        gateway = None
        if self.gateway:
            gateway = self.signal_engine.main_engine.get_gateway(self.gateway)

        if gateway and hasattr(gateway, "get_full_tick"):
            try:
                tick = gateway.get_full_tick(vt_symbol)
                if tick:
                    return tick
            except Exception as e:
                import traceback

                self.write_log(f"主动获取五档行情异常: {vt_symbol} {e}\n{traceback.format_exc()}")

        return self.signal_engine.main_engine.get_tick(vt_symbol)

    def get_best_price(self, vt_symbol: str, direction: Direction, fallback_price: float) -> float:
        """获取仓位计算参考价（默认买一/卖一，缺失回退last_price/fallback_price）。"""
        tick = self.get_active_tick(vt_symbol)
        contract = self.signal_engine.main_engine.get_contract(vt_symbol)
        pricetick = contract.pricetick if contract else None
        return choose_order_price(tick, direction, fallback_price, pricetick)

    def get_order_price(self, vt_symbol: str, direction: Direction, fallback_price: float) -> float:
        """获取最终下单价（默认等同get_best_price，测试策略可覆盖用于制造场景）。"""
        return self.get_best_price(vt_symbol, direction, fallback_price)

    def connect_db(self):
        """连接数据库"""
        try:
            url = f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
            self.engine = create_engine(url)
            self.Session = sessionmaker(bind=self.engine)
            self.write_log("数据库连接成功")
        except Exception as e:
            self.write_log(f"数据库连接失败: {e}")

    def get_account_asset(self, gateway_name: str):
        for account in self.signal_engine.main_engine.get_all_accounts():
            if account.gateway_name == gateway_name:
                total_capital = float(account.balance)
                self.write_log(f"账户总资产(权益口径): {total_capital}")
                return total_capital
        self.write_log(f"未找到账户信息: {gateway_name}")
        return 0.0
