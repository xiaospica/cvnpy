"""
Tushare 数据管道重构验证测试

核心目标：验证重构后的代码产出的数据与基准数据一模一样。

基准数据：
- daily_merged_all.parquet (15.7M 行, 57 列) - 原始代码完整处理后的结果
- all_ma20_vol_filtered.parquet - 原始代码过滤链的结果

测试策略：
1. Test A (数据管道等价性): 从基准 parquet 反向切分各数据源（daily/adj_factor 等），
   用重构后的管道（merge → ST → list_info → rights_adjustment）重新处理，
   逐列精确对比输出 == 基准
2. Test B (过滤链等价性): 用 baseline + 指数日线（与 notebook 相同输入）跑过滤链，
   与 all_ma20_vol_filtered.parquet 精确匹配 (ts_code, trade_date)
"""

import sys
import os
import pytest
import pandas as pd
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 基准文件路径
BASELINE_PARQUET = r'F:\Quant\code\quant-ml-qlib\factor_factory\stock_data\daily_merged_all.parquet'
FILTERED_BASELINE = r'F:\Quant\code\qlib_strategy_dev\factor_factory\all_ma20_vol_filtered.parquet'
INDEX_DAILY_DIR = r'F:\Quant\code\qlib_strategy_dev\factor_factory\data\index_daily'
LOCAL_DATA_DIR = str(PROJECT_ROOT / 'stock_data')
ST_CSV_PATH = os.path.join(LOCAL_DATA_DIR, 'st_data', 'jq_stock_st_data.csv')
STOCK_LIST_PATH = os.path.join(LOCAL_DATA_DIR, 'stock_list.parquet')


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(scope="session")
def baseline_df():
    """加载基准 parquet（会话级缓存）"""
    if not os.path.exists(BASELINE_PARQUET):
        pytest.skip(f"基准文件不存在: {BASELINE_PARQUET}")
    return pd.read_parquet(BASELINE_PARQUET)


@pytest.fixture(scope="session")
def filtered_baseline():
    """加载过滤后的基准数据"""
    if not os.path.exists(FILTERED_BASELINE):
        pytest.skip(f"过滤基准文件不存在: {FILTERED_BASELINE}")
    return pd.read_parquet(FILTERED_BASELINE)


@pytest.fixture(scope="session")
def stock_info_df():
    """加载股票基本信息"""
    if not os.path.exists(STOCK_LIST_PATH):
        pytest.skip(f"股票列表文件不存在: {STOCK_LIST_PATH}")
    df = pd.read_parquet(STOCK_LIST_PATH)
    if 'list_date' in df.columns and not np.issubdtype(df['list_date'].dtype, np.datetime64):
        df['list_date'] = pd.to_datetime(df['list_date'], errors='coerce')
    if 'delist_date' in df.columns and np.issubdtype(df['delist_date'].dtype, np.datetime64):
        df['delist_date'] = df['delist_date'].dt.strftime('%Y%m%d')
    if 'ts_code' in df.columns:
        df = df.drop_duplicates(subset=['ts_code'], keep='last')
    return df


# =============================================================================
# Import 验证
# =============================================================================

class TestImports:

    def test_core_imports(self):
        from vnpy_tushare_pro.ml_data_build import (
            TushareApiClient, StockDataProcessor, DataPipeline, trd_days_since_list,
        )

    def test_qlib_imports(self):
        from vnpy_tushare_pro.ml_data_build import (
            STFilter, SuspendFilter, OpenLimitFilter, NewStockFilter,
            TopNFilter, MeanDeviationTopNFilter, VolatilityPercentileFilter,
            CompositeFilter, IndexConstituentFilter,
            build_custom_index, vectorized_qlib_converter, INDEX_CODE_MAP,
        )

    def test_data_source_imports(self):
        from vnpy_tushare_pro.ml_data_build import (
            OfflineIndexDataSource, OfflineSTDataSource,
            TushareIndexDataSource, TushareSTDataSource,
            QLibDatasetBuilder, jq_code_to_tushare,
        )

    def test_tushare_datafeed_import_chain(self):
        from vnpy_tushare_pro.tushare_datafeed import TushareDatafeedPro

    def test_old_files_removed(self):
        assert not (PROJECT_ROOT / 'vnpy_tushare_pro' / 'utils.py').exists()
        assert not (PROJECT_ROOT / 'vnpy_tushare_pro' / 'qlib_data_helper.py').exists()

    def test_jq_code_to_tushare(self):
        from vnpy_tushare_pro.ml_data_build import jq_code_to_tushare
        assert jq_code_to_tushare('000001.XSHE') == '000001.SZ'
        assert jq_code_to_tushare('600000.XSHG') == '600000.SH'
        assert jq_code_to_tushare('430047.BJSE') == '430047.BJ'


