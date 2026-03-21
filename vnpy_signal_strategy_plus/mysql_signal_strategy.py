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
        "account_id", "db_host", "db_port", "db_user", 
        "db_password", "db_name", "poll_interval",        
        "engine_type", "start_date", "end_date", "gateway"
    ]
    variables = ["last_signal_id"]
    account_id = ""
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
        
        self.active = False
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
            self.current_dt = datetime.now(CHINA_TZ).strftime("%Y%m%d %H:%M:%S")
        elif self.engine_type == EngineType.BACKTESTING.value:
            self.write_log("模拟策略启动")
            self.current_dt = self.start_date
            self.current_dt = datetime.combine(self.current_dt, datetime.max.time())
            self.signal_sim_thread = Thread(target=self.signal_simulation, daemon=True)
            self.signal_sim_thread.start()
        else:
            self.write_log(f'unsupported engine type: {self.engine_type}')
        self.active = True
        self.poll_thread = Thread(target=self.run_polling, daemon=True)
        self.poll_thread.start()

    def on_stop(self):
        self.write_log("策略停止")
        self.active = False
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

    def on_timer(self):
        """定时驱动重挂队列执行。"""
        self.on_timer_for_resubmit()
        # pass

    def signal_simulation(self):
        """模拟信号处理"""
        if self.engine_type == EngineType.BACKTESTING.value:
            while self.current_dt < self.end_date:
                time.sleep(self.poll_interval+0.5)
                self.current_dt += timedelta(days=1)
                self.write_log(f'模拟时间: {self.current_dt}')

    def query_trade_signals(self, session):
        """查询当天未处理的指定策略交易信号"""
        today_start = datetime.combine(self.current_dt, datetime.min.time())  # 当天开始时间
        # today_end = today_start + timedelta(days=1) - timedelta(seconds=1)  # 当天结束时间
        return session.query(Stock).order_by(Stock.id.asc()).filter(
            Stock.stg == self.strategy_name,
            Stock.remark >= today_start,
            Stock.remark <= self.current_dt,
            # Stock.processed == False  # 查询未处理的信号
        ).limit(100).all()  # 每次最多处理 100 条信号

    def run_polling(self):
        """独立线程轮询数据库"""
        while self.active:
            if not self.Session:
                time.sleep(self.poll_interval)
                continue
            self.write_log(f'当前时间: {self.current_dt}')
            try:
                session = self.Session()
                # # Query new signals for this strategy
                # query = session.query(Stock).filter(
                #     Stock.stg == self.strategy_name,
                #     Stock.id > self.last_signal_id,
                #     # Stock.processed == False
                # ).order_by(Stock.id.asc())                
                # signals = query.all()

                signals = self.query_trade_signals(session)
                
                for signal in signals:
                    if not self.active:
                        break
                    if self.engine_type == EngineType.BACKTESTING.value and signal.id in self.id_processed:
                        self.write_log(f'信号 {signal.id} 已处理，跳过')
                        continue
                    self.process_signal(signal)
                    self.last_signal_id = max(self.last_signal_id, signal.id)

                    self.id_processed.append(signal.id)
                    # Add a small delay to allow position update if in simulation or rapid trading
                    time.sleep(0.05)
                    
                session.close()
                self.put_event()
                
            except Exception as e:
                import traceback
                self.write_log(f"轮询出错: {e}\n{traceback.format_exc()}")
            
            time.sleep(self.poll_interval)

    def process_signal(self, signal: Stock):
        """处理信号"""
        self.write_log(f"收到信号: {signal.id} {signal.code} {signal.pct} {signal.type} {signal.remark}")
        
        # Parse symbol
        symbol = signal.code
        exchange_str = "SSE" if symbol.startswith("6") else "SZSE"
        if "." in symbol:
            symbol, suffix = symbol.split(".")
        
        vt_symbol = f"{symbol}.{exchange_str}"
        
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

        # Volume calculation
        volume = signal.pct
        
        # If volume is small (percentage), try to calculate
        if volume <= 1.0:

            # Get last price from engine
            tick = self.signal_engine.main_engine.get_tick(vt_symbol)
            if tick:
                calc_price = tick.last_price
            else:
                self.write_log(f"无法获取{vt_symbol}行情，使用signal.price={signal.price}")
                # return
            # Determine price for calculation
            calc_price = signal.price

            if calc_price <= 0:
                self.write_log(f"signal.price异常，无法计算百分比仓位")
                return

            # Buy: use percentage of capital
            # Get total capital
            total_capital = self.get_account_asset(self.account_id)
            
            if total_capital <= 0:
                self.write_log("账户资金为0，无法计算百分比仓位")
                return
            
            # Calculate target volume
            target_value = total_capital * volume
            volume = target_value / calc_price
        
            # Ensure volume is integer (stock usually 100 multiples, but let's just int)
            # If it was percentage 0.5 -> 0, so it returns below.
            vol_int = int(volume)
            # Round down to nearest 100 for stock?
            # Usually yes for A-share buying. For selling, it can be odd lots if clearing out?
            # But usually we trade in 100s.
            # Let's apply 100 rounding if it's a stock (exchange SSE/SZSE)
            if exchange_str in ["SSE", "SZSE", "SS", "SZ"]:
                vol_int = (vol_int // 100) * 100

            if vol_int <= 0:
                self.write_log(f"下单数量为0 (计算后: {vol_int})，忽略信号: {volume}")
                return

            if direction == Direction.LONG:
                
                self.write_log(f"百分比仓位计算: 资金{total_capital} * 比例{signal.pct} / 价格{calc_price} = 数量{vol_int}")
                
            else:
                # Sell: percentage of holding
                # Find position
                # Try long position for stock
                # Need to find gateway name first to construct correct vt_positionid
                contract = self.signal_engine.main_engine.get_contract(vt_symbol)
                gateway_name = ""
                if contract:
                    gateway_name = contract.gateway_name
                else:
                    # Fallback: try to guess or use the first gateway
                    # For QMT Sim, it's QMT_SIM
                    for gw in self.signal_engine.main_engine.gateways.values():
                        if Exchange(exchange_str) in gw.exchanges:
                            gateway_name = gw.gateway_name
                            break
                
                pos = None
                if gateway_name:
                    # vt_positionid format: gateway_name.symbol.exchange.Direction.Long
                    # Direction.LONG.value is 'Long'
                    vt_positionid = f"{self.account_id}.{vt_symbol}.{Direction.LONG.value}"                    
                    for position in self.signal_engine.main_engine.get_all_positions():
                        if position.vt_positionid == vt_positionid:
                            pos = position
                    
                    if not pos:
                         self.write_log(f"未找到持仓: {vt_positionid}")
                else:
                     self.write_log(f"无法确定网关，无法查询持仓: {vt_symbol}")

                if not pos:
                    # Try legacy/fallback query if needed, or fail
                    pass
                
                if pos:
                    if pos.volume >= vol_int:
                        self.write_log(f"百分比仓位计算(卖出): 持仓{pos.volume} * 比例{signal.pct} = 数量{vol_int}")
                    else:
                        self.write_log(f"[测试]卖出超过当前持仓: 持仓{pos.volume} * 比例{signal.pct} = 数量{vol_int}，清仓")
                        vol_int = pos.volume
                        # return
                else:
                    self.write_log(f"未找到持仓{vt_symbol}，无法计算卖出比例")
                    return

        price = calc_price
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

    def connect_db(self):
        """连接数据库"""
        try:
            url = f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
            self.engine = create_engine(url)
            self.Session = sessionmaker(bind=self.engine)
            self.write_log("数据库连接成功")
        except Exception as e:
            self.write_log(f"数据库连接失败: {e}")

    def get_account_asset(self, account_id: str):
        total_capital = 0.0
        position_capital = 0.0
        account_capital = 0.0
        for position in self.signal_engine.main_engine.get_all_positions():
            # print(f'{position.gateway_name} {position.volume} {position.price}')
            if position.gateway_name == account_id:
                tick = self.signal_engine.main_engine.get_tick(position.vt_symbol)
                if tick:
                    calc_price = tick.last_price
                    position_capital += position.volume * calc_price
                else:
                    # self.write_log(f'未获取到{position.vt_symbol}的最新价格，使用持仓价格计算仓位资产')
                    position_capital += position.volume * position.price
        for account in self.signal_engine.main_engine.get_all_accounts():
            if account.accountid == account_id:
                account_capital += account.balance

        total_capital = position_capital + account_capital
        self.write_log(f"账户总资产: {total_capital, position_capital, account_capital}")
        return total_capital
