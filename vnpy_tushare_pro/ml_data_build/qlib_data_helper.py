"""
QLib数据转换辅助函数模块

本模块提供将DataFrame数据转换为QLib格式所需的辅助函数，包括：
- 数据格式转换
- 交易日历生成
- instruments文件生成
- 指数成分管理
- 股票过滤条件类
- 自定义指数构建
"""

import pandas as pd
import numpy as np
from pathlib import Path
import os
import shutil
from tqdm import tqdm
from dataclasses import dataclass
from typing import Dict, List, Optional, Union, Any
from abc import ABC, abstractmethod
from loguru import logger


@dataclass
class IndexInfo:
    """
    指数描述信息数据类

    Attributes:
        code: 指数代码，如 '000300.XSHG'
        name: 指数名称，如 '沪深300'
        instrument_name: qlib中的instruments名称，如 'csi300'
        market: 市场代码，默认 'cn'
    """
    code: str
    name: str
    instrument_name: str
    market: str = 'cn'


# =============================================================================
# 股票过滤条件类
# =============================================================================

class StockFilter(ABC):
    """
    股票过滤条件基类

    所有具体过滤条件类都需要继承此类并实现apply方法。
    支持链式调用，可以通过 & 运算符组合多个过滤条件。

    Example:
        >>> filter1 = STFilter()
        >>> filter2 = TopNFilter('mean_deviation', n=1000, ascending=True)
        >>> combined = filter1 & filter2
    """

    def __init__(self, name: str, description: str = ""):
        """
        初始化过滤条件

        Args:
            name: 过滤条件名称
            description: 过滤条件描述
        """
        self.name = name
        self.description = description

    @abstractmethod
    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        应用过滤条件

        Args:
            df: 输入数据DataFrame
            date_col: 日期列名

        Returns:
            pd.DataFrame: 过滤后的数据
        """
        pass

    def __and__(self, other: 'StockFilter') -> 'CompositeFilter':
        """
        支持使用 & 运算符组合过滤条件

        Args:
            other: 另一个StockFilter实例

        Returns:
            CompositeFilter: 组合后的过滤条件
        """
        return CompositeFilter([self, other])

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name='{self.name}', description='{self.description}')"


class STFilter(StockFilter):
    """
    ST股票过滤条件

    过滤掉ST股票（is_st == 1），只保留非ST股票。

    Example:
        >>> filter = STFilter()
        >>> filtered_df = filter.apply(df)
    """

    def __init__(self):
        super().__init__("no_st", "过滤ST股票")

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        过滤ST股票

        Args:
            df: 输入数据，需要包含is_st列
            date_col: 日期列名（本过滤器不使用，但保持接口一致）

        Returns:
            pd.DataFrame: 非ST股票数据
        """
        if 'is_st' not in df.columns:
            logger.warning("数据中不包含is_st列，跳过ST过滤")
            return df.copy()
        return df[df['is_st'] == 0].copy()


class SuspendFilter(StockFilter):
    """
    停牌股票过滤条件

    过滤掉停牌股票（suspend_type == 'S'），只保留非停牌股票。

    Example:
        >>> filter = SuspendFilter()
        >>> filtered_df = filter.apply(df)
    """

    def __init__(self):
        super().__init__("no_suspend", "过滤停牌股票")

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        过滤停牌股票

        Args:
            df: 输入数据，需要包含suspend_type列
            date_col: 日期列名（本过滤器不使用，但保持接口一致）

        Returns:
            pd.DataFrame: 非停牌股票数据
        """
        if 'suspend_type' not in df.columns:
            logger.warning("数据中不包含suspend_type列，跳过停牌过滤")
            return df.copy()
        return df[df['suspend_type'] != 'S'].copy()


class OpenLimitFilter(StockFilter):
    """
    开盘涨跌停过滤条件

    过滤掉开盘涨停或开盘跌停的股票。
    - 开盘涨停: open == up_limit
    - 开盘跌停: open == down_limit

    Example:
        >>> # 过滤开盘涨停和跌停
        >>> filter = OpenLimitFilter()
        >>> filtered_df = filter.apply(df)
        >>>
        >>> # 只过滤开盘涨停
        >>> filter = OpenLimitFilter(filter_limit_down=False)
        >>> filtered_df = filter.apply(df)
        >>>
        >>> # 使用后复权价格判断
        >>> filter = OpenLimitFilter(price_type='hfq')
        >>> filtered_df = filter.apply(df)
    """

    def __init__(
        self,
        filter_limit_up: bool = True,
        filter_limit_down: bool = True,
        price_type: str = 'original'
    ):
        """
        初始化开盘涨跌停过滤条件

        Args:
            filter_limit_up: 是否过滤开盘涨停，默认True
            filter_limit_down: 是否过滤开盘跌停，默认True
            price_type: 价格类型，可选值：
                - 'original': 使用原始价格 (open, up_limit, down_limit)
                - 'qfq': 使用前复权价格 (open_qfq, up_limit_qfq, down_limit_qfq)
                - 'hfq': 使用后复权价格 (open_hfq, up_limit_hfq, down_limit_hfq)
        """
        if not filter_limit_up and not filter_limit_down:
            raise ValueError("至少需要启用一个过滤条件")

        if price_type not in ['original', 'qfq', 'hfq']:
            raise ValueError("price_type 必须是 'original', 'qfq' 或 'hfq'")

        filters = []
        if filter_limit_up:
            filters.append("uplimit")
        if filter_limit_down:
            filters.append("downlimit")

        name = f"no_open_{'_'.join(filters)}"
        description = f"过滤开盘{'和'.join(filters)}股票"
        if price_type != 'original':
            description += f"({price_type}价格)"

        super().__init__(name, description)
        self.filter_limit_up = filter_limit_up
        self.filter_limit_down = filter_limit_down
        self.price_type = price_type

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        过滤开盘涨跌停股票

        Args:
            df: 输入数据
            date_col: 日期列名（本过滤器不使用，但保持接口一致）

        Returns:
            pd.DataFrame: 过滤后的数据
        """
        import functools
        import operator

        if self.price_type == 'qfq':
            open_col = 'open_qfq'
            up_limit_col = 'up_limit_qfq'
            down_limit_col = 'down_limit_qfq'
        elif self.price_type == 'hfq':
            open_col = 'open_hfq'
            up_limit_col = 'up_limit_hfq'
            down_limit_col = 'down_limit_hfq'
        else:
            open_col = 'open'
            up_limit_col = 'up_limit'
            down_limit_col = 'down_limit'

        required_cols = [open_col]
        if self.filter_limit_up:
            required_cols.append(up_limit_col)
        if self.filter_limit_down:
            required_cols.append(down_limit_col)

        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.warning(f"数据中不包含列 {missing_cols}，跳过开盘涨跌停过滤")
            return df.copy()

        conditions = []
        if self.filter_limit_up:
            conditions.append(df[open_col] == df[up_limit_col])
        if self.filter_limit_down:
            conditions.append(df[open_col] == df[down_limit_col])

        should_filter = functools.reduce(operator.or_, conditions)

        return df[~should_filter].copy()