# =============================================================================
# Test A: 数据管道等价性
# =============================================================================

def _split_raw_data_from_baseline(baseline_df_subset: pd.DataFrame) -> dict:
    """
    从基准 parquet 反向切分出各 raw 数据源。

    基于对 baseline 的列分析：
    - daily: open/high/low/close/pre_close/change/pct_chg/vol/amount
    - adj_factor: adj_factor
    - stk_limit: up_limit/down_limit (实际 API 不返回 pre_close)
    - daily_basic: close_daily_basic(→close)/turnover_rate/.../circ_mv
    - bak_basic: name/industry (name 非空的行)
    - suspend_d: suspend_type/suspend_timing (suspend_type=='S' 的行)
    - stock_st: name_stock_st(→name)/type/type_name (type=='ST' 的行)
    """
    df = baseline_df_subset

    # 1. daily - 所有行都有
    daily = df[['ts_code', 'trade_date', 'open', 'high', 'low', 'close',
                'pre_close', 'change', 'pct_chg', 'vol', 'amount']].copy()
    # 只保留 open 非空的行（代表该日确实有日线数据）
    daily = daily.dropna(subset=['open'])

    # 2. adj_factor
    adj_factor = df[['ts_code', 'trade_date', 'adj_factor']].dropna(subset=['adj_factor']).copy()

    # 3. stk_limit
    stk_limit = df[['ts_code', 'trade_date', 'up_limit', 'down_limit']].dropna(
        subset=['up_limit', 'down_limit'], how='any'
    ).copy()

    # 4. daily_basic - close_daily_basic → close
    db_cols = ['ts_code', 'trade_date', 'close_daily_basic', 'turnover_rate', 'turnover_rate_f',
               'volume_ratio', 'pe', 'pe_ttm', 'pb', 'ps', 'dv_ratio', 'dv_ttm',
               'total_share', 'float_share', 'free_share', 'total_mv', 'circ_mv']
    daily_basic = df[db_cols].dropna(subset=['close_daily_basic']).rename(
        columns={'close_daily_basic': 'close'}
    ).copy()

    # 5. bak_basic - name/industry 非空
    bak_basic = df[['ts_code', 'trade_date', 'name', 'industry']].dropna(
        subset=['name', 'industry'], how='all'
    ).copy()

    # 6. suspend_d - suspend_type == 'S'
    suspend_d = df[df['suspend_type'] == 'S'][
        ['ts_code', 'trade_date', 'suspend_type', 'suspend_timing']
    ].copy()

    # 7. stock_st - type == 'ST'，且 name_stock_st → name
    stock_st = df[df['type'] == 'ST'][
        ['ts_code', 'trade_date', 'name_stock_st', 'type', 'type_name']
    ].rename(columns={'name_stock_st': 'name'}).copy()

    # 转 trade_date 为字符串格式（模拟 Tushare API 返回）
    for data_dict_value in [daily, adj_factor, stk_limit, daily_basic, bak_basic, suspend_d, stock_st]:
        if not data_dict_value.empty:
            data_dict_value['trade_date'] = pd.to_datetime(
                data_dict_value['trade_date']
            ).dt.strftime('%Y%m%d')

    return {
        'daily': daily.reset_index(drop=True),
        'adj_factor': adj_factor.reset_index(drop=True),
        'stk_limit': stk_limit.reset_index(drop=True),
        'daily_basic': daily_basic.reset_index(drop=True),
        'bak_basic': bak_basic.reset_index(drop=True),
        'suspend_d': suspend_d.reset_index(drop=True),
        'stock_st': stock_st.reset_index(drop=True),
    }


