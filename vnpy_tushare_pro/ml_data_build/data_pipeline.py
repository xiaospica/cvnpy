"""
数据处理管道模块

提供全量和增量数据处理管道，编排数据下载、合并、复权计算等流程：
- 全量管道：从零开始下载并处理所有历史数据
- 增量管道：基于已有数据追加更新
- ST数据合并、上市信息补充、复权因子增量更新
"""

import pandas as pd
import numpy as np
import os
from datetime import datetime
from typing import Optional
from loguru import logger

from .api_client import TushareApiClient
from .data_processor import StockDataProcessor


class DataPipeline:
    """
    数据处理管道

    编排数据下载、合并、复权计算等完整流程，支持全量和增量两种模式。

    Args:
        downloader: TushareApiClient 实例
        processor: StockDataProcessor 实例
    """

    def __init__(self, downloader: TushareApiClient, processor: StockDataProcessor):
        self.downloader = downloader
        self.processor = processor

    @staticmethod
    def _normalize_trade_date(df: pd.DataFrame) -> pd.DataFrame:
        """将 trade_date 列统一为 datetime64 格式"""
        if 'trade_date' not in df.columns:
            return df
        if np.issubdtype(df['trade_date'].dtype, np.datetime64):
            return df
        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
        return df

    @staticmethod
    def _align_schema_like(reference_df: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
        """将 df 的列对齐到 reference_df 的列结构"""
        ref_cols = list(reference_df.columns)
        aligned = df.copy()
        for col in ref_cols:
            if col not in aligned.columns:
                aligned[col] = pd.NA
        aligned = aligned[ref_cols]
        return aligned

    @staticmethod
    def _upsert_by_primary_key(existing_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        """基于主键 (ts_code, trade_date) 合并数据，新数据覆盖旧数据"""
        if existing_df.empty:
            return new_df.copy()
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        if 'ts_code' in combined.columns:
            combined['ts_code'] = combined['ts_code'].astype(str)
        if 'trade_date' in combined.columns:
            combined = DataPipeline._normalize_trade_date(combined)
        combined = combined.drop_duplicates(subset=['ts_code', 'trade_date'], keep='last')
        combined = combined.sort_values(['ts_code', 'trade_date'])
        return combined

    def _load_stock_info(self) -> pd.DataFrame:
        """加载股票基本信息（优先从本地 parquet，否则从 API 获取）"""
        stock_list_path = os.path.join(self.downloader.data_dir, "stock_list.parquet")
        if os.path.exists(stock_list_path):
            df = pd.read_parquet(stock_list_path)
            if 'list_date' in df.columns and not np.issubdtype(df['list_date'].dtype, np.datetime64):
                df['list_date'] = pd.to_datetime(df['list_date'], errors='coerce')
            if 'delist_date' in df.columns and np.issubdtype(df['delist_date'].dtype, np.datetime64):
                df['delist_date'] = df['delist_date'].dt.strftime('%Y%m%d')
            if 'ts_code' in df.columns:
                df = df.drop_duplicates(subset=['ts_code'], keep='last')
            return df

        df = self.downloader.get_all_stocks_info()
        if not df.empty and 'ts_code' in df.columns:
            df = df.drop_duplicates(subset=['ts_code'], keep='last')
        return df

    def _apply_jq_st_data(self, merged_df: pd.DataFrame) -> pd.DataFrame:
        """
        合并聚宽 ST 历史数据

        从 st_data/jq_stock_st_data.csv 读取 ST 状态宽表，
        转换为长表后合并到主 DataFrame。
        """
        if merged_df.empty:
            return merged_df

        jq_st_data_path = os.path.join(self.downloader.data_dir, "st_data", "jq_stock_st_data.csv")
        if not os.path.exists(jq_st_data_path):
            if 'is_st' not in merged_df.columns:
                merged_df = merged_df.copy()
                merged_df['is_st'] = pd.NA
            return merged_df

        jq_st_data = pd.read_csv(jq_st_data_path)
        jq_st_data.rename(columns={jq_st_data.columns[0]: 'trade_date'}, inplace=True)
        jq_st_data = jq_st_data.set_index(jq_st_data.columns[0])

        def jq2ts(code: str) -> str:
            head, suffix = code.split('.')
            return head + ('.SZ' if suffix == 'XSHE' else '.SH')

        df_st = jq_st_data.rename(columns=jq2ts)
        st_long = (
            df_st.stack()
            .reset_index()
            .rename(columns={'level_0': 'trade_date', 'level_1': 'ts_code', 0: 'is_st'})
        )
        st_long['trade_date'] = pd.to_datetime(st_long['trade_date'], errors='coerce').dt.strftime('%Y%m%d')

        df = merged_df.copy()
        df['trade_date'] = df['trade_date'].astype(str)
        df = df.merge(st_long, on=['trade_date', 'ts_code'], how='left')
        return df

    def _apply_list_info_and_days(
        self,
        merged_df: pd.DataFrame,
        stock_info_df: pd.DataFrame,
        calendar_trade_dates: list[str],
    ) -> pd.DataFrame:
        """
        补充上市信息和上市天数

        添加 list_date, delist_date, days_since_list, trd_days_since_list 列，
        并过滤掉上市前的数据。

        Args:
            merged_df: 主数据DataFrame
            stock_info_df: 股票基本信息DataFrame
            calendar_trade_dates: 交易日历列表
        """
        if merged_df.empty:
            return merged_df

        if stock_info_df.empty or 'ts_code' not in stock_info_df.columns or 'list_date' not in stock_info_df.columns:
            df = merged_df.copy()
            df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
            if 'days_since_list' not in df.columns:
                df['days_since_list'] = pd.NA
            if 'trd_days_since_list' not in df.columns:
                df['trd_days_since_list'] = pd.NA
            return df

        cols_to_merge = [c for c in ['ts_code', 'list_date', 'delist_date'] if c in stock_info_df.columns]
        df = merged_df.merge(stock_info_df[cols_to_merge], on='ts_code', how='left')

        df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
        if 'list_date' in df.columns:
            df['list_date'] = pd.to_datetime(df['list_date'], errors='coerce')
        if 'days_since_list' not in df.columns and 'list_date' in df.columns:
            df['days_since_list'] = (df['trade_date'] - df['list_date']).dt.days

        cal_dt = pd.to_datetime(pd.Series(calendar_trade_dates, dtype='string'), format='%Y%m%d', errors='coerce')
        cal_dt = cal_dt.dropna().sort_values()
        cal_index = pd.Index(cal_dt)
        date_pos_map = pd.Series(np.arange(len(cal_index), dtype=np.int64), index=cal_index)

        if 'trd_days_since_list' not in df.columns and 'list_date' in df.columns:
            stock_list_date = stock_info_df[['ts_code', 'list_date']].drop_duplicates(subset=['ts_code'], keep='last').set_index('ts_code')['list_date']
            list_pos = pd.Series(
                np.searchsorted(cal_index.values, stock_list_date.values, side='left'),
                index=stock_list_date.index,
                dtype='int64'
            )
            df_date_pos = df['trade_date'].map(date_pos_map)
            df_list_pos = df['ts_code'].map(list_pos)
            df['trd_days_since_list'] = (df_date_pos - df_list_pos + 1).where(df_date_pos.notna() & df_list_pos.notna())

        if 'list_date' in df.columns:
            df = df[df['trade_date'] >= df['list_date']].copy()

        return df

    def _build_wide_df_for_trade_dates(self, trade_dates: list[str]) -> pd.DataFrame:
        """下载并合并指定交易日的所有数据"""
        if not trade_dates:
            return pd.DataFrame()
        data_dict = self.downloader.download_all_data_by_trade_date(trade_dates, save_parquet=False)
        merged_df = self.processor.merge_all_data(data_dict)
        return merged_df

    def _update_rights_adjustment_incremental(
        self,
        existing_df: pd.DataFrame,
        new_df: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        增量更新复权因子

        当新数据的 adj_factor 改变了整体复权基准时，
        需要重新计算旧数据的前复权价格。

        Args:
            existing_df: 已有数据
            new_df: 新下载的数据

        Returns:
            (更新后的旧数据, 计算了复权的新数据)
        """
        if existing_df.empty or new_df.empty:
            return existing_df, new_df
        if 'adj_factor' not in existing_df.columns or 'adj_factor' not in new_df.columns:
            return existing_df, new_df

        ex = existing_df.copy()
        nw = new_df.copy()
        ex = self._normalize_trade_date(ex)
        nw = self._normalize_trade_date(nw)

        ex = ex.sort_values(['ts_code', 'trade_date'])
        nw = nw.sort_values(['ts_code', 'trade_date'])

        ex_adj = ex.dropna(subset=['adj_factor'])
        nw_adj = nw.dropna(subset=['adj_factor'])
        if ex_adj.empty or nw_adj.empty:
            return ex, nw

        old_latest_adj = ex_adj.groupby('ts_code')['adj_factor'].last()
        combined_adj = pd.concat(
            [ex_adj[['ts_code', 'trade_date', 'adj_factor']], nw_adj[['ts_code', 'trade_date', 'adj_factor']]],
            ignore_index=True
        ).sort_values(['ts_code', 'trade_date'])
        new_latest_adj = combined_adj.groupby('ts_code')['adj_factor'].last()
        scale = (old_latest_adj / new_latest_adj).replace([np.inf, -np.inf], np.nan)

        qfq_cols = []
        if 'qfq_factor' in ex.columns:
            qfq_cols.append('qfq_factor')
        qfq_cols.extend([c for c in ex.columns if c.endswith('_qfq')])

        if qfq_cols:
            ts_scale = ex['ts_code'].map(scale).fillna(1.0)
            for col in qfq_cols:
                if col in ex.columns:
                    ex[col] = pd.to_numeric(ex[col], errors='coerce') * ts_scale

        first_adj = ex_adj.groupby('ts_code')['adj_factor'].first()
        first_adj = first_adj.reindex(new_latest_adj.index)

        ts_latest = nw['ts_code'].map(new_latest_adj)
        ts_first = nw['ts_code'].map(first_adj)
        nw['qfq_factor'] = nw['adj_factor'] / ts_latest
        nw['hfq_factor'] = nw['adj_factor'] / ts_first

        price_columns = ['open', 'high', 'low', 'close', 'pre_close', 'up_limit', 'down_limit']
        for col in price_columns:
            if col in nw.columns:
                nw[f'{col}_qfq'] = nw[col] * nw['qfq_factor']
                nw[f'{col}_hfq'] = nw[col] * nw['hfq_factor']

        ex_first_date = ex.groupby('ts_code')['trade_date'].min()
        nw_first_date = nw.groupby('ts_code')['trade_date'].min()
        affected = nw_first_date[nw_first_date < ex_first_date.reindex(nw_first_date.index)]
        affected_codes = affected.index.dropna().tolist()
        if affected_codes:
            combined = pd.concat([ex, nw], ignore_index=True).sort_values(['ts_code', 'trade_date'])
            keep = combined[~combined['ts_code'].isin(affected_codes)].copy()
            subset = combined[combined['ts_code'].isin(affected_codes)].copy()
            subset = self.processor.calculate_rights_adjustment(subset)
            combined = pd.concat([keep, subset], ignore_index=True).sort_values(['ts_code', 'trade_date'])

            new_keys = pd.MultiIndex.from_frame(nw[['ts_code', 'trade_date']])
            combined_keys = pd.MultiIndex.from_frame(combined[['ts_code', 'trade_date']])
            is_new = combined_keys.isin(new_keys)
            nw = combined[is_new].copy()
            ex = combined[~is_new].copy()

        return ex, nw

    def run_incremental_pipeline(
        self,
        parquet_path: str,
        start_date: str | None = None,
        end_date: str | None = None,
        save_parquet: bool = True,
    ) -> pd.DataFrame | None:
        """
        运行增量数据处理管道

        基于已有 parquet 文件，仅下载新数据并合并。

        Args:
            parquet_path: 已有数据的parquet文件路径
            start_date: 起始日期（默认从已有数据的最后一天+1开始）
            end_date: 结束日期（默认今天）
            save_parquet: 是否保存结果到parquet

        Returns:
            更新后的完整DataFrame，失败返回None
        """
        try:
            end_date = end_date or datetime.now().strftime("%Y%m%d")

            existing_df = pd.DataFrame()
            if os.path.exists(parquet_path):
                existing_df = pd.read_parquet(parquet_path)
                existing_df = self._normalize_trade_date(existing_df)

            if not existing_df.empty and start_date is None:
                max_dt = existing_df['trade_date'].max()
                if pd.isna(max_dt):
                    start_date = "20050101"
                else:
                    start_date = (pd.Timestamp(max_dt) + pd.Timedelta(days=1)).strftime("%Y%m%d")

            logger.info(f'data update from start_date: {start_date}')
            start_date = start_date or "20050101"
            trade_dates = self.downloader.get_trade_calendars(start_date, end_date)
            if not trade_dates:
                return existing_df if not existing_df.empty else None

            merged_df = self._build_wide_df_for_trade_dates(trade_dates)
            if merged_df.empty:
                return existing_df if not existing_df.empty else None

            stock_info_df = self._load_stock_info()
            merged_df = self._apply_jq_st_data(merged_df)
            full_calendar = self.downloader.get_trade_calendars("20050101", end_date)
            merged_df = self._apply_list_info_and_days(merged_df, stock_info_df, full_calendar)

            if not existing_df.empty:
                existing_df, merged_df = self._update_rights_adjustment_incremental(existing_df, merged_df)
                merged_df = self._align_schema_like(existing_df, merged_df)
                existing_df = self._align_schema_like(existing_df, existing_df)
                result_df = self._upsert_by_primary_key(existing_df, merged_df)
                result_df = self._align_schema_like(existing_df, result_df)
            else:
                merged_df = self.processor.calculate_rights_adjustment(merged_df)
                result_df = merged_df

            if save_parquet:
                os.makedirs(os.path.dirname(parquet_path) or ".", exist_ok=True)
                result_df.to_parquet(parquet_path, index=False)

            return result_df
        except Exception as e:
            logger.error(f"增量数据处理管道执行失败: {e}")
            raise

    def run_full_pipeline(self, start_date: str = '20050101', end_date: str | None = None):
        """
        运行完整数据处理管道

        从零开始下载所有历史数据，合并、补充ST信息、计算复权、按股票拆分保存。

        Args:
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            处理完成的完整DataFrame，失败返回None
        """
        try:
            logger.info("开始运行完整数据处理管道...")

            # 1. 获取交易日
            logger.info("获取交易日历...")
            trade_dates = self.downloader.get_trade_calendars(start_date, end_date)
            if not trade_dates:
                logger.error("未获取到交易日历")
                return None

            # 2. 下载所有数据
            logger.info("下载所有数据...")
            # data_dict = self.downloader.download_all_data_by_trade_date(trade_dates)
            # return
            data_dict = self.downloader.load_all_parquet_data()

            # 3. 合并数据
            merged_df = self.processor.merge_all_data(data_dict)
            if merged_df.empty:
                logger.error("数据合并失败")
                return None

            # 4. 获取股票信息
            logger.info("获取股票信息...")
            stock_info_df = self.downloader.get_all_stocks_info()

            # 5. 补充聚宽ST历史数据
            jq_st_data_path = os.path.join(self.downloader.data_dir, "st_data")
            jq_st_data = pd.read_csv(os.path.join(jq_st_data_path, 'jq_stock_st_data.csv'))
            jq_st_data.rename(columns={jq_st_data.columns[0]: 'trade_date'}, inplace=True)
            jq_st_data = jq_st_data.set_index(jq_st_data.columns[0])

            def jq2ts(code: str) -> str:
                """000001.XSHE -> 000001.SZ；688xxx.XSHG -> 688xxx.SH"""
                head, suffix = code.split('.')
                return head + ('.SZ' if suffix == 'XSHE' else '.SH')

            df_st = jq_st_data.rename(columns=jq2ts)

            st_long = (df_st.stack()
                         .reset_index()
                         .rename(columns={'level_0': 'trade_date',
                                          'level_1': 'ts_code',
                                          0: 'is_st'}))

            st_long['trade_date'] = pd.to_datetime(st_long['trade_date']).dt.strftime('%Y%m%d')
            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'].astype(str)).dt.strftime('%Y%m%d')

            merged_df = merged_df.merge(st_long,
                                      on=['trade_date', 'ts_code'],
                                      how='left')
            logger.info("聚宽ST数据补充完整！")

            # 6. 合并股票信息，补充上市到现在时间
            cols_to_merge = ['ts_code', 'list_date', 'delist_date']
            merged_df = merged_df.merge(stock_info_df[cols_to_merge], on='ts_code', how='left')
            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'], format='%Y%m%d')
            merged_df['days_since_list'] = (merged_df['trade_date'] - merged_df['list_date']).dt.days
            merged_df = merged_df.sort_values(['ts_code', 'trade_date'])
            cal = self.downloader.get_trade_calendars('', '')
            cal = pd.DataFrame({'cal_date': cal}).sort_values('cal_date')
            date_idx = pd.DatetimeIndex(cal['cal_date'])
            trd_df = trd_days_since_list(stock_info_df, date_idx)

            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'], format='%Y%m%d')
            merged_df = merged_df.merge(trd_df, on=['ts_code', 'trade_date'], how='left')

            merged_df['list_date'] = pd.to_datetime(merged_df['list_date'])
            merged_df = merged_df[merged_df['trade_date'] >= merged_df['list_date']].copy()

            # 7. 计算复权因子
            logger.info("计算复权价格...")
            merged_df = self.processor.calculate_rights_adjustment(merged_df)

            # 8. 按股票拆分保存
            logger.info("按股票拆分保存...")
            self.processor.split_and_save_by_stock(
                merged_df,
                f"{self.downloader.data_dir}/by_stock"
            )

            logger.info("完整数据处理管道执行成功！")
            return merged_df

        except Exception as e:
            logger.error(f"数据处理管道执行失败: {e}")
            raise


def trd_days_since_list(basic_df: pd.DataFrame, date_idx: pd.DatetimeIndex) -> pd.DataFrame:
    """
    计算每只股票自上市以来的交易日天数

    Args:
        basic_df: 股票基本信息DataFrame（需包含 ts_code, list_date 列）
        date_idx: 交易日历 DatetimeIndex

    Returns:
        长表DataFrame，包含 ts_code, trade_date, trd_days_since_list 列
    """
    res = []
    for _, row in basic_df.iterrows():
        ts_code = row['ts_code']
        list_dt = row['list_date']
        # 只保留 >= 上市日的交易日
        sub_dates = date_idx[date_idx >= list_dt]
        # 从 1 开始计数
        tmp = pd.DataFrame({'trade_date': sub_dates})
        tmp['ts_code'] = ts_code
        tmp['trd_days_since_list'] = range(1, len(tmp) + 1)
        res.append(tmp)
    return pd.concat(res, ignore_index=True)
