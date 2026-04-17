"""
Tushare API 客户端模块

提供 Tushare Pro API 的封装，包括：
- 带重试机制的安全 API 调用
- 按交易日并行批量下载数据
- 交易日历查询
- 股票基本信息查询
- 指数权重查询
"""

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
import threading
import weakref


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """守护线程池执行器，线程随主线程退出自动终止"""

    def _adjust_thread_count(self):
        def weakref_cb(_, q=self._work_queue):
            try:
                q.put(None)
            except Exception:
                pass

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"

            # 动态构建参数（兼容所有版本）
            args = [weakref.ref(self, weakref_cb), self._work_queue]

            # Python 3.11+ 需要额外参数
            if hasattr(self, '_initializer'):
                args.extend([self._initializer, self._initargs])

            t = threading.Thread(
                target=self._work_queue._worker if hasattr(self._work_queue, '_worker')
                      else __import__('concurrent.futures.thread', fromlist=['_worker'])._worker,
                args=tuple(args),
                name=thread_name,
                daemon=True
            )
            t.start()
            self._threads.add(t)
            weakref.ref(t, self._threads.remove)


class TushareApiClient:
    """
    Tushare Pro API 客户端

    封装数据下载、交易日历查询、股票信息查询等功能，
    支持并发下载和自动重试。

    Args:
        token: Tushare Pro API token
        data_dir: 数据保存目录
        max_workers: 最大并发线程数
    """

    def __init__(self, token: str, data_dir: str = './stock_data_enhanced', max_workers: int = 8):
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
            'bak_basic': {  # 从20160101开始的数据
                'method': 'bak_basic',
                'fields': 'trade_date,ts_code,name,industry',
                'desc': '备用财务数据',
                'params': {'trade_date': '{trade_date}'}
            },
            'stock_st': {  # 从20160101开始的数据
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

    def _safe_api_call(self, method_name: str, params: dict, description: str = "", max_attempts: int = 15) -> pd.DataFrame:
        """
        安全的API调用，带重试机制

        Args:
            method_name: API方法名
            params: 调用参数
            description: 调用描述（用于日志）
            max_attempts: 最大重试次数

        Returns:
            API返回的DataFrame，失败时返回空DataFrame
        """
        for attempt in range(max_attempts):
            try:
                logger.debug(f"调用API: {description}, 参数: {params}")
                method = getattr(self.pro, method_name)
                df = method(**params)
                if df is not None and not df.empty:
                    # 避免接口频率限制
                    time.sleep(0.05)
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

        Args:
            trade_date: 交易日期（YYYYMMDD格式）
            data_type: 数据类型名称

        Returns:
            下载的数据DataFrame
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

        Args:
            trade_dates: 交易日列表
            save_parquet: 是否保存为parquet文件
            raw_subdir: 原始数据子目录名

        Returns:
            字典格式结果 {'daily': df, 'daily_basic': df, ...}
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
        with DaemonThreadPoolExecutor(max_workers=self.max_workers) as executor:
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
                    logger.info(f"已保存 {data_type}: {len(merged_df)}条记录 -> {output_path}")
            else:
                final_results[data_type] = pd.DataFrame()
                logger.warning(f"没有获取到 {data_type} 数据")

        logger.info("数据下载完成！")
        return final_results

    def load_all_parquet_data(self) -> Dict[str, pd.DataFrame]:
        """
        加载已下载的所有 parquet 原始数据

        Returns:
            字典格式结果 {'daily': df, 'daily_basic': df, ...}
        """
        final_results = {}

        for data_type, dfs in self.data_config.items():
            output_path = f"{self.data_dir}/raw/{data_type}_all.parquet"
            merged_df = pd.read_parquet(output_path)
            logger.info(f"读取 {data_type}: {len(merged_df)}条记录 -> {output_path}")
            final_results[data_type] = merged_df

        return final_results

    def is_trade_date(self, date: str | Date | datetime | None = None) -> bool:
        """
        判断指定日期是否为交易日

        Args:
            date: datetime.date 或 'YYYYMMDD' 格式字符串，默认今天

        Returns:
            是否为交易日
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

    def get_trade_calendars(self, start_date: str = '20050101', end_date: str | None = None) -> List[str]:
        """
        获取交易日历

        Args:
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD），默认今天

        Returns:
            交易日列表
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
            logger.info(f"获取到 {len(trade_dates)} 个交易日")
            return trade_dates
        else:
            logger.error("未获取到交易日历数据")
            return []

    def get_all_stocks_info(self) -> pd.DataFrame:
        """
        获取所有股票基本信息（包含所有上市状态：L上市/D退市/P暂停/G过会未交易）

        Returns:
            股票基本信息DataFrame
        """
        list_statuses = ['L', 'D', 'P', 'G']
        all_stocks_dfs = []

        for status in list_statuses:
            params = {
                'exchange': '',
                'list_status': status,
                'fields': 'ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type'
            }

            stocks_df = self._safe_api_call(
                'stock_basic',
                params,
                f"获取{status}状态股票列表"
            )

            if not stocks_df.empty:
                logger.info(f"获取到 {status} 状态股票 {len(stocks_df)} 只")
                all_stocks_dfs.append(stocks_df)
            else:
                logger.warning(f"{status} 状态未获取到股票列表数据")

        if all_stocks_dfs:
            combined_df = pd.concat(all_stocks_dfs, ignore_index=True)
            combined_df['list_date'] = pd.to_datetime(combined_df['list_date'], format='%Y%m%d')
            if 'delist_date' in combined_df.columns and not combined_df['delist_date'].isna().all():
                combined_df['delist_date'] = pd.to_datetime(combined_df['delist_date'], format='%Y%m%d').dt.strftime('%Y%m%d')

            combined_df.to_parquet(f"{self.data_dir}/stock_list.parquet", index=False)
            logger.info(f"合并后共获取到 {len(combined_df)} 只股票（包含所有上市状态）")
            return combined_df
        else:
            logger.error("所有状态均未获取到股票列表数据")
            return pd.DataFrame()

    def get_index_weight(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
        """
        查询指数权重

        Args:
            index_code: 指数代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            指数权重DataFrame
        """
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