class TestDataPipelineEquivalence:
    """
    从基准 parquet 反向切分 raw 数据源，用重构后的管道重新处理，
    逐列精确对比输出与基准一致。
    """

    @pytest.fixture(scope="class")
    def sample_stocks(self, baseline_df):
        """选 30 只有完整数据的股票作为测试样本"""
        # 选有复权数据的股票
        valid = baseline_df.dropna(subset=['adj_factor', 'close_qfq'])
        stock_date_count = valid.groupby('ts_code').size()
        # 取数据量适中的股票（避免只有几条或超大量）
        candidates = stock_date_count[(stock_date_count > 100) & (stock_date_count < 5000)].index.tolist()
        rng = np.random.RandomState(42)
        n = min(30, len(candidates))
        return list(rng.choice(candidates, size=n, replace=False))

    @pytest.fixture(scope="class")
    def baseline_subset(self, baseline_df, sample_stocks):
        """基准数据中样本股票的子集"""
        return baseline_df[baseline_df['ts_code'].isin(sample_stocks)].copy().reset_index(drop=True)

    @pytest.fixture(scope="class")
    def pipeline_output(self, baseline_subset, stock_info_df, baseline_df):
        """运行完整管道处理样本数据"""
        from vnpy_tushare_pro.ml_data_build import StockDataProcessor, DataPipeline
        from vnpy_tushare_pro.ml_data_build.data_pipeline import DataPipeline as DP

        # 1. 反向切分 raw 数据源
        data_dict = _split_raw_data_from_baseline(baseline_subset)

        # 2. merge_all_data
        processor = StockDataProcessor()
        merged = processor.merge_all_data(data_dict)

        # 3. 构造一个假的 pipeline（不需要真实 API client）
        class FakeApiClient:
            data_dir = LOCAL_DATA_DIR

        pipeline = DataPipeline(FakeApiClient(), processor)

        # 4. 应用 ST 数据
        merged = pipeline._apply_jq_st_data(merged)

        # 5. 提取完整交易日历（从基准所有数据的 unique trade_date）
        all_dates = pd.DatetimeIndex(baseline_df['trade_date'].unique()).sort_values()
        calendar = [d.strftime('%Y%m%d') for d in all_dates]

        # 6. 应用上市信息
        merged = pipeline._apply_list_info_and_days(merged, stock_info_df, calendar)

        # 7. 计算复权
        merged = processor.calculate_rights_adjustment(merged)

        # 按主键排序以便对齐
        merged = merged.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        return merged

    def test_pipeline_output_columns(self, pipeline_output, baseline_subset):
        """验证输出列与基准一致"""
        expected_cols = set(baseline_subset.columns)
        actual_cols = set(pipeline_output.columns)
        missing = expected_cols - actual_cols
        assert len(missing) == 0, f"管道输出缺少列: {missing}"

    def test_pipeline_output_row_count(self, pipeline_output, baseline_subset):
        """验证输出行数与基准一致"""
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        assert len(pipeline_output) == len(baseline_sorted), (
            f"行数不匹配: 管道输出 {len(pipeline_output)} vs 基准 {len(baseline_sorted)}"
        )

    def test_pipeline_output_primary_keys_match(self, pipeline_output, baseline_subset):
        """验证 (ts_code, trade_date) 主键完全匹配"""
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        pipe_dates = pd.to_datetime(pipeline_output['trade_date'])
        base_dates = pd.to_datetime(baseline_sorted['trade_date'])

        pipe_keys = set(zip(pipeline_output['ts_code'], pipe_dates))
        base_keys = set(zip(baseline_sorted['ts_code'], base_dates))

        only_in_pipe = pipe_keys - base_keys
        only_in_base = base_keys - pipe_keys

        assert len(only_in_pipe) == 0 and len(only_in_base) == 0, (
            f"主键不匹配:\n"
            f"  仅在管道输出中: {len(only_in_pipe)} 行\n"
            f"  仅在基准中: {len(only_in_base)} 行"
        )

    def test_pipeline_output_daily_values_exact(self, pipeline_output, baseline_subset):
        """验证日线列（open/high/low/close 等）数值完全一致"""
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        pipe_sorted = pipeline_output.copy()
        pipe_sorted['trade_date'] = pd.to_datetime(pipe_sorted['trade_date'])
        pipe_sorted = pipe_sorted.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        for col in ['open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount']:
            if col not in pipe_sorted.columns:
                continue
            pd.testing.assert_series_equal(
                pd.to_numeric(pipe_sorted[col], errors='coerce'),
                pd.to_numeric(baseline_sorted[col], errors='coerce'),
                check_names=False, rtol=0, atol=0,
                obj=f"column {col}"
            )

    def test_pipeline_output_rights_adjustment_exact(self, pipeline_output, baseline_subset):
        """验证复权列（qfq_factor/hfq_factor/*_qfq/*_hfq）完全一致"""
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        pipe_sorted = pipeline_output.copy()
        pipe_sorted['trade_date'] = pd.to_datetime(pipe_sorted['trade_date'])
        pipe_sorted = pipe_sorted.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        rights_cols = ['qfq_factor', 'hfq_factor',
                       'open_qfq', 'high_qfq', 'low_qfq', 'close_qfq', 'pre_close_qfq',
                       'up_limit_qfq', 'down_limit_qfq',
                       'open_hfq', 'high_hfq', 'low_hfq', 'close_hfq', 'pre_close_hfq',
                       'up_limit_hfq', 'down_limit_hfq']

        errors = []
        for col in rights_cols:
            if col not in pipe_sorted.columns:
                errors.append(f"缺少列: {col}")
                continue
            actual = pd.to_numeric(pipe_sorted[col], errors='coerce')
            expect = pd.to_numeric(baseline_sorted[col], errors='coerce')
            mask = actual.notna() & expect.notna()
            if mask.sum() == 0:
                continue
            rel_err = ((actual[mask] - expect[mask]).abs()
                       / expect[mask].abs().replace(0, np.nan)).dropna()
            if len(rel_err) > 0 and rel_err.max() > 1e-9:
                errors.append(f"{col}: 最大相对误差 {rel_err.max():.2e}")

            # 验证 NaN 位置一致
            pipe_nan = actual.isna()
            base_nan = expect.isna()
            nan_diff = (pipe_nan != base_nan).sum()
            if nan_diff > 0:
                errors.append(f"{col}: NaN 位置不一致 ({nan_diff} 行)")

        assert len(errors) == 0, "复权列不匹配:\n" + "\n".join(errors)

    def test_pipeline_output_is_st_exact(self, pipeline_output, baseline_subset):
        """
        验证 is_st 列在两边都有值的位置上完全一致。

        基准 is_st 有三种值：True / False / None（None 表示 JQ ST CSV 未覆盖该日）。
        如果本地 CSV 与生成基准时的版本完全一致，True/False/None 应完全匹配。
        如果本地 CSV 是更新版本（覆盖更长日期），则可能基准为 None 而本地有值。
        我们要求：在两边都有 True/False 值的位置上必须严格相等（不允许 True ↔ False）。
        """
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        pipe_sorted = pipeline_output.copy()
        pipe_sorted['trade_date'] = pd.to_datetime(pipe_sorted['trade_date'])
        pipe_sorted = pipe_sorted.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        # 将 None/NaN 视为缺失，仅比较两边都有 True/False 值的位置
        def normalize(s):
            return s.where(s.isin([True, False]), other=pd.NA)

        pipe_is_st = normalize(pipe_sorted['is_st'])
        base_is_st = normalize(baseline_sorted['is_st'])

        # 两边都非空的位置
        both_valid = pipe_is_st.notna() & base_is_st.notna()
        if both_valid.sum() == 0:
            pytest.skip("两边都没有 True/False 值的位置")

        # 在重叠区域必须完全一致
        mismatch = ((pipe_is_st[both_valid] != base_is_st[both_valid])).sum()
        assert mismatch == 0, (
            f"is_st 在重叠区域不一致: {mismatch}/{both_valid.sum()} 行 "
            f"(True↔False 翻转)"
        )

    def test_pipeline_output_list_info_exact(self, pipeline_output, baseline_subset, stock_info_df):
        """
        验证上市信息：list_date, days_since_list, trd_days_since_list

        注意：trd_days_since_list 是基于交易日历的累计值。
        基准生成时使用的是完整 Tushare 交易日历（含 2005 年前的历史日期）。
        我们从 baseline_df 重建的日历从 2005-01-04 开始，对于 2005 年前上市的股票，
        其 trd_days_since_list 会比基准小（少计了 2005 年前的交易日数量）。
        因此本测试只对比 2005 年后上市的股票。
        """
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        pipe_sorted = pipeline_output.copy()
        pipe_sorted['trade_date'] = pd.to_datetime(pipe_sorted['trade_date'])
        pipe_sorted = pipe_sorted.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        # list_date 必须完全一致
        pipe_list = pd.to_datetime(pipe_sorted['list_date'])
        base_list = pd.to_datetime(baseline_sorted['list_date'])
        list_diff = (pipe_list != base_list) & ~(pipe_list.isna() & base_list.isna())
        assert list_diff.sum() == 0, f"list_date 不一致: {list_diff.sum()} 行"

        # days_since_list 是自然日数，不依赖交易日历，必须严格一致
        pipe_days = pd.to_numeric(pipe_sorted['days_since_list'], errors='coerce')
        base_days = pd.to_numeric(baseline_sorted['days_since_list'], errors='coerce')
        mask = pipe_days.notna() & base_days.notna()
        if mask.sum() > 0:
            diff = (pipe_days[mask] - base_days[mask]).abs()
            assert diff.max() == 0, f"days_since_list 最大差异: {diff.max()}"

        # trd_days_since_list: 仅对 2005 年后上市的股票严格对比
        # （我们重建的日历从 2005 起，无法正确计算 2005 年前上市股票的 trd 天数）
        post_2005_stocks = stock_info_df[
            stock_info_df['list_date'] >= pd.Timestamp('2005-01-04')
        ]['ts_code'].unique()

        post_mask = pipe_sorted['ts_code'].isin(post_2005_stocks)
        if post_mask.sum() == 0:
            pytest.skip("样本中没有 2005 年后上市的股票")

        pipe_trd = pd.to_numeric(pipe_sorted.loc[post_mask, 'trd_days_since_list'], errors='coerce')
        base_trd = pd.to_numeric(baseline_sorted.loc[post_mask, 'trd_days_since_list'], errors='coerce')
        mask = pipe_trd.notna() & base_trd.notna()
        if mask.sum() > 0:
            diff = (pipe_trd[mask] - base_trd[mask]).abs()
            assert diff.max() == 0, (
                f"trd_days_since_list 不一致 (2005 后上市股票): 最大差异 {diff.max()} 天"
            )

    def test_pipeline_output_bak_basic_exact(self, pipeline_output, baseline_subset):
        """验证 name/industry 完全一致（统一处理空值）"""
        baseline_sorted = baseline_subset.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        pipe_sorted = pipeline_output.copy()
        pipe_sorted['trade_date'] = pd.to_datetime(pipe_sorted['trade_date'])
        pipe_sorted = pipe_sorted.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        for col in ['name', 'industry']:
            # 统一处理空值：None/NaN 都视为 ''
            pipe_vals = pipe_sorted[col].where(pipe_sorted[col].notna(), '').astype(str)
            base_vals = baseline_sorted[col].where(baseline_sorted[col].notna(), '').astype(str)

            # 找到不一致的位置
            mismatch_mask = pipe_vals != base_vals
            mismatch = mismatch_mask.sum()
            if mismatch > 0:
                # 打印前 5 个不一致样本帮助调试
                samples = pipe_sorted[mismatch_mask].head(5)[['ts_code', 'trade_date']].copy()
                samples['pipe_val'] = pipe_vals[mismatch_mask].head(5).values
                samples['base_val'] = base_vals[mismatch_mask].head(5).values
                print(f"\n{col} 不一致样本:\n{samples.to_string()}")
            assert mismatch == 0, f"{col} 不一致: {mismatch}/{len(pipe_vals)} 行"


