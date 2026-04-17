"""
ml_data_build - 机器学习数据构建模块

提供从 Tushare 下载 A 股数据、合并处理、构建 QLib 数据集的完整管道。

主要组件：
- TushareApiClient: Tushare API 客户端（下载、日历、股票信息）
- StockDataProcessor: 数据合并、复权计算
- DataPipeline: 全量/增量数据处理管道
- IndexDataSource / STDataSource: 双数据源抽象（离线/API）
- QLibDatasetBuilder: QLib 数据集构建编排器
- 过滤器类: STFilter, SuspendFilter, OpenLimitFilter 等
"""

from .api_client import TushareApiClient, DaemonThreadPoolExecutor
from .data_processor import StockDataProcessor
from .data_pipeline import DataPipeline, trd_days_since_list
from .data_source import (
    IndexDataSource, STDataSource,
    OfflineIndexDataSource, OfflineSTDataSource,
    TushareIndexDataSource, TushareSTDataSource,
    jq_code_to_tushare,
)
from .qlib_builder import QLibDatasetBuilder
from .qlib_data_helper import (
    StockFilter, STFilter, SuspendFilter, OpenLimitFilter,
    NewStockFilter, TopNFilter, MeanDeviationTopNFilter,
    VolatilityPercentileFilter, CompositeFilter, IndexConstituentFilter,
    IndexInfo, build_custom_index, generate_base_instruments,
    generate_index_instruments, generate_custom_index_instruments,
    add_custom_index,
    vectorized_qlib_converter, init_qlib, test_qlib_data,
    check_instrument_files, validate_vwap_calculation,
    INDEX_CODE_MAP,
)

__all__ = [
    # API 客户端
    'TushareApiClient', 'DaemonThreadPoolExecutor',
    # 数据处理
    'StockDataProcessor',
    # 管道
    'DataPipeline', 'trd_days_since_list',
    # 数据源
    'IndexDataSource', 'STDataSource',
    'OfflineIndexDataSource', 'OfflineSTDataSource',
    'TushareIndexDataSource', 'TushareSTDataSource',
    'jq_code_to_tushare',
    # QLib 构建
    'QLibDatasetBuilder',
    # 过滤器
    'StockFilter', 'STFilter', 'SuspendFilter', 'OpenLimitFilter',
    'NewStockFilter', 'TopNFilter', 'MeanDeviationTopNFilter',
    'VolatilityPercentileFilter', 'CompositeFilter', 'IndexConstituentFilter',
    # QLib 工具
    'IndexInfo', 'build_custom_index', 'generate_base_instruments',
    'generate_index_instruments', 'generate_custom_index_instruments',
    'add_custom_index',
    'vectorized_qlib_converter', 'init_qlib', 'test_qlib_data',
    'check_instrument_files', 'validate_vwap_calculation',
    'INDEX_CODE_MAP',
]
