from datetime import timedelta, datetime
from collections.abc import Callable
from copy import deepcopy
import re
from pathlib import Path

import pandas as pd
from pandas import DataFrame
import tushare as ts
from tushare.pro.client import DataApi

from vnpy.trader.setting import SETTINGS
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData, HistoryRequest
from vnpy.trader.utility import round_to, ZoneInfo

from .ml_data_build import TushareApiClient, StockDataProcessor, DataPipeline
from .scheduler import DailyTimeTaskScheduler

# 数据频率映射
INTERVAL_VT2TS: dict[Interval, str] = {
    Interval.MINUTE: "1min",
    Interval.HOUR: "60min",
    Interval.DAILY: "D",
}

# 股票支持列表
STOCK_LIST: list[Exchange] = [
    Exchange.SSE,
    Exchange.SZSE,
    Exchange.BSE,
]

# 期货支持列表
FUTURE_LIST: list[Exchange] = [
    Exchange.CFFEX,
    Exchange.SHFE,
    Exchange.CZCE,
    Exchange.DCE,
    Exchange.INE,
    Exchange.GFEX
]

# 交易所映射
EXCHANGE_VT2TS: dict[Exchange, str] = {
    Exchange.CFFEX: "CFX",
    Exchange.SHFE: "SHF",
    Exchange.CZCE: "ZCE",
    Exchange.DCE: "DCE",
    Exchange.INE: "INE",
    Exchange.SSE: "SH",
    Exchange.SZSE: "SZ",
    Exchange.BSE: "BJ",
    Exchange.GFEX: "GFE"
}

# 时间调整映射
INTERVAL_ADJUSTMENT_MAP: dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta()
}

# 中国上海时区
CHINA_TZ = ZoneInfo("Asia/Shanghai")

DATA_DIR = Path.cwd() / 'stock_data'

def to_ts_symbol(symbol: str, exchange: Exchange) -> str | None:
    """将交易所代码转换为tushare代码"""
    # 股票
    if exchange in STOCK_LIST:
        ts_symbol: str = f"{symbol}.{EXCHANGE_VT2TS[exchange]}"
    # 期货
    elif exchange in FUTURE_LIST:
        if exchange is not Exchange.CZCE:
            ts_symbol = f"{symbol}.{EXCHANGE_VT2TS[exchange]}".upper()
        else:
            for _count, word in enumerate(symbol):
                if word.isdigit():
                    break

            year: str = symbol[_count]
            month: str = symbol[_count + 1:]
            if year == "9":
                year = "1" + year
            else:
                year = "2" + year

            product: str = symbol[:_count]
            ts_symbol = f"{product}{year}{month}.ZCE".upper()
    else:
        return None

    return ts_symbol


def to_ts_asset(symbol: str, exchange: Exchange) -> str | None:
    """生成tushare资产类别"""
    # 股票
    if exchange in STOCK_LIST:
        if exchange is Exchange.SSE and symbol[0] == "6":
            asset: str = "E"
        elif exchange is Exchange.SSE and symbol[0] == "5":
            asset = "FD"  # 场内etf
        elif exchange is Exchange.SZSE and symbol[0] == "1":
            asset = "FD"  # 场内etf
        # 39开头是指数，比如399001
        elif exchange is Exchange.SZSE and re.search("^(0|3)", symbol) and not symbol.startswith('39'):
            asset= "E"
        # 89开头是指数，比如899050
        elif exchange is Exchange.BSE and not symbol.startswith('89'):
            asset = "E"
        else:
            asset = "I"
    # 期货
    elif exchange in FUTURE_LIST:
        asset = "FT"
    else:
        return None

    return asset