# =============================================================================
# Test B: 过滤链等价性 - 精确匹配 all_ma20_vol_filtered.parquet
# =============================================================================

def _load_index_daily_and_merge(df_all_orig: pd.DataFrame) -> pd.DataFrame:
    """
    复现 notebook 的指数日线合并逻辑：
    加载6个指数 CSV，合并到 df_all_orig。
    """
    INDICES_DAILY_CONFIG = {
        'sh': {'code': '000001.SH', 'name': '上证指数'},
        'sz': {'code': '399001.SZ', 'name': '深证成指'},
        'csi300': {'code': '000300.SH', 'name': '沪深300'},
        'csi500': {'code': '000905.SH', 'name': '中证500'},
        'csi1000': {'code': '000852.SH', 'name': '中证1000'},
        'csi2000': {'code': '399303.SZ', 'name': '国证2000'},
    }
    START_DATE = df_all_orig['trade_date'].min().strftime('%Y%m%d')
    END_DATE = df_all_orig['trade_date'].max().strftime('%Y%m%d')

    all_index_daily = []
    for name in INDICES_DAILY_CONFIG:
        csv_path = os.path.join(INDEX_DAILY_DIR, f'{name}_daily_{START_DATE}_{END_DATE}.csv')
        if os.path.exists(csv_path):
            all_index_daily.append(pd.read_csv(csv_path))

    if not all_index_daily:
        return df_all_orig

    df_index_combined = pd.concat(all_index_daily, ignore_index=True)
    df_index_combined['trade_date'] = pd.to_datetime(df_index_combined['trade_date'])
    df_index_combined.rename(columns={
        'open': 'open_hfq', 'high': 'high_hfq',
        'low': 'low_hfq', 'close': 'close_hfq'
    }, inplace=True)

    # 补充缺失列
    missing_cols = [col for col in df_all_orig.columns if col not in df_index_combined.columns]
    for col in missing_cols:
        if col == 'is_st':
            df_index_combined[col] = False
        elif col in ['suspend_type', 'name_stock_st']:
            df_index_combined[col] = ''
        elif col in ['trd_days_since_list', 'days_since_list']:
            df_index_combined[col] = 99999
        else:
            df_index_combined[col] = np.nan

    df_index_combined = df_index_combined[df_all_orig.columns]
    return pd.concat([df_all_orig, df_index_combined], ignore_index=True)