class NewStockFilter(StockFilter):
    """
    新股过滤条件

    过滤掉上市时间不足指定天数的新股。
    默认为过滤掉上市不足90天的股票。

    Example:
        >>> # 过滤上市不足90天的新股
        >>> filter = NewStockFilter(min_days=90)
        >>> filtered_df = filter.apply(df)
    """

    def __init__(self, min_days: int = 90):
        """
        初始化新股过滤条件

        Args:
            min_days: 最小上市天数，默认为90天
        """
        name = f"min_{min_days}_days"
        description = f"过滤上市不足{min_days}天的新股"
        super().__init__(name, description)
        self.min_days = min_days

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        过滤新股

        Args:
            df: 输入数据，需要包含trd_days_since_list或days_since_list列
            date_col: 日期列名（本过滤器不使用，但保持接口一致）

        Returns:
            pd.DataFrame: 非新股数据
        """
        days_col = None
        if 'trd_days_since_list' in df.columns:
            days_col = 'trd_days_since_list'
        
        if days_col is None:
            logger.warning("数据中不包含trd_days_since_list或days_since_list列，跳过新股过滤")
            return df.copy()
        
        return df[df[days_col] >= self.min_days].copy()


class TopNFilter(StockFilter):
    """
    Top N过滤条件

    按指定列的值取每日前N名或后N名。

    Example:
        >>> # 取mean_deviation最小的1000只股票
        >>> filter = TopNFilter('mean_deviation', n=1000, ascending=True)
        >>> filtered_df = filter.apply(df)
        >>>
        >>> # 取成交量最大的100只股票
        >>> filter = TopNFilter('vol', n=100, ascending=False)
    """

    def __init__(self, column: str, n: int, ascending: bool = True):
        """
        初始化Top N过滤条件

        Args:
            column: 用于排序的列名
            n: 取前N名
            ascending: 是否升序（True表示取最小的N个，False表示取最大的N个）
        """
        name = f"top{n}_{column}" if ascending else f"bottom{n}_{column}"
        description = f"按{column}取{'最小' if ascending else '最大'}{n}名"
        super().__init__(name, description)
        self.column = column
        self.n = n
        self.ascending = ascending

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        应用Top N过滤

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 每日前N名股票数据

        Raises:
            ValueError: 当指定列不存在于数据中
        """
        if self.column not in df.columns:
            raise ValueError(f"数据中不包含列: {self.column}")

        def get_top_n(group: pd.DataFrame) -> pd.DataFrame:
            if self.ascending:
                return group.nsmallest(self.n, self.column)
            else:
                return group.nlargest(self.n, self.column)

        result = df.groupby(date_col, group_keys=False).apply(get_top_n)
        return result.reset_index(drop=True)


