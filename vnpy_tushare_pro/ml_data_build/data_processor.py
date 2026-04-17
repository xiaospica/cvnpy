"""
数据处理器模块

提供股票数据的合并、清洗、复权计算等功能：
- 多数据源合并（日线、复权因子、涨跌停、每日指标等）
- 前复权(QFQ)/后复权(HFQ)价格计算
- 按股票拆分保存
"""

import pandas as pd
import numpy as np
import os
from tqdm import tqdm
from typing import Dict, List
from loguru import logger


class StockDataProcessor:
    """
    股票数据处理器

    提供数据合并、复权计算、拆分保存等静态方法。
    """

    @staticmethod
    def merge_all_data(data_dict: Dict[str, pd.DataFrame],
                      primary_key: List[str] = ['ts_code', 'trade_date']) -> pd.DataFrame:
        """
        通用数据合并函数，支持所有数据类型

        即使某些数据类型为空，也会创建对应列并填充NaN，保证最终DataFrame结构完整。

        Args:
            data_dict: 包含各种数据类型的字典
            primary_key: 合并主键

        Returns:
            合并后的DataFrame（包含所有配置列）
        """
        logger.info("开始合并所有数据...")

        # 检查基础数据
        if 'daily' not in data_dict or data_dict['daily'].empty:
            logger.error("缺少基础日线数据，无法合并")
            return pd.DataFrame()

        data_type_fields = {
            'daily': ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount'],
            'adj_factor': ['ts_code', 'trade_date', 'adj_factor'],
            'stk_limit': ['ts_code', 'trade_date', 'pre_close', 'up_limit', 'down_limit'],
            'daily_basic': ['ts_code', 'trade_date', 'close', 'turnover_rate', 'turnover_rate_f', 'volume_ratio', 'pe', 'pe_ttm', 'pb', 'ps', 'dv_ratio', 'dv_ttm', 'total_share', 'float_share', 'free_share', 'total_mv', 'circ_mv'],
            'bak_basic': ['ts_code', 'trade_date', 'name', 'industry'],
            'suspend_d': ['ts_code', 'suspend_type', 'trade_date', 'suspend_timing'],
            'stock_st': ['ts_code', 'name', 'trade_date', 'type', 'type_name']
        }

        # 按优先级顺序合并（重要数据优先）
        merge_order = [
            'daily',           # 1. 基础日线数据
            'adj_factor',      # 2. 复权因子（计算复权价格必需）
            'stk_limit',       # 3. 涨跌停价格（技术分析重要）
            'daily_basic',     # 4. 每日指标
            'bak_basic',       # 5. 备用财务数据
            'suspend_d',       # 6. 停复牌信息
            'stock_st'         # 7. ST状态
        ]

        # 初始化合并结果
        merged_df = data_dict['daily'].copy()
        logger.info(f"基础日线数据: {len(merged_df)}条记录")

        # 逐个合并其他数据
        for data_type in merge_order[1:]:
            # 检查数据类型是否存在（即使为空也处理）
            if data_type in data_dict:
                df_to_merge = data_dict[data_type].copy()

                # 如果DataFrame为空，创建只包含主键的空DataFrame
                if df_to_merge.empty:
                    logger.warning(f"{data_type} 数据为空，将创建空列")
                    # 获取该数据类型应有的所有字段
                    expected_fields = data_type_fields.get(data_type, [])
                    if not expected_fields:
                        logger.warning(f"无法获取 {data_type} 的字段定义，跳过")
                        continue

                    # 提取主键的唯一组合（基于已有的merged_df）
                    primary_keys_df = merged_df[primary_key].drop_duplicates()

                    # 创建包含所有应有字段的空DataFrame（值为NaN）
                    empty_cols = {col: np.nan for col in expected_fields if col not in primary_key}
                    df_to_merge = primary_keys_df.copy()
                    for col, val in empty_cols.items():
                        df_to_merge[col] = val

                    logger.info(f"   -> 为 {data_type} 创建了包含 {len(df_to_merge.columns)} 列的空结构")
                else:
                    # 确保主键格式一致
                    for key in primary_key:
                        if key in merged_df.columns and key in df_to_merge.columns:
                            merged_df[key] = merged_df[key].astype(str)
                            df_to_merge[key] = df_to_merge[key].astype(str)

                # 合并（使用outer join，缺失值自动填充为NaN）
                before_count = len(merged_df)
                merged_df = pd.merge(
                    merged_df,
                    df_to_merge,
                    on=primary_key,
                    how='outer',
                    suffixes=('', f'_{data_type}')
                )
                after_count = len(merged_df)

                # 记录合并结果
                added_cols = set(df_to_merge.columns) - set(primary_key)
                if added_cols:
                    logger.info(f"合并 {data_type}: {len(df_to_merge)}条, 新增列: {', '.join(added_cols)} -> 合并后: {after_count}条")
                else:
                    logger.info(f"合并 {data_type}: 空数据，仅保留主键 -> 合并后: {after_count}条")
            else:
                logger.warning(f"{data_type} 不存在于data_dict中，跳过")

        logger.info(f"最终合并结果: {len(merged_df)}条记录, {len(merged_df.columns)}列")
        logger.info(f"数据预览:\n{merged_df.head().to_string()}")

        return merged_df

    @staticmethod
    def _post_merge_processing(df: pd.DataFrame) -> pd.DataFrame:
        """
        合并后的数据清洗和处理

        Args:
            df: 合并后的DataFrame

        Returns:
            清洗后的DataFrame
        """
        logger.info("开始数据清洗和后处理...")

        # 确保数值列类型正确
        numeric_columns = ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount', 'turnover_rate', 'pe', 'pb']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 按股票和日期排序
        if 'ts_code' in df.columns and 'trade_date' in df.columns:
            df = df.sort_values(['ts_code', 'trade_date'])
            logger.info("已按股票和日期排序")

        return df

    @staticmethod
    def calculate_rights_adjustment(df: pd.DataFrame) -> pd.DataFrame:
        """
        统一计算复权价格（前复权QFQ和后复权HFQ）

        Args:
            df: 包含adj_factor列的DataFrame

        Returns:
            添加了复权价格列的DataFrame
        """
        logger.info("开始计算复权价格...")

        if 'adj_factor' not in df.columns or df['adj_factor'].isna().all():
            logger.warning("缺少复权因子，跳过复权计算")
            return df

        # 按股票分组处理
        result_dfs = []
        price_columns = ['open', 'high', 'low', 'close', 'pre_close', 'up_limit', 'down_limit']

        for ts_code, group in tqdm(df.groupby('ts_code'), desc="计算复权价格"):
            group = group.sort_values('trade_date').copy()

            if group['adj_factor'].isna().all():
                logger.warning(f"股票 {ts_code} 缺少复权因子，跳过计算")
                result_dfs.append(group)
                continue

            # 填充复权因子
            group['adj_factor'] = group['adj_factor'].ffill()

            # 计算前复权因子
            latest_adj = group['adj_factor'].iloc[-1]
            if latest_adj == 0:
                raise RuntimeError(f"<latest_adj==0> {ts_code} <UNK>")
            group['qfq_factor'] = group['adj_factor'] / latest_adj

            # 计算后复权因子
            first_adj = group['adj_factor'].iloc[0]
            if first_adj == 0:
                raise RuntimeError(f"<first_adj==0> {ts_code} <UNK>")
            group['hfq_factor'] = group['adj_factor'] / first_adj

            # 计算前复权价格
            for col in price_columns:
                if col in group.columns:
                    group[f'{col}_qfq'] = group[col] * group['qfq_factor']

            # 计算后复权价格
            for col in price_columns:
                if col in group.columns:
                    group[f'{col}_hfq'] = group[col] * group['hfq_factor']

            result_dfs.append(group)

        result_df = pd.concat(result_dfs, ignore_index=True)
        logger.info("复权价格计算完成")
        return result_df

    @staticmethod
    def split_and_save_by_stock(df: pd.DataFrame, output_dir: str) -> None:
        """
        按股票拆分并保存数据

        Args:
            df: 完整的DataFrame
            output_dir: 输出目录
        """
        logger.info("开始按股票拆分数据...")

        for ts_code, group in tqdm(df.groupby('ts_code'), desc="保存股票数据"):
            filename = f"{ts_code}.csv"
            filepath = os.path.join(output_dir, filename)
            group.to_csv(filepath, index=False)

        logger.info(f"已保存 {len(df['ts_code'].unique())} 只股票的数据到 {output_dir}")