class TestFilterChainEquivalence:
    """
    使用与 notebook 完全相同的输入（基准 parquet + 指数日线），
    跑过滤链，结果必须与 all_ma20_vol_filtered.parquet 完全一致。
    """

    @pytest.fixture(scope="class")
    def filter_result(self, baseline_df):
        """运行完整过滤链（与 notebook 完全一致的输入）"""
        from vnpy_tushare_pro.ml_data_build import (
            STFilter, SuspendFilter, NewStockFilter, OpenLimitFilter,
            MeanDeviationTopNFilter, VolatilityPercentileFilter,
            build_custom_index,
        )

        # Step 1: 过滤 2026-01-29（notebook 的第一步）
        df = baseline_df[baseline_df['trade_date'] != '2026-01-29'].copy()

        # Step 2: 合并指数日线数据（notebook 的关键步骤）
        df_with_index = _load_index_daily_and_merge(df)

        # Step 3: 运行过滤链（notebook 完全相同）
        filters = [
            STFilter(),
            SuspendFilter(),
            NewStockFilter(),
            OpenLimitFilter(),
            MeanDeviationTopNFilter(n=1000, ascending=True),
            VolatilityPercentileFilter(),
        ]
        filtered_data, custom_name = build_custom_index(
            base_data=df_with_index, filters=filters,
            base_index_name='all', date_col='trade_date',
        )
        return filtered_data, custom_name

    def test_filter_row_count_exact(self, filter_result, filtered_baseline):
        """过滤后行数与基准完全一致"""
        filtered_data, _ = filter_result
        assert len(filtered_data) == len(filtered_baseline), (
            f"行数不匹配: 结果 {len(filtered_data)} vs 基准 {len(filtered_baseline)}"
        )

    def test_filter_keys_exact_match(self, filter_result, filtered_baseline):
        """(ts_code, trade_date) 集合与基准完全一致"""
        filtered_data, _ = filter_result

        result_keys = set(zip(filtered_data['ts_code'], filtered_data['trade_date']))
        expected_keys = set(zip(filtered_baseline['ts_code'], filtered_baseline['trade_date']))

        only_in_result = result_keys - expected_keys
        only_in_expected = expected_keys - result_keys

        assert len(only_in_result) == 0 and len(only_in_expected) == 0, (
            f"过滤结果与基准不完全匹配:\n"
            f"  结果行数: {len(result_keys)}\n"
            f"  基准行数: {len(expected_keys)}\n"
            f"  仅在结果中: {len(only_in_result)} 行, 示例: {list(only_in_result)[:5]}\n"
            f"  仅在基准中: {len(only_in_expected)} 行, 示例: {list(only_in_expected)[:5]}"
        )

    def test_filter_index_name_correct(self, filter_result):
        """自定义指数名称与 notebook 一致"""
        _, custom_name = filter_result
        expected = 'all_no_st_no_suspend_min_90_days_no_open_uplimit_downlimit_top1000_mean_deviation_vol_0.01_0.99'
        assert custom_name == expected, f"指数名称不匹配:\n  实际: {custom_name}\n  期望: {expected}"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=long'])