class MeanDeviationTopNFilter(StockFilter):
    """
    均值偏离度Top N过滤条件

    根据tushare_alldata_rebuild.ipynb中的逻辑，按mean_deviation取Top N。
    该过滤器会自动计算mean_deviation列（如果数据中没有）。

    mean_deviation计算公式：
        mean_deviation = close_hfq / MA20 - 1
    其中MA20是收盘价的20日移动平均。

    Example:
        >>> # 取均值偏离度最小的1000只股票
        >>> filter = MeanDeviationTopNFilter(n=1000, ascending=True)
        >>> filtered_df = filter.apply(df)
    """

    def __init__(self, n: int, ascending: bool = True, close_col: str = 'close_hfq'):
        """
        初始化均值偏离度Top N过滤条件

        Args:
            n: 取前N名
            ascending: 是否升序（True表示取偏离度最小的N个，通常是价值股）
            close_col: 收盘价列名，默认'close_hfq'
        """
        name = f"top{n}_mean_deviation" if ascending else f"bottom{n}_mean_deviation"
        description = f"取均值偏离度{'最小' if ascending else '最大'}{n}名"
        super().__init__(name, description)
        self.n = n
        self.ascending = ascending
        self.close_col = close_col

    def _calculate_mean_deviation(self, df: pd.DataFrame, date_col: str) -> pd.DataFrame:
        """
        计算mean_deviation列

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 添加了mean_deviation列的数据
        """
        df_copy = df.copy()

        # 如果已经存在mean_deviation列，直接返回
        if 'mean_deviation' in df_copy.columns:
            return df_copy

        # 检查必要的列是否存在
        if self.close_col not in df_copy.columns:
            raise ValueError(f"计算mean_deviation需要{self.close_col}列")

        # 获取股票代码列
        code_col = 'ts_code' if 'ts_code' in df_copy.columns else 'symbol'

        # 按股票代码分组计算MA20和mean_deviation
        def calc_deviation(group: pd.DataFrame) -> pd.DataFrame:
            group = group.sort_values(date_col)
            group['MA20'] = group[self.close_col].rolling(window=20, min_periods=20).mean()
            group['mean_deviation'] = group[self.close_col] / group['MA20'] - 1
            return group

        df_copy = df_copy.groupby(code_col, group_keys=False).apply(calc_deviation)

        # 删除MA20列（临时计算用）
        if 'MA20' in df_copy.columns:
            df_copy = df_copy.drop(columns=['MA20'])

        return df_copy

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        应用均值偏离度Top N过滤

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 过滤后的数据
        """
        # 确保mean_deviation列存在
        df_with_deviation = self._calculate_mean_deviation(df, date_col)

        # 过滤掉NA值
        df_valid = df_with_deviation[df_with_deviation['mean_deviation'].notna()]

        if len(df_valid) == 0:
            logger.warning("没有有效的mean_deviation数据")
            return df_with_deviation

        # 使用TopNFilter进行过滤
        top_n_filter = TopNFilter('mean_deviation', self.n, self.ascending)
        return top_n_filter.apply(df_valid, date_col)


class VolatilityPercentileFilter(StockFilter):
    """
    波动率百分位过滤条件

    根据tushare_alldata_rebuild.ipynb中的逻辑，按20日波动率的百分位进行过滤。
    保留波动率在指定百分位范围内的股票（默认1%-99%）。
    该过滤器会自动计算volatility_20d列（如果数据中没有）。

    volatility_20d计算公式：
        volatility_20d = close_hfq的20日收益率标准差

    Example:
        >>> # 保留波动率在1%-99%之间的股票
        >>> filter = VolatilityPercentileFilter(lower_percentile=0.01, upper_percentile=0.99)
        >>> filtered_df = filter.apply(df)
    """

    def __init__(
        self,
        lower_percentile: float = 0.01,
        upper_percentile: float = 0.99,
        close_col: str = 'close_hfq',
        window: int = 20
    ):
        """
        初始化波动率百分位过滤条件

        Args:
            lower_percentile: 下百分位，默认0.01（1%）
            upper_percentile: 上百分位，默认0.99（99%）
            close_col: 收盘价列名，默认'close_hfq'
            window: 计算波动率的窗口大小，默认20
        """
        name = f"vol_{lower_percentile:.2f}_{upper_percentile:.2f}"
        description = f"保留波动率在{lower_percentile*100:.0f}%-{upper_percentile*100:.0f}%之间的股票"
        super().__init__(name, description)
        self.lower_percentile = lower_percentile
        self.upper_percentile = upper_percentile
        self.close_col = close_col
        self.window = window

    def _calculate_volatility(self, df: pd.DataFrame, date_col: str) -> pd.DataFrame:
        """
        计算volatility_20d列

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 添加了volatility_20d列的数据
        """
        df_copy = df.copy()

        # 如果已经存在volatility_20d列，直接返回
        if 'volatility_20d' in df_copy.columns:
            return df_copy

        # 检查必要的列是否存在
        if self.close_col not in df_copy.columns:
            raise ValueError(f"计算volatility_20d需要{self.close_col}列")

        # 获取股票代码列
        code_col = 'ts_code' if 'ts_code' in df_copy.columns else 'symbol'

        # 按股票代码分组计算波动率
        def calc_volatility(group: pd.DataFrame) -> pd.DataFrame:
            group = group.sort_values(date_col)
            group['volatility_20d'] = group[self.close_col].pct_change().rolling(
                window=self.window, min_periods=self.window
            ).std()
            return group

        df_copy = df_copy.groupby(code_col, group_keys=False).apply(calc_volatility)

        return df_copy

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        应用波动率百分位过滤

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 过滤后的数据
        """
        # 确保volatility_20d列存在
        df_with_volatility = self._calculate_volatility(df, date_col)

        # 过滤掉NA值
        df_valid = df_with_volatility[df_with_volatility['volatility_20d'].notna()]

        if len(df_valid) == 0:
            logger.warning("没有有效的volatility_20d数据")
            return df_with_volatility

        # 计算每日波动率的百分位数
        daily_vol_quantiles = (
            df_valid.groupby(date_col)['volatility_20d']
            .quantile([self.lower_percentile, self.upper_percentile])
            .unstack()
            .rename(columns={
                self.lower_percentile: 'vol_q_lower',
                self.upper_percentile: 'vol_q_upper'
            })
        )

        # 合并分位数到主表
        df_with_quantiles = df_valid.merge(
            daily_vol_quantiles,
            left_on=date_col,
            right_index=True,
            how='left'
        )

        # 应用过滤条件：保留波动率在上下百分位之间的股票
        df_filtered = df_with_quantiles[
            (df_with_quantiles['volatility_20d'] > df_with_quantiles['vol_q_lower']) &
            (df_with_quantiles['volatility_20d'] < df_with_quantiles['vol_q_upper'])
        ]

        # 删除临时计算的列
        if 'vol_q_lower' in df_filtered.columns:
            df_filtered = df_filtered.drop(columns=['vol_q_lower'])
        if 'vol_q_upper' in df_filtered.columns:
            df_filtered = df_filtered.drop(columns=['vol_q_upper'])

        return df_filtered.copy()


