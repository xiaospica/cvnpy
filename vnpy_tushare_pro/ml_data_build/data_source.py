"""
双数据源抽象模块

提供离线文件和 Tushare API 两种数据源的统一接口，
用于获取指数成分、指数日线、ST 状态等数据。

离线数据源从聚宽(JoinQuant)导出的 CSV 文件中读取，
API 数据源从 Tushare Pro API 在线下载（部分尚待测试）。
"""

import pandas as pd
import numpy as np
import os
import glob
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from loguru import logger


# =============================================================================
# 代码转换工具函数
# =============================================================================

def jq_code_to_tushare(code: str) -> str:
    """
    聚宽代码转换为 Tushare 代码

    Args:
        code: 聚宽格式代码，如 '000001.XSHE'

    Returns:
        Tushare 格式代码，如 '000001.SZ'

    Examples:
        >>> jq_code_to_tushare('000001.XSHE')
        '000001.SZ'
        >>> jq_code_to_tushare('600000.XSHG')
        '600000.SH'
        >>> jq_code_to_tushare('430047.BJSE')
        '430047.BJ'
    """
    head, suffix = code.split('.')
    suffix_map = {
        'XSHE': '.SZ',
        'XSHG': '.SH',
        'BJSE': '.BJ',
    }
    return head + suffix_map.get(suffix, f'.{suffix}')


def _batch_jq_to_tushare(codes: pd.Series) -> pd.Series:
    """
    批量将聚宽代码转换为 Tushare 代码

    Args:
        codes: 聚宽格式代码 Series

    Returns:
        Tushare 格式代码 Series
    """
    return (
        codes
        .str.replace(r'\.XSHG$', '.SH', regex=True)
        .str.replace(r'\.XSHE$', '.SZ', regex=True)
        .str.replace(r'\.BJSE$', '.BJ', regex=True)
    )


# =============================================================================
# 抽象基类
# =============================================================================

