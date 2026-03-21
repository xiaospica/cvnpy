import tushare as ts
import pandas as pd
import numpy as np
import os
import time
from datetime import datetime, timedelta, date as Date
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional, Any
from loguru import logger

class TushareDataDownloaderEnhanced:
    """
    增强版Tushare数据下载器，支持所有数据类型
    """

    def __init__(self, token, data_dir='./stock_data_enhanced', max_workers=8):
        """
        初始化下载器
        :param token: Tushare Pro token
        :param data_dir: 数据保存目录
        :param max_workers: 最大并发线程数
        """
        self.token = token
        self.pro = ts.pro_api(token)
        self.data_dir = data_dir
        self.max_workers = max_workers
        self.max_retry = 3

        # 完整的数据类型配置 - 集中管理所有接口
        self.data_config = {
            'daily': {
                'method': 'daily',
                'desc': '日线数据',
                'params': {'trade_date': '{trade_date}'}
            },
            'daily_basic': {
                'method': 'daily_basic',
                'fields': 'ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,pe_ttm,pb,ps,dv_ratio,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv',
                'desc': '每日指标',
                'params': {'ts_code': '',
                            'trade_date': '{trade_date}'}
            },
            'adj_factor': {
                'method': 'adj_factor',
                'desc': '复权因子',
                'params': {'ts_code': '',
                            'trade_date': '{trade_date}'}
            },
            'bak_basic': { # 从20160101开始的数据
                'method': 'bak_basic',
                'fields': 'trade_date,ts_code,name,industry',
                'desc': '备用财务数据',
                'params': {'trade_date': '{trade_date}'}
            },
            'stock_st': { # 从20160101开始的数据
                'method': 'stock_st',
                'desc': 'ST状态',
                'params': {'trade_date': '{trade_date}'}
            },
            'stk_limit': {
                'method': 'stk_limit',
                'desc': '涨跌停价格',
                'params': {'trade_date': '{trade_date}'}
            },
            'suspend_d': {
                'method': 'suspend_d',
                'desc': '停复牌信息',
                'params': {'trade_date': '{trade_date}', 'suspend_type': 'S'}  # S=停牌
            }
        }

        # 创建数据目录
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(f"{data_dir}/raw", exist_ok=True)
        os.makedirs(f"{data_dir}/by_stock", exist_ok=True)

        # 设置Tushare
        ts.set_token(token)

    def _safe_api_call(self, method_name, params, description="", max_attempts=15):
        """
        安全的API调用，带重试机制
        """
        for attempt in range(max_attempts):
            try:
                logger.debug(f"调用API: {description}, 参数: {params}")
                method = getattr(self.pro, method_name)
                df = method(**params)
                if df is not None and not df.empty:
                    # 避免接口频率限制
                    time.sleep(0.05)  # 适当减少sleep时间
                    return df
                else:
                    logger.warning(f"API返回空数据: {description}")
                    time.sleep(0.05)
                    return pd.DataFrame()
            except Exception as e:
                logger.error(f"API调用失败 (尝试 {attempt+1}/{max_attempts}): {e}")
                if attempt < max_attempts - 1:
                    time.sleep(self.max_retry * (attempt + 1))
                else:
                    raise
        return pd.DataFrame()

    def _download_single_data_type(self, trade_date: str, data_type: str) -> pd.DataFrame:
        """
        下载单个数据类型
        """
        config = self.data_config[data_type]

        # 构建参数
        params = {}
        for key, value in config['params'].items():
            if isinstance(value, str) and '{trade_date}' in value:
                params[key] = trade_date
            else:
                params[key] = value

        # 添加fields参数（如果存在）
        if 'fields' in config:
            params['fields'] = config['fields']

        return self._safe_api_call(
            config['method'],
            params,
            f"{config['desc']} {trade_date}"
        )

    def download_all_data_by_trade_date(
        self,
        trade_dates: List[str],
        save_parquet: bool = True,
        raw_subdir: str = "raw",
    ) -> Dict[str, pd.DataFrame]:
        """
        按交易日批量下载所有数据
        :param trade_dates: 交易日列表
        :return: 字典格式结果 {'daily': df, 'daily_basic': df, ...}
        """
        logger.info(f"开始下载 {len(trade_dates)} 个交易日的数据")
        logger.info(f"数据类型: {', '.join(self.data_config.keys())}")

        # 初始化结果容器
        result_dfs = {data_type: [] for data_type in self.data_config.keys()}

        # 创建任务队列
        tasks = [(trade_date, data_type)
                for trade_date in trade_dates
                for data_type in self.data_config.keys()]

        logger.info(f"总任务数: {len(tasks)}")

        # 使用线程池并发下载
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_task = {
                executor.submit(self._download_single_data_type, trade_date, data_type): (trade_date, data_type)
                for trade_date, data_type in tasks
            }

            # 处理完成的任务
            completed_count = 0
            for future in tqdm(as_completed(future_to_task), total=len(tasks), desc="下载数据"):
                trade_date, data_type = future_to_task[future]
                completed_count += 1

                try:
                    df = future.result()
                    if not df.empty:
                        result_dfs[data_type].append(df)
                        logger.debug(f"成功下载 {data_type} {trade_date}: {len(df)}条记录")
                    else:
                        result_dfs[data_type].append(pd.DataFrame())
                        logger.debug(f"空数据 {data_type} {trade_date}")
                except Exception as e:
                    logger.error(f"下载失败 - 日期: {trade_date}, 类型: {data_type}, 错误: {e}")

                # 每完成100个任务记录进度
                if completed_count % 100 == 0:
                    logger.info(f"进度: {completed_count}/{len(tasks)} 个任务完成")

        # 合并所有DataFrame
        logger.info("合并数据...")
        final_results = {}

        for data_type, dfs in result_dfs.items():
            if dfs:
                merged_df = pd.concat(dfs, ignore_index=True)
                final_results[data_type] = merged_df

                if save_parquet:
                    output_dir = os.path.join(self.data_dir, raw_subdir)
                    os.makedirs(output_dir, exist_ok=True)
                    output_path = os.path.join(output_dir, f"{data_type}_all.parquet")
                    merged_df.to_parquet(output_path, index=False, compression='snappy')
                    logger.info(f"✅ 已保存 {data_type}: {len(merged_df)}条记录 -> {output_path}")
            else:
                final_results[data_type] = pd.DataFrame()
                logger.warning(f"⚠️  没有获取到 {data_type} 数据")

        logger.info("🎉 数据下载完成！")
        return final_results

    def load_all_parquet_data(self):

        final_results = {}

        for data_type, dfs in self.data_config.items():

            output_path = f"{self.data_dir}/raw/{data_type}_all.parquet"
            merged_df = pd.read_parquet(output_path)
            logger.info(f"✅ 读取 {data_type}: {len(merged_df)}条记录 -> {output_path}")
            final_results[data_type] = merged_df

        return final_results

    def is_trade_date(self, date: str | Date | datetime | None = None) -> bool:
        """
        判断指定日期是否为交易日
        date: datetime.date 或 'YYYYMMDD' 格式字符串，默认今天
        """
        if date is None:
            date_str = datetime.now().strftime('%Y%m%d')
        elif isinstance(date, datetime):
            date_str = date.strftime('%Y%m%d')
        elif isinstance(date, Date):
            date_str = date.strftime('%Y%m%d')
        else:
            date_str = str(date)

        params = {
            'exchange': 'SSE',
            'start_date': date_str,
            'end_date': date_str,
        }

        trade_cal_df = self._safe_api_call(
            'trade_cal',
            params,
            "获取所有交易日历"
        )

        if trade_cal_df.empty:
            return False

        return int(trade_cal_df.iloc[0]['is_open']) == 1

    def get_trade_calendars(self, start_date='20050101', end_date=None) -> List[str]:
        """
        获取交易日历
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        params = {
            'start_date': start_date,
            'end_date': end_date,
            'is_open': 1  # 只获取开市日期
        }

        trade_cal_df = self._safe_api_call(
            'trade_cal',
            params,
            "获取交易日历"
        )

        if not trade_cal_df.empty:
            trade_dates = trade_cal_df['cal_date'].tolist()
            logger.info(f"📅 获取到 {len(trade_dates)} 个交易日")
            return trade_dates
        else:
            logger.error("❌ 未获取到交易日历数据")
            return []

    def get_all_stocks_info(self) -> pd.DataFrame:
        """
        获取所有股票基本信息（包含所有上市状态）
        """
        # 定义所有上市状态：L上市 D退市 P暂停上市 G过会未交易
        list_statuses = ['L', 'D', 'P', 'G']
        all_stocks_dfs = []  # 存储所有状态的数据框

        for status in list_statuses:
            params = {
                'exchange': '',  # 不指定交易所，获取全部
                'list_status': status,  # 遍历每种状态
                'fields': 'ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type'
            }

            stocks_df = self._safe_api_call(
                'stock_basic',
                params,
                f"获取{status}状态股票列表"
            )

            if not stocks_df.empty:
                logger.info(f"✅ 获取到 {status} 状态股票 {len(stocks_df)} 只")
                all_stocks_dfs.append(stocks_df)
            else:
                logger.warning(f"⚠️  {status} 状态未获取到股票列表数据")

        # 合并所有状态的数据[7,8](@ref)
        if all_stocks_dfs:
            # 使用concat方法合并所有DataFrame[6,7](@ref)
            combined_df = pd.concat(all_stocks_dfs, ignore_index=True)

            # 转换日期格式
            combined_df['list_date'] = pd.to_datetime(combined_df['list_date'], format='%Y%m%d')
            if 'delist_date' in combined_df.columns and not combined_df['delist_date'].isna().all():
                combined_df['delist_date'] = pd.to_datetime(combined_df['delist_date'], format='%Y%m%d').dt.strftime('%Y%m%d')

            # 保存合并后的股票列表
            combined_df.to_parquet(f"{self.data_dir}/stock_list.parquet", index=False)
            logger.info(f"🏢 合并后共获取到 {len(combined_df)} 只股票（包含所有上市状态）")
            return combined_df
        else:
            logger.error("❌ 所有状态均未获取到股票列表数据")
            return pd.DataFrame()

    def get_index_weight(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """查询指数权重"""

        params = {
            'start_date': start_date,
            'end_date': end_date
        }

        stocks_df = self._safe_api_call(
            'index_weight',
            params,
            f"获取指数{index_code}股票池列表"
        )
        return stocks_df


class StockDataProcessorEnhanced:
    """
    增强版数据处理器，支持所有数据类型的合并和处理
    """

    @staticmethod
    def merge_all_data(data_dict: Dict[str, pd.DataFrame],
                      primary_key: List[str] = ['ts_code', 'trade_date']) -> pd.DataFrame:
        """
        通用数据合并函数，支持所有数据类型
        即使某些数据类型为空，也会创建对应列并填充NaN，保证最终DataFrame结构完整
        :param data_dict: 包含各种数据类型的字典
        :param primary_key: 合并主键
        :return: 合并后的DataFrame（包含所有配置列）
        """
        logger.info("🔄 开始合并所有数据...")

        # 检查基础数据
        if 'daily' not in data_dict or data_dict['daily'].empty:
            logger.error("❌ 缺少基础日线数据，无法合并")
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
        logger.info(f"📊 基础日线数据: {len(merged_df)}条记录")

        # 逐个合并其他数据
        for data_type in merge_order[1:]:
            # 检查数据类型是否存在（即使为空也处理）
            if data_type in data_dict:
                df_to_merge = data_dict[data_type].copy()

                # 如果DataFrame为空，创建只包含主键的空DataFrame
                if df_to_merge.empty:
                    logger.warning(f"⚠️  {data_type} 数据为空，将创建空列")
                    # 获取该数据类型应有的所有字段
                    expected_fields = data_type_fields.get(data_type, [])
                    if not expected_fields:
                        logger.warning(f"⚠️  无法获取 {data_type} 的字段定义，跳过")
                        continue

                    # 提取主键的唯一组合（基于已有的merged_df）
                    primary_keys_df = merged_df[primary_key].drop_duplicates()

                    # 创建包含所有应有字段的空DataFrame（值为NaN）
                    empty_cols = {col: np.nan for col in expected_fields if col not in primary_key}
                    df_to_merge = primary_keys_df.copy()
                    for col, val in empty_cols.items():
                        df_to_merge[col] = val

                    logger.info(f"   → 为 {data_type} 创建了包含 {len(df_to_merge.columns)} 列的空结构")
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
                    logger.info(f"🔗 合并 {data_type}: {len(df_to_merge)}条, 新增列: {', '.join(added_cols)} -> 合并后: {after_count}条")
                else:
                    logger.info(f"🔗 合并 {data_type}: 空数据，仅保留主键 -> 合并后: {after_count}条")
            else:
                logger.warning(f"⚠️  {data_type} 不存在于data_dict中，跳过")

        # # 数据清洗和后处理
        # merged_df = StockDataProcessorEnhanced._post_merge_processing(merged_df)

        logger.info(f"✅ 最终合并结果: {len(merged_df)}条记录, {len(merged_df.columns)}列")
        logger.info(f"📈 数据预览:\n{merged_df.head().to_string()}")

        return merged_df

    @staticmethod
    def _post_merge_processing(df: pd.DataFrame) -> pd.DataFrame:
        """
        合并后的数据清洗和处理
        """
        logger.info("🧹 开始数据清洗和后处理...")

        # # 1. 填充复权因子缺失值
        # if 'adj_factor' in df.columns:
        #     df['adj_factor'] = df.groupby('ts_code')['adj_factor'].ffill()
        #     logger.info("✅ 已填充复权因子缺失值")
        #
        # # 2. 处理停牌信息
        # if 'suspend_type' in df.columns:
        #     df['is_suspended'] = df['suspend_type'].notna()
        #     df['suspend_reason'] = df['suspend_reason'].fillna('正常交易')
        #     logger.info("✅ 已处理停牌信息")
        #
        # # 3. 处理ST状态
        # if 'is_st' in df.columns:
        #     df['is_st'] = df['is_st'].fillna(0).astype(int)
        #     logger.info("✅ 已处理ST状态")

        # 4. 确保数值列类型正确
        numeric_columns = ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount', 'turnover_rate', 'pe', 'pb']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 5. 按股票和日期排序
        if 'ts_code' in df.columns and 'trade_date' in df.columns:
            df = df.sort_values(['ts_code', 'trade_date'])
            logger.info("✅ 已按股票和日期排序")

        return df

    @staticmethod
    def calculate_rights_adjustment(df: pd.DataFrame) -> pd.DataFrame:
        """
        统一计算复权价格
        """
        logger.info("💰 开始计算复权价格...")

        if 'adj_factor' not in df.columns or df['adj_factor'].isna().all():
            logger.warning("⚠️  缺少复权因子，跳过复权计算")
            return df

        # 按股票分组处理
        result_dfs = []
        price_columns = ['open', 'high', 'low', 'close', 'pre_close', 'up_limit', 'down_limit']
        mv_columns = ['total_mv', 'circ_mv']

        # 使用groupby分组，避免内存溢出
        for ts_code, group in tqdm(df.groupby('ts_code'), desc="🧮 计算复权价格"):
            group = group.sort_values('trade_date').copy()

            if group['adj_factor'].isna().all():
                logger.warning(f"⚠️  股票 {ts_code} 缺少复权因子，跳过计算")
                result_dfs.append(group)
                continue

            # 填充复权因子
            group['adj_factor'] = group['adj_factor'].ffill()

            # 计算前复权因子
            latest_adj = group['adj_factor'].iloc[-1]
            if latest_adj == 0:
                # latest_adj = 1.0
                raise RuntimeError(f"<latest_adj==0> {ts_code} <UNK>")
            group['qfq_factor'] = group['adj_factor'] / latest_adj

            # 计算后复权因子
            first_adj = group['adj_factor'].iloc[0]
            if first_adj == 0:
                # first_adj = 1.0
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

            # # 计算复权市值
            # for col in mv_columns:
            #     if col in group.columns:
            #         group[f'{col}_qfq'] = group[col] * group['qfq_factor']
            #         group[f'{col}_hfq'] = group[col] * group['hfq_factor']

            result_dfs.append(group)

        result_df = pd.concat(result_dfs, ignore_index=True)
        logger.info("✅ 复权价格计算完成")
        return result_df

    @staticmethod
    def split_and_save_by_stock(df: pd.DataFrame, output_dir: str) -> None:
        """
        按股票拆分并保存数据，仅使用ts_code命名
        :param df: 完整的DataFrame
        :param output_dir: 输出目录
        """
        logger.info("📁 开始按股票拆分数据...")

        # 按股票分组
        for ts_code, group in tqdm(df.groupby('ts_code'), desc="💾 保存股票数据"):
            # # 直接使用ts_code作为文件名
            # filename = f"{ts_code}.parquet"
            # filepath = os.path.join(output_dir, filename)
            # # 保存文件
            # group.to_parquet(filepath, index=False, compression='snappy')

            filename = f"{ts_code}.csv"
            filepath = os.path.join(output_dir, filename)
            group.to_csv(filepath, index=False)

        logger.info(f"✅ 已保存 {len(df['ts_code'].unique())} 只股票的数据到 {output_dir}")


class DataPipelineEnhanced:
    """
    增强版数据处理管道
    """

    def __init__(self, downloader, processor):
        self.downloader = downloader
        self.processor = processor

    @staticmethod
    def _normalize_trade_date(df: pd.DataFrame) -> pd.DataFrame:
        if 'trade_date' not in df.columns:
            return df
        if np.issubdtype(df['trade_date'].dtype, np.datetime64):
            return df
        df = df.copy()
        df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str), format='%Y%m%d', errors='coerce')
        return df

    @staticmethod
    def _align_schema_like(reference_df: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
        ref_cols = list(reference_df.columns)
        aligned = df.copy()
        for col in ref_cols:
            if col not in aligned.columns:
                aligned[col] = pd.NA
        aligned = aligned[ref_cols]
        return aligned

    @staticmethod
    def _upsert_by_primary_key(existing_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        if existing_df.empty:
            return new_df.copy()
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        if 'ts_code' in combined.columns:
            combined['ts_code'] = combined['ts_code'].astype(str)
        if 'trade_date' in combined.columns:
            combined = DataPipelineEnhanced._normalize_trade_date(combined)
        combined = combined.drop_duplicates(subset=['ts_code', 'trade_date'], keep='last')
        combined = combined.sort_values(['ts_code', 'trade_date'])
        return combined

    def _load_stock_info(self) -> pd.DataFrame:
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
            logger.error(f"❌ 增量数据处理管道执行失败: {e}")
            raise

    def run_full_pipeline(self, start_date: str = '20050101', end_date: str = None):
        """
        运行完整数据处理管道
        """
        try:
            logger.info("🚀 开始运行完整数据处理管道...")

            # 1. 获取交易日
            logger.info("📅 获取交易日历...")
            trade_dates = self.downloader.get_trade_calendars(start_date, end_date)
            if not trade_dates:
                logger.error("❌ 未获取到交易日历")
                return None

            # 2. 下载所有数据
            logger.info("⬇️  下载所有数据...")
            # data_dict = self.downloader.download_all_data_by_trade_date(trade_dates)
            # return
            data_dict = self.downloader.load_all_parquet_data()

            # 3. 合并数据
            # logger.info("🔀 合并所有数据...")
            merged_df = self.processor.merge_all_data(data_dict)
            if merged_df.empty:
                logger.error("❌ 数据合并失败")
                return None

            # 5. 获取股票信息
            logger.info("🏢 获取股票信息...")
            stock_info_df = self.downloader.get_all_stocks_info()

            # # 6. 保存完整数据
            # logger.info("💾 保存完整数据...")
            # full_data_path = f"{self.downloader.data_dir}/full_data.parquet"
            # processed_df.to_parquet(full_data_path, index=False, compression='snappy')
            # logger.info(f"✅ 完整数据已保存: {len(processed_df)}条记录 -> {full_data_path}")

            # TODO 补充聚宽ST历史数据
            jq_st_data_path = os.path.join(self.downloader.data_dir, "st_data")
            jq_st_data = pd.read_csv(os.path.join(jq_st_data_path, 'jq_stock_st_data.csv'))
            jq_st_data.rename(columns={jq_st_data.columns[0]: 'trade_date'}, inplace=True)
            jq_st_data = jq_st_data.set_index(jq_st_data.columns[0])

            def jq2ts(code: str) -> str:
                """000001.XSHE -> 000001.SZ；688xxx.XSHG -> 688xxx.SH"""
                head, suffix = code.split('.')
                return head + ('.SZ' if suffix == 'XSHE' else '.SH')

            df_st = jq_st_data.rename(columns=jq2ts)

            # ------------------------------------------------------------------
            # 2. 把“日期×股票” 透视成 “股票×日期” 的长表
            # ------------------------------------------------------------------
            st_long = (df_st.stack()               # 变成 MultiIndex  Series
                         .reset_index()            # 成为三列：level_0(日期)  level_1(ts_code)  0
                         .rename(columns={'level_0': 'trade_date',
                                          'level_1': 'ts_code',
                                          0: 'is_st'}))

            # 确保日期格式一致
            st_long['trade_date'] = pd.to_datetime(st_long['trade_date']).dt.strftime('%Y%m%d')
            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'].astype(str)).dt.strftime('%Y%m%d')

            # ------------------------------------------------------------------
            # 3. 合并到主表
            # ------------------------------------------------------------------
            merged_df = merged_df.merge(st_long,
                                      on=['trade_date', 'ts_code'],
                                      how='left')   # 无 ST 信息默认 NaN，可再 .fillna(False)
            logger.info("🎉 聚宽ST数据补充完整！")

            # TODO 合并股票信息，补充上市到现在时间
            cols_to_merge = ['ts_code', 'list_date', 'delist_date']
            merged_df = merged_df.merge(stock_info_df[cols_to_merge], on='ts_code', how='left')
            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'], format='%Y%m%d')
            # 5. 自然日天数
            merged_df['days_since_list'] = (merged_df['trade_date'] - merged_df['list_date']).dt.days
            # 6. 交易日天数（每个股票内部按交易日排序后累加）
            merged_df = merged_df.sort_values(['ts_code','trade_date'])
            cal = self.downloader.get_trade_calendars('','')
            cal = pd.DataFrame({'cal_date': cal}).sort_values('cal_date')
            date_idx = pd.DatetimeIndex(cal['cal_date'])
            trd_df = trd_days_since_list(stock_info_df, date_idx)

            # 5. 并回主表（你的 merged_df）
            merged_df['trade_date'] = pd.to_datetime(merged_df['trade_date'], format='%Y%m%d')
            merged_df = merged_df.merge(trd_df, on=['ts_code','trade_date'], how='left')

            merged_df['list_date'] = pd.to_datetime(merged_df['list_date'])
            merged_df = merged_df[merged_df['trade_date'] >= merged_df['list_date']].copy()

            # TODO 计算复权因子
            logger.info("🧮 计算复权价格...")
            merged_df = self.processor.calculate_rights_adjustment(merged_df)

            # 7. 按股票拆分保存
            logger.info("📁 按股票拆分保存...")
            self.processor.split_and_save_by_stock(
                merged_df,
                f"{self.downloader.data_dir}/by_stock"
            )

            logger.info("🎉 完整数据处理管道执行成功！")
            return merged_df

        except Exception as e:
            logger.error(f"❌ 数据处理管道执行失败: {e}")
            raise

def trd_days_since_list(basic_df, date_idx):
    """
    返回长表：ts_code, trade_date, trd_days_since_list
    """
    res = []
    for _, row in basic_df.iterrows():
        ts_code   = row['ts_code']
        list_dt   = row['list_date']
        # 只保留 ≥ 上市日的交易日
        sub_dates = date_idx[date_idx >= list_dt]
        # 从 1 开始计数
        tmp = pd.DataFrame({'trade_date': sub_dates})
        tmp['ts_code'] = ts_code
        tmp['trd_days_since_list'] = range(1, len(tmp)+1)
        res.append(tmp)
    return pd.concat(res, ignore_index=True)

if __name__ == '__main__':
    
    # import json
    # with open(f'api.json', 'r', encoding='utf-8') as f:
    #     token = json.load(f)['token']

    # data_dir = './stock_data'
    # parquet_path = os.path.join(data_dir, 'df_all_stock.parquet')

    # old_df: pd.DataFrame | None = None
    # if os.path.exists(parquet_path):
    #     old_df = pd.read_parquet(parquet_path)

    # downloader = TushareDataDownloaderEnhanced(token, data_dir=data_dir, max_workers=4)
    # processor = StockDataProcessorEnhanced()
    # pipeline = DataPipelineEnhanced(downloader, processor)

    # end_date = datetime.now().strftime("%Y%m%d")
    # df_all = pipeline.run_incremental_pipeline(parquet_path=parquet_path, end_date=end_date, save_parquet=True)
    # if df_all is None or df_all.empty:
    #     raise RuntimeError("增量更新未产出数据")

    # if old_df is not None and not old_df.empty:
    #     if list(df_all.columns) != list(old_df.columns):
    #         raise RuntimeError("增量更新后的列顺序/字段与历史不一致")

    # dup_count = int(df_all.duplicated(subset=['ts_code', 'trade_date']).sum())
    # if dup_count != 0:
    #     raise RuntimeError(f"主键重复: {dup_count}")

    # if {'close', 'close_qfq', 'qfq_factor'}.issubset(df_all.columns):
    #     check_df = df_all.dropna(subset=['close', 'close_qfq', 'qfq_factor'])
    #     if len(check_df) > 0:
    #         n = min(20, len(check_df))
    #         sample = check_df.sample(n=n, random_state=1)
    #         expected = pd.to_numeric(sample['close'], errors='coerce') * pd.to_numeric(sample['qfq_factor'], errors='coerce')
    #         actual = pd.to_numeric(sample['close_qfq'], errors='coerce')
    #         rel_err = ((expected - actual).abs() / actual.abs().replace(0, np.nan)).dropna()
    #         if not rel_err.empty:
    #             logger.info(f"close_qfq 校验样本数: {len(rel_err)}, 最大相对误差: {rel_err.max():.6g}")

    # reloaded = pd.read_parquet(parquet_path)
    # if reloaded.empty:
    #     raise RuntimeError("parquet 覆盖写后读取为空")
    # logger.info(f"✅ 增量更新完成: {len(reloaded):,} 行, {len(reloaded.columns)} 列 -> {parquet_path}")


    import json
    import os

    import numpy as np
    import pandas as pd

    with open('api.json','r',encoding='utf-8') as f:
        token = json.load(f)['token']

    src = os.path.join('stock_data','df_all_stock.parquet')
    base_filters = [("trade_date", ">=", pd.Timestamp('2026-01-01'))]

    df_base = pd.read_parquet(src, filters=base_filters)
    print('BASE rows', len(df_base), 'cols', len(df_base.columns))
    print('BASE min', df_base['trade_date'].min(), 'max', df_base['trade_date'].max())

    parquet_path = os.path.join('stock_data','df_all_stock_test.parquet')
    df_base.to_parquet(parquet_path, index=False)
    old_cols = list(df_base.columns)

    print('TEST parquet written:', parquet_path)

    downloader = TushareDataDownloaderEnhanced(token, data_dir='./stock_data', max_workers=4)
    processor = StockDataProcessorEnhanced()
    pipeline = DataPipelineEnhanced(downloader, processor)

    end_date = '20260204'
    df_all = pipeline.run_incremental_pipeline(parquet_path=parquet_path, end_date=end_date, save_parquet=True)

    assert df_all is not None and not df_all.empty, 'incremental pipeline returned empty'

    if list(df_all.columns) != old_cols:
        raise RuntimeError('columns mismatch after incremental update')

    dup_count = int(df_all.duplicated(subset=['ts_code','trade_date']).sum())
    if dup_count != 0:
        raise RuntimeError(f'primary key duplicates: {dup_count}')

    check_cols = {'close','close_qfq','qfq_factor'}
    if check_cols.issubset(df_all.columns):
        check_df = df_all.dropna(subset=list(check_cols))
        if len(check_df) > 0:
            n = min(50, len(check_df))
            sample = check_df.sample(n=n, random_state=1)
            expected = pd.to_numeric(sample['close'], errors='coerce') * pd.to_numeric(sample['qfq_factor'], errors='coerce')
            actual = pd.to_numeric(sample['close_qfq'], errors='coerce')
            diff = (expected - actual).abs()
            rel = (diff / actual.abs().replace(0, np.nan)).dropna()
            print('QFQ check samples', len(rel), 'max_rel_err', float(rel.max()) if not rel.empty else None)

    reloaded = pd.read_parquet(parquet_path)
    assert not reloaded.empty, 'reloaded parquet is empty'
    assert list(reloaded.columns) == old_cols, 'reloaded columns mismatch'

    print('PASS incremental update test')
    print('RESULT rows', len(reloaded), 'cols', len(reloaded.columns))
    print('RESULT min', reloaded['trade_date'].min(), 'max', reloaded['trade_date'].max())


    # 测试判断交易日历
    print(downloader.is_trade_date())  # 判断今天
    print(downloader.is_trade_date('20260320'))  # 判断指定日期

    from vnpy_tushare_pro.scheduler import DailyTimeTaskScheduler

    scheduler = DailyTimeTaskScheduler()
    scheduler.register_daily_job(
        name="test_job",
        time_str="00:00",
        job_func=lambda: print("test_job_ok"),
        is_trade_date_func=lambda _d: True,
    )
    scheduler.start()
    scheduler.run_job_now("test_job")
    scheduler.update_job_time("test_job", "00:01")
    scheduler.stop()