class CompositeFilter(StockFilter):
    """
    组合过滤条件

    将多个过滤条件组合在一起依次应用。

    Example:
        >>> filters = [STFilter(), TopNFilter('mean_deviation', 1000, True)]
        >>> composite = CompositeFilter(filters, name="no_st_top1000")
        >>> filtered_df = composite.apply(df)
    """

    def __init__(self, filters: List[StockFilter], name: Optional[str] = None):
        """
        初始化组合过滤条件

        Args:
            filters: 过滤条件列表
            name: 组合名称，如果不提供则自动生成
        """
        if name is None:
            name = "_".join([f.name for f in filters])
        description = "组合过滤: " + ", ".join([f.description for f in filters])
        super().__init__(name, description)
        self.filters = filters

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        依次应用所有过滤条件

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 过滤后的数据
        """
        result = df.copy()
        for f in self.filters:
            logger.info(f"  应用过滤: {f.name}")
            result = f.apply(result, date_col)
            logger.info(f"    过滤后数据量: {len(result)}")
        return result


class IndexConstituentFilter(StockFilter):
    """
    指数成分过滤条件

    使用 inner join 方式合并数据，只保留同时存在于股票数据和指数成分数据中的记录。

    Example:
        >>> # 假设csi300_constituents包含沪深300成分数据
        >>> filter = IndexConstituentFilter(csi300_constituents, index_name="csi300")
        >>> filtered_df = filter.apply(df)
    """

    def __init__(self, index_constituents: pd.DataFrame, index_name: str = "index"):
        """
        初始化指数成分过滤

        Args:
            index_constituents: 指数成分DataFrame，需要包含date和stock_code列
            index_name: 指数名称
        """
        super().__init__(f"in_{index_name}", f"属于{index_name}成分")
        self.index_constituents = index_constituents.copy()
        self.index_name = index_name

    def apply(self, df: pd.DataFrame, date_col: str = 'trade_date') -> pd.DataFrame:
        """
        过滤指数成分股，使用 inner join 方式

        Args:
            df: 输入数据
            date_col: 日期列名

        Returns:
            pd.DataFrame: 指数成分股数据
        """
        df_copy = df.copy()
        index_copy = self.index_constituents.copy()

        # 确保日期格式一致
        df_copy[date_col] = pd.to_datetime(df_copy[date_col])
        index_copy['date'] = pd.to_datetime(index_copy['date'])

        # 确定股票代码列名
        code_col = 'ts_code' if 'ts_code' in df_copy.columns else 'symbol'

        # 使用 inner join 合并，只保留两个数据集中都存在的记录
        df_filtered = pd.merge(
            df_copy,
            index_copy,
            left_on=[date_col, code_col],
            right_on=['date', 'stock_code'],
            how='inner'
        )

        # 删除合并后多余的列
        df_filtered = df_filtered.drop(
            columns=['Unnamed: 0', 'index_code', 'date'],
            errors='ignore'
        )

        return df_filtered


# =============================================================================
# 连续日期范围检测辅助函数
# =============================================================================

def _find_continuous_date_ranges(dates: pd.Series) -> List[tuple]:
    """
    检测日期序列中的连续区间

    当过滤条件（如停牌、ST等）导致某只股票的中间时间段被过滤掉时，
    剩余的日期序列会出现"断点"。本函数用于识别这些连续区间，
    以便在instruments文件中为每只股票输出多段日期范围。

    断点判定规则：相邻两个日期之差 > 1天 则视为断点。
    这是因为股票日线数据中相邻交易日间隔最多为1天（跨周末/节假日），
    如果差值>1天说明中间存在被过滤掉的非连续交易日。

    Args:
        dates: 日期序列（pd.Series），包含某只股票所有通过过滤条件的交易日期

    Returns:
        List[(pd.Timestamp, pd.Timestamp)]: 连续日期范围列表，每个元素为 (start_date, end_date)

    Example:
        >>> dates = pd.Series(pd.to_datetime(['2020-01-01', '2020-01-02', '2020-01-05', '2020-01-06']))
        >>> _find_continuous_date_ranges(dates)
        [(Timestamp('2020-01-01'), Timestamp('2020-01-02')), (Timestamp('2020-01-05'), Timestamp('2020-01-06'))]
    """
    if len(dates) == 0:
        return []

    dates = pd.to_datetime(dates).sort_values().reset_index(drop=True)
    ranges = []
    range_start = dates.iloc[0]

    for i in range(1, len(dates)):
        if dates.iloc[i] - dates.iloc[i - 1] > pd.Timedelta(days=1):
            ranges.append((range_start, dates.iloc[i - 1]))
            range_start = dates.iloc[i]

    ranges.append((range_start, dates.iloc[-1]))
    return ranges


def _find_continuous_date_ranges_v2(
    valid_dates: pd.Series,
    reference_calendar: pd.DatetimeIndex = None
) -> List[tuple]:
    """
    基于参考交易日历的连续区间检测（v2）

    与v1 (_find_continuous_date_ranges)的区别：
    - v1: 基于时间间隔 > pd.Timedelta(days=1) 判定断点 → 周末/节假日被误判为断点
    - v2: 基于参考日历的相邻关系判定断点 → 只有真正缺失的交易日才算断点

    算法逻辑：
        1. 将 valid_dates 转为 set 以便 O(1) 查找
        2. 遍历 reference_calendar（已排序的完整交易日历）:
           - 当前日历日期 ∈ valid_dates → 属于当前区间
           - 当前日历日期 ∉ valid_dates → 区间断开
        3. 输出所有连续区间的 (start, end)

    Args:
        valid_dates: 某只股票的有效日期序列（过滤后/在指数内的日期）
        reference_calendar: 该股票在原始数据中的完整交易日历（已排序）。
            用于判断"连续性"。如果为None，回退到v1逻辑。

    Returns:
        List[(pd.Timestamp, pd.Timestamp)]: 连续日期范围列表

    Example:
        >>> cal = pd.DatetimeIndex(['2023-01-02','2023-01-03','2023-01-04',
        ...                         '2023-01-05','2023-01-08','2023-01-09'])
        >>> dates = pd.Series(pd.to_datetime(['2023-01-02','2023-01-03',
        ...                                  '2023-01-04','2023-01-05',
        ...                                  '2023-01-09']))
        >>> _find_continuous_date_ranges_v2(dates, cal)
        [(Timestamp('2023-01-02'), Timestamp('2023-01-05')),
         (Timestamp('2023-01-09'), Timestamp('2023-01-09'))]
    """
    if len(valid_dates) == 0:
        return []

    if reference_calendar is None or len(reference_calendar) == 0:
        return _find_continuous_date_ranges(valid_dates)

    valid_set = set(pd.to_datetime(valid_dates))
    ref_cal = pd.to_datetime(reference_calendar).sort_values()

    ranges = []
    range_start = None

    for d in ref_cal:
        if d in valid_set:
            if range_start is None:
                range_start = d
        else:
            if range_start is not None:
                ranges.append((range_start, prev_valid))
                range_start = None
        prev_valid = d

    if range_start is not None:
        ranges.append((range_start, prev_valid))

    return ranges


def _resolve_column(df: pd.DataFrame, col_name: str, fallback_candidates: List[str]) -> str:
    """
    解析列名：优先使用指定名称，若不存在则从候选列表中自动检测

    用于处理不同来源的数据可能使用不同列名的情况（如 'trade_date' vs 'date'）。

    Args:
        df: DataFrame
        col_name: 首选列名
        fallback_candidates: 备选列名列表

    Returns:
        str: 实际使用的列名

    Raises:
        KeyError: 当首选和所有备选列名都不存在时

    Example:
        >>> df = pd.DataFrame({'date': [1, 2], 'symbol': ['A', 'B']})
        >>> _resolve_column(df, 'trade_date', ['date', 'time'])
        UserWarning: Column 'trade_date' not found in base_data, using 'date' instead
        'date'
    """
    if col_name in df.columns:
        return col_name
    for candidate in fallback_candidates:
        if candidate in df.columns:
            import warnings
            warnings.warn(
                f"Column '{col_name}' not found in base_data, "
                f"using '{candidate}' instead",
                UserWarning
            )
            return candidate
    raise KeyError(f"Column '{col_name}' not found and no fallback available from {fallback_candidates}")


# =============================================================================
# 基础instrument文件生成函数
# =============================================================================

def generate_base_instruments(
    df_qlib: pd.DataFrame,
    instruments_dir: Union[str, Path],
    index_info: Optional[pd.DataFrame] = None,
    index_code_map: Optional[Dict[str, str]] = None
) -> None:
    """
    生成QLib基础instrument文件

    从已转换的QLib格式数据生成以下文件：
    - all.txt: 全量股票及日期范围（每只股票一行）
    - {year}.txt: 按年份分组的股票日期范围
    - market.txt: 同all.txt的副本（QLib默认市场定义）
    - {index_name}.txt: 指数成分instruments文件（可选，使用v2算法）

    Args:
        df_qlib: 已转换的QLib格式数据（必须包含 symbol, date 列）
        instruments_dir: instruments目录路径
        index_info: 指数成分信息DataFrame（可选），需包含 date, index_code, stock_code 列
        index_code_map: 指数代码映射字典（可选）

    Example:
        >>> generate_base_instruments(
        ...     df_qlib=df_converted,
        ...     instruments_dir='./qlib_data/instruments',
        ...     index_info=index_constituents,
        ...     index_code_map={'000300.XSHG': 'csi300'}
        ... )
    """
    instruments_dir = Path(instruments_dir)

    logger.info("计算股票日期范围...")
    symbol_dates = df_qlib.groupby('symbol')['date'].agg(['min', 'max']).reset_index()
    symbol_dates.columns = ['symbol', 'start_date', 'end_date']
    symbol_dates['start_date_str'] = symbol_dates['start_date'].dt.strftime('%Y-%m-%d')
    symbol_dates['end_date_str'] = symbol_dates['end_date'].dt.strftime('%Y-%m-%d')

    logger.info("创建instrument文件...")

    with open(instruments_dir / "all.txt", 'w') as f:
        for _, row in symbol_dates.iterrows():
            f.write(f"{row['symbol']}\t{row['start_date_str']}\t{row['end_date_str']}\n")

    df_qlib['year'] = df_qlib['date'].dt.year
    years = df_qlib['year'].unique()

    for year in years:
        year_data = df_qlib[df_qlib['year'] == year]
        year_symbol_dates = year_data.groupby('symbol')['date'].agg(['min', 'max']).reset_index()
        year_symbol_dates.columns = ['symbol', 'start_date', 'end_date']
        year_symbol_dates['start_date_str'] = year_symbol_dates['start_date'].dt.strftime('%Y-%m-%d')
        year_symbol_dates['end_date_str'] = year_symbol_dates['end_date'].dt.strftime('%Y-%m-%d')

        with open(instruments_dir / f"{year}.txt", 'w') as f:
            for _, row in year_symbol_dates.iterrows():
                f.write(f"{row['symbol']}\t{row['start_date_str']}\t{row['end_date_str']}\n")

    shutil.copy2(instruments_dir / "all.txt", instruments_dir / "market.txt")

    if index_info is not None and index_code_map is not None:
        logger.info("生成指数成分instruments文件...")
        generate_index_instruments(
            index_info, instruments_dir, index_code_map,
            base_data=df_qlib
        )


# =============================================================================
# 自定义指数构建函数
# =============================================================================

def build_custom_index(
    base_data: pd.DataFrame,
    filters: List[StockFilter],
    base_index_name: str = "all",
    date_col: str = 'trade_date'
) -> tuple[pd.DataFrame, str]:
    """
    构建自定义指数

    根据基础数据和过滤条件构建自定义指数，生成指数名称。

    Args:
        base_data: 基础股票数据
        filters: 过滤条件列表
        base_index_name: 基础指数名称（如'csi300', 'csi500', 'all'）
        date_col: 日期列名

    Returns:
        tuple: (过滤后的数据, 自定义指数名称)
        指数名称格式: {base_index_name}_{filter1}_{filter2}_...

    Example:
        >>> filters = [STFilter(), TopNFilter('mean_deviation', 1000, True)]
        >>> data, name = build_custom_index(df, filters, 'all')
        >>> print(name)  # 输出: all_no_st_top1000_mean_deviation
    """
    logger.info("构建自定义指数...")
    logger.info(f"  基础指数: {base_index_name}")
    logger.info(f"  原始数据量: {len(base_data)}")

    # 应用过滤条件
    composite_filter = CompositeFilter(filters)
    filtered_data = composite_filter.apply(base_data, date_col)

    # 生成指数名称
    custom_index_name = f"{base_index_name}_{composite_filter.name}"

    logger.info(f"  自定义指数名称: {custom_index_name}")
    logger.info(f"  过滤后数据量: {len(filtered_data)}")

    return filtered_data, custom_index_name


def generate_custom_index_instruments(
    custom_index_name: str,
    filtered_data: pd.DataFrame,
    instruments_dir: Union[str, Path],
    code_col: str = 'ts_code',
    date_col: str = 'trade_date',
    base_data: pd.DataFrame = None
) -> Path:
    """
    生成自定义指数的instruments文件

    根据过滤后的数据生成QLib格式的instruments文件。
    支持同一股票输出多段日期范围（当过滤条件导致中间时间段被排除时）。
    使用基于参考交易日历的v2算法，可正确处理停牌/调入调出等场景，
    同时避免将周末/节假日误判为断点（行数最小化）。

    Args:
        custom_index_name: 自定义指数名称
        filtered_data: 过滤后的股票数据
        instruments_dir: instruments目录路径
        code_col: 股票代码列名
        date_col: 日期列名
        base_data: 原始未过滤的全量数据（可选）。提供后将使用v2算法
            （基于参考日历的连续区间检测），输出行数最小化。
            不提供则回退到v1算法（基于时间间隔阈值）。

    Returns:
        Path: 生成的instruments文件路径

    Example:
        >>> # 推荐用法：传入原始数据以启用v2算法
        >>> generate_custom_index_instruments(
        ...     "all_no_st",
        ...     filtered_df,
        ...     "./qlib_data/instruments",
        ...     base_data=df_all_orig
        ... )
    """
    df = filtered_data.copy()
    df[date_col] = pd.to_datetime(df[date_col])

    if base_data is not None:
        base_df = base_data.copy()
        resolved_code_col = _resolve_column(base_df, code_col, ['symbol', 'ts_code', 'stock_code', 'code'])
        resolved_date_col = _resolve_column(base_df, date_col, ['date', 'trade_date', 'time'])
        cal_map = dict(
            (sym, grp[resolved_date_col].sort_values().values)
            for sym, grp in base_df.groupby(resolved_code_col)
        )

    instrument_file = Path(instruments_dir) / f"{custom_index_name}.txt"
    total_lines = 0
    use_v2 = base_data is not None
    with open(instrument_file, 'w') as f:
        for symbol, group in df.groupby(code_col):
            ref_cal = cal_map.get(symbol) if use_v2 else None
            date_ranges = _find_continuous_date_ranges_v2(group[date_col], ref_cal)
            for start, end in date_ranges:
                f.write(
                    f"{symbol}\t"
                    f"{start.strftime('%Y-%m-%d')}\t"
                    f"{end.strftime('%Y-%m-%d')}\n"
                )
                total_lines += 1

    unique_symbols = df[code_col].nunique()
    algo = "v2(参考日历)" if use_v2 else "v1(时间阈值)"
    logger.info(f"生成instruments文件: {instrument_file}")
    logger.info(f"  算法: {algo}")
    logger.info(f"  成分股数量: {unique_symbols}")
    logger.info(f"  总记录行数: {total_lines}（含多段日期范围的股票）")

    return instrument_file


# =============================================================================
# QLib数据转换函数
# =============================================================================

def vectorized_qlib_converter(
    df: pd.DataFrame,
    qlib_dir: str = "./qlib_data",
    index_info: Optional[pd.DataFrame] = None,
    index_code_map: Optional[Dict[str, str]] = None
) -> Path:
    """
    向量化方法快速转换DataFrame为QLib格式

    将包含股票日线数据的DataFrame转换为QLib所需的数据格式，包括：
    - features目录：按股票代码存储的CSV文件
    - instruments目录：股票池定义文件
    - calendars目录：交易日历文件

    Args:
        df: 原始数据DataFrame，需包含以下列：
            - symbol/ts_code: 股票代码
            - date/trade_date: 交易日期
            - open/open_hfq: 开盘价（建议使用后复权价格）
            - close/close_hfq: 收盘价
            - high/high_hfq: 最高价
            - low/low_hfq: 最低价
            - volume/vol: 成交量
            - amount: 成交额
            - factor/adj_factor: 复权因子（可选）
        qlib_dir: QLib数据目录路径
        index_info: 指数成分信息DataFrame，需包含 date, index_code, stock_code 列
        index_code_map: 指数代码映射字典，如 {'000300.XSHG': 'csi300'}

    Returns:
        Path: QLib数据目录路径

    Example:
        >>> qlib_path = vectorized_qlib_converter(
        ...     df_filtered,
        ...     "./qlib_data_cn",
        ...     index_info=df_index_info,
        ...     index_code_map={'000300.XSHG': 'csi300'}
        ... )
    """
    logger.info("开始向量化转换...")

    df_qlib = df.copy()
    
    rename_map = {
        'time': 'date',
        'code': 'symbol',
        'money': 'amount',
        'ts_code': 'symbol',
        'trade_date': 'date',
        'vol': 'volume',
        'open_hfq': 'open',
        'high_hfq': 'high',
        'low_hfq': 'low',
        'close_hfq': 'close',
        'adj_factor': 'factor'
    }
    
    safe_rename_map = {}
    for old_col, new_col in rename_map.items():
        if old_col in df_qlib.columns and new_col not in df_qlib.columns:
            safe_rename_map[old_col] = new_col
    
    if safe_rename_map:
        df_qlib = df_qlib.rename(columns=safe_rename_map)

    logger.info("计算VWAP字段...")
    df_qlib['vwap'] = np.where(
        df_qlib['volume'] > 0,
        df_qlib['amount'] / df_qlib['volume'],
        df_qlib['close']
    )
    df_qlib['vwap'] = df_qlib['vwap'].replace([np.inf, -np.inf], np.nan)
    df_qlib['vwap'] = df_qlib['vwap'].fillna(df_qlib['close'])

    keep_cols = ['symbol', 'date', 'open', 'close', 'high', 'low', 'volume', 'amount', 'vwap']
    if 'factor' in df_qlib.columns:
        keep_cols.append('factor')

    df_qlib = df_qlib[keep_cols]
    df_qlib['date'] = pd.to_datetime(df_qlib['date'])
    df_qlib['symbol'] = df_qlib['symbol'].astype(str)
    df_qlib = df_qlib.sort_values(['symbol', 'date']).reset_index(drop=True)

    qlib_path = Path(qlib_dir)
    if qlib_path.exists():
        shutil.rmtree(qlib_path)

    features_dir = qlib_path / "features"
    instruments_dir = qlib_path / "instruments"
    calendars_dir = qlib_path / "calendars"

    for dir_path in [features_dir, instruments_dir, calendars_dir]:
        dir_path.mkdir(parents=True, exist_ok=True)

    logger.info("生成交易日历...")
    unique_dates = df_qlib['date'].drop_duplicates().sort_values()
    date_strings = unique_dates.dt.strftime('%Y-%m-%d').values
    np.savetxt(calendars_dir / "day.txt", date_strings, fmt='%s')

    logger.info("保存股票数据...")
    symbols = df_qlib['symbol'].unique()

    batch_size = 1000
    for i in tqdm(range(0, len(symbols), batch_size), desc="处理股票批次"):
        batch_symbols = symbols[i:i+batch_size]
        batch_data = df_qlib[df_qlib['symbol'].isin(batch_symbols)]

        for symbol, group in batch_data.groupby('symbol'):
            symbol_file = features_dir / f"{symbol}.csv"
            group[keep_cols].to_csv(symbol_file, index=False)

    logger.info(f"转换完成！共处理 {len(symbols)} 只股票")
    logger.info(f"可用字段: {keep_cols}")
    return qlib_path


def generate_index_instruments(
    df_index_info: pd.DataFrame,
    instruments_dir: Union[str, Path],
    index_code_map: Dict[str, str],
    base_data: pd.DataFrame = None,
    code_col: str = 'stock_code',
    date_col: str = 'date'
) -> None:
    """
    根据指数成分信息生成instruments文件

    为每个指数生成一个独立的instruments文件，文件内容为该指数所有成分股的
    代码及其在该指数中的起始和结束日期。

    支持两种模式：
    - v1 (min/max): 当不提供 base_data 时，每只股票取 min~max 日期输出一行。
      适用于无调入调出的简单场景。
    - v2 (参考日历): 当提供 base_data 时，基于原始交易日历检测连续区间，
      可正确处理股票反复调入调出指数的场景，同时行数最小化。

    Args:
        df_index_info: 指数成分DataFrame，需包含以下列：
            - date: 日期
            - index_code: 指数代码
            - stock_code: 成分股代码
        instruments_dir: instruments目录路径
        index_code_map: 指数代码映射字典
            key: 原始指数代码（如 '000300.XSHG'）
            value: instruments文件名（如 'csi300'）
        base_data: 原始全量数据（可选），用于构建参考日历以启用v2算法
        code_col: 股票代码列名（默认 stock_code，与df_index_info一致）
        date_col: 日期列名（默认 date，与df_index_info一致）

    Example:
        >>> index_code_map = {
        ...     '000300.XSHG': 'csi300',
        ...     '000905.XSHG': 'csi500'
        ... }
        >>> generate_index_instruments(df_index_info, instruments_dir, index_code_map)
        >>> # 启用v2算法（处理调入调出）:
        >>> generate_index_instruments(
        ...     df_index_info, instruments_dir, index_code_map,
        ...     base_data=df_all_orig, code_col='ts_code', date_col='trade_date'
        ... )
    """
    df_index_info = df_index_info.copy()
    df_index_info['date'] = pd.to_datetime(df_index_info['date'])
    instruments_dir = Path(instruments_dir)

    use_v2 = base_data is not None
    if use_v2:
        base_df = base_data.copy()
        resolved_code_col = _resolve_column(base_df, code_col, ['symbol', 'ts_code', 'stock_code', 'code'])
        resolved_date_col = _resolve_column(base_df, date_col, ['date', 'trade_date', 'time'])
        cal_map = dict(
            (sym, grp[resolved_date_col].sort_values().values)
            for sym, grp in base_df.groupby(resolved_code_col)
        )
        logger.debug(f'cal_map: {cal_map.keys()}')

    for index_code, instrument_name in index_code_map.items():
        index_data = df_index_info[df_index_info['index_code'] == index_code]
        if index_data.empty:
            logger.warning(f"指数 {index_code} 没有成分数据")
            continue

        total_lines = 0
        instrument_file = instruments_dir / f"{instrument_name}.txt"
        with open(instrument_file, 'w') as f:
            for symbol, group in index_data.groupby(code_col):
                ref_cal = cal_map.get(symbol) if use_v2 else None
                if use_v2:
                    date_ranges = _find_continuous_date_ranges_v2(group[date_col], ref_cal)
                else:
                    start = group[date_col].min()
                    end = group[date_col].max()
                    date_ranges = [(start, end)]
                for start, end in date_ranges:
                    f.write(
                        f"{symbol}\t"
                        f"{start.strftime('%Y-%m-%d')}\t"
                        f"{end.strftime('%Y-%m-%d')}\n"
                    )
                    total_lines += 1

        unique_symbols = index_data[code_col].nunique()
        algo = "v2(参考日历)" if use_v2 else "v1(min/max)"
        logger.info(f"生成指数instruments文件: {instrument_file}，共 {unique_symbols} 只股票，{total_lines} 条记录 ({algo})")


def add_custom_index(
    index_info: IndexInfo,
    constituents: pd.DataFrame,
    instruments_dir: Union[str, Path],
    base_data: pd.DataFrame = None,
    code_col: str = 'stock_code',
    date_col: str = 'date'
) -> None:
    """
    添加自定义指数成分信息

    允许用户创建自定义的指数成分股池，用于策略回测。
    支持两种模式：
    - v1 (min/max): 当不提供 base_data 时，每只股票取 min~max 日期输出一行
    - v2 (参考日历): 当提供 base_data 时，基于原始交易日历检测连续区间，
      可正确处理调入调出/停牌等场景，行数最小化

    Args:
        index_info: 指数描述信息，IndexInfo实例
        constituents: 成分股DataFrame，需包含以下列：
            - date: 日期
            - stock_code: 成分股代码
        instruments_dir: instruments目录路径
        base_data: 原始全量数据（可选），用于构建参考日历以启用v2算法
        code_col: 股票代码列名（默认 stock_code）
        date_col: 日期列名（默认 date）

    Example:
        >>> custom_index = IndexInfo(
        ...     code='CUSTOM001',
        ...     name='自定义指数',
        ...     instrument_name='custom_index'
        ... )
        >>> add_custom_index(custom_index, constituents_df, instruments_dir)
        >>> # 启用v2算法:
        >>> add_custom_index(custom_index, constituents_df, instruments_dir,
        ...                    base_data=df_all_orig, code_col='ts_code', date_col='trade_date')
    """
    constituents = constituents.copy()
    constituents['date'] = pd.to_datetime(constituents['date'])
    instruments_dir = Path(instruments_dir)

    use_v2 = base_data is not None
    if use_v2:
        base_df = base_data.copy()
        resolved_code_col = _resolve_column(base_df, code_col, ['symbol', 'ts_code', 'stock_code', 'code'])
        resolved_date_col = _resolve_column(base_df, date_col, ['date', 'trade_date', 'time'])
        cal_map = dict(
            (sym, grp[resolved_date_col].sort_values().values)
            for sym, grp in base_df.groupby(resolved_code_col)
        )

    total_lines = 0
    instrument_file = instruments_dir / f"{index_info.instrument_name}.txt"
    with open(instrument_file, 'w') as f:
        for symbol, group in constituents.groupby(code_col):
            ref_cal = cal_map.get(symbol) if use_v2 else None
            if use_v2:
                date_ranges = _find_continuous_date_ranges_v2(group[date_col], ref_cal)
            else:
                start = group[date_col].min()
                end = group[date_col].max()
                date_ranges = [(start, end)]
            for start, end in date_ranges:
                f.write(
                    f"{symbol}\t"
                    f"{start.strftime('%Y-%m-%d')}\t"
                    f"{end.strftime('%Y-%m-%d')}\n"
                )
                total_lines += 1

    unique_symbols = constituents[code_col].nunique()
    algo = "v2(参考日历)" if use_v2 else "v1(min/max)"
    logger.info(f"添加自定义指数 {index_info.name} ({index_info.code})")
    logger.info(f"  文件: {instrument_file}")
    logger.info(f"  算法: {algo}")
    logger.info(f"  成分股数量: {unique_symbols}，总记录行数: {total_lines}")


def init_qlib(qlib_dir: str = "./qlib_data", region: str = "cn") -> None:
    """
    初始化QLib

    初始化QLib环境，设置数据提供者和市场区域。

    Args:
        qlib_dir: QLib数据目录路径
        region: 市场区域，可选 'cn' 或 'us'

    Example:
        >>> init_qlib("./qlib_data_cn", "cn")
    """
    import qlib
    from qlib.constant import REG_CN, REG_US

    region_map = {
        "cn": REG_CN,
        "us": REG_US
    }

    qlib.init(
        provider_uri=str(qlib_dir),
        region=region_map.get(region, REG_CN),
        market="all",
    )

    logger.info("QLib初始化完成！")


def test_qlib_data(
    symbols: Optional[List[str]] = None,
    start_date: str = "2020-01-01",
    end_date: str = "2020-12-31"
) -> Optional[pd.DataFrame]:
    """
    测试QLib数据加载

    验证QLib数据是否正确加载，返回指定股票的行情数据。

    Args:
        symbols: 股票代码列表，如 ['000001.SZ', '000002.SZ']
        start_date: 开始日期，格式 'YYYY-MM-DD'
        end_date: 结束日期，格式 'YYYY-MM-DD'

    Returns:
        pd.DataFrame: 行情数据，如果失败返回None

    Example:
        >>> data = test_qlib_data(['000001.SZ'], '2020-01-01', '2020-12-31')
    """
    from qlib.data import D

    if symbols is None:
        instruments_dir = Path("./qlib_data_cn/instruments")
        all_file = instruments_dir / "all.txt"
        if all_file.exists():
            with open(all_file, 'r') as f:
                symbols = [line.split('\t')[0] for line in f.readlines()[:5]]
        else:
            symbols = []

    if not symbols:
        logger.warning("没有找到测试股票")
        return None

    fields = ["$open", "$close", "$high", "$low", "$volume", "$amount", "$vwap"]

    logger.info(f"测试加载数据: {symbols}")
    data = D.features(symbols, fields, start_time=start_date, end_time=end_date)
    logger.info("数据加载成功！")
    logger.info(f"数据形状: {data.shape}")
    logger.info(f"\n{data.head()}")

    return data


def check_instrument_files(qlib_dir: str = "./qlib_data") -> bool:
    """
    检查instrument文件格式是否正确

    验证instruments文件是否符合QLib要求的格式。

    Args:
        qlib_dir: QLib数据目录路径

    Returns:
        bool: 格式是否正确

    Example:
        >>> if check_instrument_files("./qlib_data_cn"):
        ...     print("格式正确")
    """
    instruments_dir = Path(qlib_dir) / "instruments"

    logger.info("检查instrument文件格式...")

    all_file = instruments_dir / "all.txt"
    if all_file.exists():
        with open(all_file, 'r') as f:
            lines = f.readlines()
            if lines:
                logger.info("all.txt文件格式示例:")
                for i, line in enumerate(lines[:5]):
                    logger.info(f"  {i+1}: {line.strip()}")

                first_line = lines[0].strip()
                parts = first_line.split('\t')
                if len(parts) == 3:
                    logger.info("instrument文件格式正确")
                else:
                    logger.error("instrument文件格式不正确，应有3列（代码、开始日期、结束日期）")
                    return False
            else:
                logger.error("all.txt文件为空")
                return False
    else:
        logger.error("缺少all.txt文件")
        return False

    return True


def validate_vwap_calculation(df_sample: pd.DataFrame) -> bool:
    """
    验证VWAP计算是否正确

    检查VWAP字段的计算结果是否符合预期。

    Args:
        df_sample: 包含VWAP字段的DataFrame

    Returns:
        bool: 验证是否通过

    Example:
        >>> validate_vwap_calculation(df_qlib)
    """
    logger.info("验证VWAP计算...")

    sample_symbols = df_sample['symbol'].unique()[:3]

    for symbol in sample_symbols:
        symbol_data = df_sample[df_sample['symbol'] == symbol].head(5)
        logger.info(f"验证股票 {symbol} 的VWAP计算:")

        for idx, row in symbol_data.iterrows():
            calculated_vwap = row['amount'] / row['volume'] if row['volume'] > 0 else row['close']
            logger.info(f"  日期: {row['date']}, 成交额: {row['amount']:.2f}, 成交量: {row['volume']:.2f}")
            logger.info(f"  计算VWAP: {calculated_vwap:.4f}, 实际VWAP: {row['vwap']:.4f}")
            logger.info(f"  差异: {abs(calculated_vwap - row['vwap']):.6f}")

    return True


# =============================================================================
# 常量定义
# =============================================================================

INDEX_CODE_MAP = {
    '000300.XSHG': 'csi300',
    '000905.XSHG': 'csi500',
    '000852.XSHG': 'csi1000',
}


# =============================================================================
# 主程序
# =============================================================================

if __name__ == '__main__':
    """
    QLib数据转换辅助函数使用示例

    运行方式:
        python qlib_data_helper.py
    """

    # 示例1: 创建示例数据并转换为QLib格式
    # import pandas as pd
    # import numpy as np
    #
    # # 创建示例数据
    # dates = pd.date_range('2020-01-01', '2020-12-31', freq='B')
    # symbols = ['000001.SZ', '000002.SZ', '600000.SH']
    #
    # data = []
    # for symbol in symbols:
    #     for date in dates:
    #         data.append({
    #             'symbol': symbol,
    #             'date': date,
    #             'open': np.random.uniform(10, 20),
    #             'close': np.random.uniform(10, 20),
    #             'high': np.random.uniform(10, 22),
    #             'low': np.random.uniform(8, 18),
    #             'volume': np.random.randint(100000, 1000000),
    #             'amount': np.random.randint(1000000, 10000000),
    #             'factor': 1.0
    #         })
    #
    # df = pd.DataFrame(data)
    #
    # # 转换数据
    # qlib_path = vectorized_qlib_converter(
    #     df,
    #     './qlib_data_example',
    #     index_info=None,
    #     index_code_map=None
    # )
    # print(f"数据已转换到: {qlib_path}")

    # 示例2: 添加自定义指数
    # custom_index = IndexInfo(
    #     code='CUSTOM001',
    #     name='自定义策略指数',
    #     instrument_name='custom_strategy'
    # )
    #
    # # 创建自定义成分数据
    # custom_constituents = pd.DataFrame({
    #     'date': pd.date_range('2020-01-01', '2020-12-31', freq='MS'),
    #     'stock_code': ['000001.SZ'] * 12
    # })
    #
    # add_custom_index(custom_index, custom_constituents, qlib_path / 'instruments')

    # 示例3: 初始化QLib并测试数据
    # init_qlib('./qlib_data_example', 'cn')
    # data = test_qlib_data(['000001.SZ'], '2020-01-01', '2020-01-31')
    # print(data.head())

    # 示例4: 检查instrument文件格式
    # is_valid = check_instrument_files('./qlib_data_example')
    # print(f"Instrument文件格式正确: {is_valid}")

    # 示例5: 验证VWAP计算
    # validate_vwap_calculation(df)

    # 示例6: 使用股票过滤条件
    # from qlib_data_helper import STFilter, TopNFilter, MeanDeviationTopNFilter, build_custom_index
    #
    # # 构建自定义指数：全市场非ST股票中mean_deviation最小的1000只
    # filters = [
    #     STFilter(),
    #     MeanDeviationTopNFilter(n=1000, ascending=True)
    # ]
    # custom_data, custom_name = build_custom_index(
    #     base_data=df,
    #     filters=filters,
    #     base_index_name='all',
    #     date_col='date'
    # )
    # print(f"自定义指数名称: {custom_name}")
    #
    # # 生成instruments文件
    # generate_custom_index_instruments(
    #     custom_index_name=custom_name,
    #     filtered_data=custom_data,
    #     instruments_dir='./qlib_data_example/instruments'
    # )

    logger.info("请取消注释上述示例代码后运行")