class IndexDataSource(ABC):
    """
    指数数据源抽象基类

    提供获取指数成分和指数日线数据的统一接口。
    """

    @abstractmethod
    def get_index_components(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取指数成分数据

        Args:
            index_code: 指数代码（Tushare格式，如 '000300.SH'）
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD）

        Returns:
            DataFrame，包含列: date(datetime), index_code(str), stock_code(str)
        """
        ...

    @abstractmethod
    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取指数日线数据

        Args:
            index_code: 指数代码（Tushare格式，如 '000300.SH'）
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD）

        Returns:
            DataFrame，包含列: ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
        """
        ...


class STDataSource(ABC):
    """
    ST 状态数据源抽象基类

    提供获取 ST 股票状态数据的统一接口。
    """

    @abstractmethod
    def get_st_data(self) -> pd.DataFrame:
        """
        获取 ST 状态长表数据

        Returns:
            DataFrame，包含列: trade_date(str, YYYYMMDD), ts_code(str), is_st(int/bool)
        """
        ...


# =============================================================================
# 离线数据源实现
# =============================================================================

class OfflineIndexDataSource(IndexDataSource):
    """
    离线指数数据源

    从聚宽导出的 CSV 文件中读取指数成分和指数日线数据。

    Args:
        index_csv_paths: 指数成分 CSV 文件路径映射，
            key 为指数简称（如 'csi300'），value 为 CSV 文件路径或 glob 模式
        index_daily_dir: 指数日线 CSV 文件目录
        index_code_config: 指数代码配置，
            key 为简称，value 为 dict 包含 'code'（Tushare格式代码）和 'name'

    Example:
        >>> source = OfflineIndexDataSource(
        ...     index_csv_paths={
        ...         'csi300': r'F:\\Quant\\jointquant\\index\\hs300_index_info_*.csv',
        ...     },
        ...     index_daily_dir='./data/index_daily',
        ...     index_code_config={
        ...         'csi300': {'code': '000300.SH', 'name': '沪深300'},
        ...     }
        ... )
    """

    def __init__(
        self,
        index_csv_paths: Dict[str, str],
        index_daily_dir: str = './data/index_daily',
        index_code_config: Optional[Dict[str, dict]] = None,
    ):
        self.index_csv_paths = index_csv_paths
        self.index_daily_dir = index_daily_dir
        self.index_code_config = index_code_config or {}
        self._cache: Dict[str, pd.DataFrame] = {}
        self._daily_cache: Dict[str, pd.DataFrame] = {}

    def _resolve_path(self, path_or_glob: str) -> str:
        """解析文件路径，支持 glob 模式"""
        if '*' in path_or_glob or '?' in path_or_glob:
            matches = glob.glob(path_or_glob)
            if not matches:
                raise FileNotFoundError(f"未找到匹配文件: {path_or_glob}")
            # 取最新的文件
            return max(matches, key=os.path.getmtime)
        return path_or_glob

    def _load_index_csv(self, index_name: str) -> pd.DataFrame:
        """加载并缓存指数成分 CSV"""
        if index_name in self._cache:
            return self._cache[index_name]

        if index_name not in self.index_csv_paths:
            logger.warning(f"未配置指数 {index_name} 的 CSV 路径")
            return pd.DataFrame()

        csv_path = self._resolve_path(self.index_csv_paths[index_name])
        logger.info(f"加载指数成分数据: {index_name} <- {csv_path}")

        df = pd.read_csv(csv_path, index_col=0)
        df['date'] = pd.to_datetime(df['date'])
        # 转换聚宽代码为 Tushare 代码
        if 'stock_code' in df.columns:
            df['stock_code'] = _batch_jq_to_tushare(df['stock_code'])

        self._cache[index_name] = df
        return df

    def _find_index_name(self, index_code: str) -> Optional[str]:
        """根据 Tushare 指数代码查找对应的简称"""
        for name, config in self.index_code_config.items():
            if config.get('code') == index_code:
                return name
        return None

    def get_index_components(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取指数成分数据"""
        index_name = self._find_index_name(index_code)
        if index_name is None:
            # 尝试直接用 index_code 作为 key
            for name in self.index_csv_paths:
                if index_code in name or name in index_code.lower():
                    index_name = name
                    break

        if index_name is None:
            logger.warning(f"未找到指数 {index_code} 的配置")
            return pd.DataFrame(columns=['date', 'index_code', 'stock_code'])

        df = self._load_index_csv(index_name)
        if df.empty:
            return pd.DataFrame(columns=['date', 'index_code', 'stock_code'])

        # 添加 index_code 列（使用 Tushare 格式）
        df = df.copy()
        df['index_code'] = index_code

        # 日期过滤
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]

        return df[['date', 'index_code', 'stock_code']].reset_index(drop=True)

    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取指数日线数据"""
        index_name = self._find_index_name(index_code)
        if index_name is None:
            logger.warning(f"未找到指数 {index_code} 的配置")
            return pd.DataFrame()

        if index_name in self._daily_cache:
            df = self._daily_cache[index_name]
        else:
            # 查找日线 CSV 文件
            pattern = os.path.join(self.index_daily_dir, f"{index_name}_daily_*.csv")
            matches = glob.glob(pattern)
            if not matches:
                logger.warning(f"未找到指数 {index_name} 的日线数据文件: {pattern}")
                return pd.DataFrame()

            csv_path = max(matches, key=os.path.getmtime)
            logger.info(f"加载指数日线数据: {index_name} <- {csv_path}")
            df = pd.read_csv(csv_path)
            self._daily_cache[index_name] = df

        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        df = df[(df['trade_date'] >= start_dt) & (df['trade_date'] <= end_dt)]

        return df.reset_index(drop=True)


class OfflineSTDataSource(STDataSource):
    """
    离线 ST 数据源

    从聚宽导出的 CSV 文件中读取 ST 状态数据。
    CSV 文件格式为宽表：行=日期，列=股票代码（聚宽格式），值=is_st。

    Args:
        st_csv_path: ST 数据 CSV 文件路径

    Example:
        >>> source = OfflineSTDataSource('./stock_data/st_data/jq_stock_st_data.csv')
        >>> st_df = source.get_st_data()
    """

    def __init__(self, st_csv_path: str):
        self.st_csv_path = st_csv_path
        self._cache: Optional[pd.DataFrame] = None

    def get_st_data(self) -> pd.DataFrame:
        """获取 ST 状态长表数据"""
        if self._cache is not None:
            return self._cache.copy()

        if not os.path.exists(self.st_csv_path):
            logger.warning(f"ST 数据文件不存在: {self.st_csv_path}")
            return pd.DataFrame(columns=['trade_date', 'ts_code', 'is_st'])

        logger.info(f"加载 ST 数据: {self.st_csv_path}")
        jq_st_data = pd.read_csv(self.st_csv_path)
        jq_st_data.rename(columns={jq_st_data.columns[0]: 'trade_date'}, inplace=True)
        jq_st_data = jq_st_data.set_index(jq_st_data.columns[0])

        # 聚宽代码 → Tushare 代码
        df_st = jq_st_data.rename(columns=jq_code_to_tushare)

        # 宽表 → 长表
        st_long = (
            df_st.stack()
            .reset_index()
            .rename(columns={'level_0': 'trade_date', 'level_1': 'ts_code', 0: 'is_st'})
        )
        st_long['trade_date'] = pd.to_datetime(st_long['trade_date'], errors='coerce').dt.strftime('%Y%m%d')

        self._cache = st_long
        return st_long.copy()


# =============================================================================
# Tushare API 数据源实现（待测试）
# =============================================================================

class TushareIndexDataSource(IndexDataSource):
    """
    Tushare API 指数数据源

    通过 Tushare Pro API 在线下载指数成分和日线数据。

    .. note::
        TODO: 此数据源尚未经过完整测试，API 调用参数和返回格式可能需要调整。

    Args:
        api_client: TushareApiClient 实例
    """

    def __init__(self, api_client: 'TushareApiClient'):
        self.api_client = api_client

    def get_index_components(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Tushare 获取指数成分数据

        TODO: 待测试 - index_weight API 返回格式和字段名可能需要调整
        """
        # TODO: 待测试 - Tushare index_weight API
        logger.info(f"[待测试] 从 Tushare 下载指数成分: {index_code}")
        df = self.api_client.get_index_weight(index_code, start_date, end_date)
        if df is None or df.empty:
            return pd.DataFrame(columns=['date', 'index_code', 'stock_code'])

        # 标准化列名
        result = pd.DataFrame({
            'date': pd.to_datetime(df['trade_date']),
            'index_code': index_code,
            'stock_code': df['con_code'],  # TODO: 确认字段名
        })
        return result

    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Tushare 获取指数日线数据

        TODO: 待测试 - index_daily API 调用方式可能需要调整
        """
        # TODO: 待测试 - Tushare index_daily API
        logger.info(f"[待测试] 从 Tushare 下载指数日线: {index_code}")
        params = {
            'ts_code': index_code,
            'start_date': start_date,
            'end_date': end_date,
        }
        df = self.api_client._safe_api_call(
            'index_daily',
            params,
            f"获取指数{index_code}日线数据"
        )
        if df.empty:
            return pd.DataFrame()

        df['trade_date'] = pd.to_datetime(df['trade_date'])
        return df


class TushareSTDataSource(STDataSource):
    """
    Tushare API ST 数据源

    使用已通过 TushareApiClient 下载的 stock_st 数据。
    stock_st 数据在批量下载时已包含在 data_config 中。

    .. note::
        TODO: 此数据源尚未经过完整测试。

    Args:
        data_dir: 数据目录（包含 raw/stock_st_all.parquet）
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._cache: Optional[pd.DataFrame] = None

    def get_st_data(self) -> pd.DataFrame:
        """
        从已下载的 Tushare stock_st parquet 中获取 ST 数据

        TODO: 待测试 - stock_st 数据格式和字段含义需要确认
        """
        if self._cache is not None:
            return self._cache.copy()

        # TODO: 待测试 - stock_st parquet 的字段结构
        parquet_path = os.path.join(self.data_dir, "raw", "stock_st_all.parquet")
        if not os.path.exists(parquet_path):
            logger.warning(f"[待测试] ST parquet 文件不存在: {parquet_path}")
            return pd.DataFrame(columns=['trade_date', 'ts_code', 'is_st'])

        logger.info(f"[待测试] 加载 Tushare ST 数据: {parquet_path}")
        df = pd.read_parquet(parquet_path)

        # stock_st API 返回的数据中有 is_new 字段标识是否为新ST
        # 这里简化处理：只要出现在 stock_st 表中就标记为 ST
        if 'trade_date' in df.columns and 'ts_code' in df.columns:
            result = df[['trade_date', 'ts_code']].copy()
            result['is_st'] = 1
            result['trade_date'] = pd.to_datetime(result['trade_date'].astype(str)).dt.strftime('%Y%m%d')
            self._cache = result
            return result.copy()

        logger.warning("[待测试] stock_st 数据格式不符合预期")
        return pd.DataFrame(columns=['trade_date', 'ts_code', 'is_st'])