class TushareDatafeedPro(BaseDatafeed):
    """TuShare数据服务接口"""

    def __init__(self) -> None:
        """"""
        self.username: str = SETTINGS["datafeed.username"]
        self.password: str = SETTINGS["datafeed.password"]

        self.inited: bool = False
        self.df_all_stock: DataFrame | None = None

        # 初始化
        self.downloader = TushareApiClient(self.password, DATA_DIR, max_workers=10)
        self.processor = StockDataProcessor()
        self.pipeline = DataPipeline(self.downloader, self.processor)

        self.scheduler = DailyTimeTaskScheduler()
        self.scheduler.register_daily_job(
            name="post_close_update",
            time_str="19:00",
            job_func=self._post_close_update,
        )
        self.scheduler.start()

    def _post_close_update(self) -> None:
        run_date = datetime.now(CHINA_TZ).strftime("%Y%m%d")
        if self.downloader.is_trade_date(run_date):
            logger.info(f"⏭️  跳过任务({name})：非交易日 {run_date}")
            return
        self.update_all_stock_history()

    def set_post_close_update_time(self, time_str: str) -> None:
        self.scheduler.update_job_time("post_close_update", time_str)

    def init(self, output: Callable = print) -> bool:
        """初始化"""
        if self.inited:
            return True

        if not self.username:
            output("Tushare数据服务初始化失败：用户名为空！")
            return False

        if not self.password:
            output("Tushare数据服务初始化失败：密码为空！")
            return False

        ts.set_token(self.password)
        self.pro: DataApi | None = ts.pro_api()
        self.inited = True

        return True

    def query_bar_history(self, req: HistoryRequest, output: Callable = print) -> list[BarData] | None:
        """查询k线数据"""
        if not self.inited:
            self.init(output)

        symbol: str = req.symbol
        exchange: Exchange = req.exchange
        interval: Interval = req.interval
        start: datetime = req.start.strftime("%Y-%m-%d %H:%M:%S")
        end: datetime = req.end.strftime("%Y-%m-%d %H:%M:%S")

        ts_symbol: str | None = to_ts_symbol(symbol, exchange)
        if not ts_symbol:
            return None

        asset: str | None = to_ts_asset(symbol, exchange)
        if not asset:
            return None

        ts_interval: str | None = INTERVAL_VT2TS.get(interval)
        if not ts_interval:
            return None

        adjustment: timedelta = INTERVAL_ADJUSTMENT_MAP[interval]

        try:
            d1: DataFrame = ts.pro_bar(
                ts_code=ts_symbol,
                start_date=start,
                end_date=end,
                asset=asset,
                freq=ts_interval
            )
        except OSError as ex:
            output(f"发生输入/输出错误：{ex.strerror}")
            return []

        df: DataFrame = deepcopy(d1)

        while True:
            if len(d1) != 8000:
                break
            tmp_end: str = d1["trade_time"].values[-1]

            d1 = ts.pro_bar(
                ts_code=ts_symbol,
                start_date=start,
                end_date=tmp_end,
                asset=asset,
                freq=ts_interval
            )
            df = pd.concat([df[:-1], d1])

        bar_keys: list[datetime] = []
        bar_dict: dict[datetime, BarData] = {}
        data: list[BarData] = []

        # 处理原始数据中的NaN值
        df.fillna(0, inplace=True)

        if df is not None:
            for _ix, row in df.iterrows():
                if row["open"] is None:
                    continue

                if interval.value == "d":
                    dt_str: str = row["trade_date"]
                    dt: datetime = datetime.strptime(dt_str, "%Y%m%d")
                else:
                    dt_str = row["trade_time"]
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S") - adjustment

                dt = dt.replace(tzinfo=CHINA_TZ)

                turnover = row.get("amount", 0)
                if turnover is None:
                    turnover = 0

                open_interest = row.get("oi", 0)
                if open_interest is None:
                    open_interest = 0

                bar: BarData = BarData(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    datetime=dt,
                    open_price=round_to(row["open"], 0.000001),
                    high_price=round_to(row["high"], 0.000001),
                    low_price=round_to(row["low"], 0.000001),
                    close_price=round_to(row["close"], 0.000001),
                    volume=row["vol"],
                    turnover=turnover,
                    open_interest=open_interest,
                    gateway_name="TS"
                )

                bar_dict[dt] = bar

        bar_keys = sorted(bar_dict.keys(), reverse=False)
        for i in bar_keys:
            data.append(bar_dict[i])

        return data

    # 获取tushare所有股票日线历史数据
    def query_all_stock_history(self, start_date: str="20050104", end_date: str=None, output: Callable = print) -> DataFrame | None:
        """查询所有股票k线数据"""
        if not self.inited:
            self.init(output)
        if not end_date:
            end_date = datetime.now(CHINA_TZ).strftime("%Y%m%d")
        
        self.df_all_stock = self.pipeline.run_full_pipeline(start_date, end_date)

        if self.df_all_stock is not None:
            output("\n" + "="*50)
            output("📈 处理结果统计")
            output("="*50)
            output(f"总记录数: {len(self.df_all_stock):,}")
            output(f"股票数量: {len(self.df_all_stock['ts_code'].unique()):,}")
            output(f"日期范围: {self.df_all_stock['trade_date'].min()} 到 {self.df_all_stock['trade_date'].max()}")
            output(f"数据列数: {len(self.df_all_stock.columns)}")

            output("\n📊 主要数据列:")
            key_columns = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'close_qfq',
                        'pre_close', 'up_limit', 'down_limit', 'is_suspended', 'is_st',
                        'turnover_rate', 'pe_ttm', 'pb', 'total_mv_qfq']
            available_columns = [col for col in key_columns if col in self.df_all_stock.columns]
            output(f"可用关键列: {', '.join(available_columns)}")

            output("\n🔍 数据预览 (前5行):")
            output(self.df_all_stock.head())

            self.df_all_stock.to_parquet(f"{DATA_DIR}/df_all_stock.parquet")
            return self.df_all_stock
        else:
            output("获取所有股票历史数据失败")
            return None

    def update_all_stock_history(self, start_date: str | None = None, end_date: str | None = None, output: Callable = print) -> DataFrame | None:
        if not self.inited:
            self.init(output)
        if not end_date:
            end_date = datetime.now(CHINA_TZ).strftime("%Y%m%d")

        output(f'增量更新所有股票历史数据，开始日期：{start_date}，结束日期：{end_date}')
        parquet_path = f"{DATA_DIR}/df_all_stock.parquet"
        self.df_all_stock = self.pipeline.run_incremental_pipeline(
            parquet_path=parquet_path,
            start_date=start_date,
            end_date=end_date,
            save_parquet=True
        )
        if self.df_all_stock is None:
            output("增量更新失败")
            return None

        output("\n" + "="*50)
        output("📈 增量更新结果统计")
        output("="*50)
        output(f"总记录数: {len(self.df_all_stock):,}")
        output(f"股票数量: {len(self.df_all_stock['ts_code'].unique()):,}")
        output(f"日期范围: {self.df_all_stock['trade_date'].min()} 到 {self.df_all_stock['trade_date'].max()}")
        output(f"数据列数: {len(self.df_all_stock.columns)}")
        return self.df_all_stock
    
    # 获取一些股票历史数据
    def query_stocks_history(self, stock_list: list[str], output: Callable = print) -> list[BarData] | None:
        """查询一些股票k线数据"""
        if not self.inited:
            self.init(output)

        if self.df_all_stock:
            df: DataFrame = self.df_all_stock[self.df_all_stock["ts_code"].isin(stock_list)]
            return df
        else:
            output("获取所有股票历史数据失败")
            return None

    # 获取指数成分股
    def query_index_composition(self, index_code: str, start_date: str, end_date: str, output: Callable = print) -> pd.DataFrame | None:
        """查询指数成分股"""
        if not self.inited:
            self.init(output)
        
        return self.pipeline.query_index_composition(index_code, start_date, end_date)

    # 加载已下载的所有股票历史数据
    def load_all_stock_history(self, output: Callable = print) -> DataFrame | None:
        """加载已下载的所有股票历史数据"""
        if not self.inited:
            self.init(output)
        
        self.df_all_stock = pd.read_parquet(f"{DATA_DIR}/df_all_stock.parquet")
        return self.df_all_stock
