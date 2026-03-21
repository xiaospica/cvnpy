# vnpy\_tushare\_pro 增量日线数据下载与合并方案（Plan）

## 目标

* 在 `vnpy_tushare_pro/utils.py` 新增“增量下载接口”，用于补齐 `df_all_stock.parquet` 的最新交易日数据。

* 通过该接口下载得到的增量 DataFrame：

  * 字段集合与现有 `DataPipelineEnhanced.run_full_pipeline()` 产出的宽表一致

  * 字段类型/关键字段格式一致（尤其 `ts_code`、`trade_date`）

  * 可以与已下载历史数据进行 upsert 合并（按 `ts_code + trade_date` 去重覆盖），最终保存回同一份 parquet。

* 在 `vnpy_tushare_pro/tushare_datafeed.py` 中提供一个调用入口（可选：在 `TushareDatafeedPro` 增加 `update_all_stock_history()`），使数据服务端可以一键执行增量更新。

## 现状梳理（基于现有实现）

* `TushareDatafeedPro.query_all_stock_history()` 通过 `pipeline.run_full_pipeline(start_date, end_date)` 生成 `df_all_stock` 并覆盖写入 `df_all_stock.parquet`：[tushare\_datafeed.py](file:///f:/Quant/vnpy/vnpy_strategy_dev/vnpy_tushare_pro/tushare_datafeed.py#L265-L295)

* `utils.py` 已具备三块关键能力：

  * 按交易日批量下载多接口数据：`TushareDataDownloaderEnhanced.download_all_data_by_trade_date()`：[utils.py](file:///f:/Quant/vnpy/vnpy_strategy_dev/vnpy_tushare_pro/utils.py#L130-L196)

  * 按 `['ts_code','trade_date']` 主键合并成宽表：`StockDataProcessorEnhanced.merge_all_data()`：[utils.py](file:///f:/Quant/vnpy/vnpy_strategy_dev/vnpy_tushare_pro/utils.py#L305-L404)

  * 管道化处理（补充 ST/上市信息/复权并拆分落地）：`DataPipelineEnhanced.run_full_pipeline()`：[utils.py](file:///f:/Quant/vnpy/vnpy_strategy_dev/vnpy_tushare_pro/utils.py#L539-L646)

## 设计原则

* “增量接口”只负责新增交易日的下载、加工、与历史宽表合并；不强行改变你现在的全量管道使用方式。

* 以历史宽表 `df_all_stock.parquet` 的 schema 作为权威 schema（列集合与列顺序），增量结果在合并前做对齐（补齐缺失列、统一 dtype、统一 `trade_date` 类型）。

* 复权列（`*_qfq`/`*_hfq`/`qfq_factor`/`hfq_factor`）需要与“全量跑一遍”的口径一致；增量更新时优先采用“缩放更新 + 新行计算”的方式，保证性能与一致性。

## 计划新增/调整的 API（utils.py）

### 1) 增量更新入口（新增）

* `DataPipelineEnhanced.run_incremental_pipeline(...) -> pd.DataFrame`

  * 输入（拟定）：

    * `parquet_path: str`：历史宽表路径（默认与 `tushare_datafeed.py` 保持一致，例如 `"{DATA_DIR}/df_all_stock.parquet"`）

    * `end_date: str | None`：增量更新截止日（默认今天，格式 `YYYYMMDD`）

    * `start_date: str | None`：可选强制起始日；不传则自动从历史最大 `trade_date` 推导下一交易日

    * `download_raw: bool = True`：是否走 API 下载 raw（默认 True），保留未来复用“离线 raw 复跑”的空间

  * 输出：

    * 返回合并后的全量宽表 DataFrame（并覆盖写回 parquet）

### 2) 交易日推导（新增内部函数）

* `_get_incremental_trade_dates(existing_df, start_date, end_date) -> list[str]`

  * 若 `existing_df` 非空，取 `max(trade_date)` 之后的开市日作为增量集合

  * 若为空，退化为 `run_full_pipeline(start_date, end_date)`（首跑逻辑）

### 3) 宽表 schema 对齐（新增内部函数）

* `_align_schema_like(reference_df, df) -> pd.DataFrame`

  * 缺失列补 NaN

  * 多余列按需保留（默认保留；最终写 parquet 前按 reference 列顺序输出）

  * 关键列统一 dtype：

    * `ts_code` -> `str`

    * `trade_date` -> `datetime64[ns]`（与现管道一致）

### 4) 增量复权更新（新增/增强）

在 `StockDataProcessorEnhanced` 新增一个增量复权函数（或在 `DataPipelineEnhanced` 内部实现）：

* `update_rights_adjustment_incremental(existing_df, new_df) -> tuple[pd.DataFrame, bool]`

  * 常规场景（new\_df 仅包含历史最大 `trade_date` 之后的日期）：

    * 对 existing\_df：仅根据“每只股票 latest\_adj 从旧到新变化比例”缩放已有 `qfq_factor` 和所有 `*_qfq` 列

    * 对 new\_df：使用 combined 后的 `first_adj`、`latest_adj` 计算 `qfq_factor/hfq_factor`，再生成 `*_qfq/*_hfq`

    * 合并并按主键去重排序

  * 异常场景（new\_df 含有早于历史最小 `trade_date` 的日期，或出现较大范围回补）：

    * 返回 `need_full_recalc=True`，对受影响股票（或全量）回退到现有 `calculate_rights_adjustment()` 全量重算，保证口径正确

## 管道一致性：增量也要做的“补充步骤”

增量宽表生成后，需要与 `run_full_pipeline()` 同口径完成以下步骤（对增量部分即可）：

* 聚宽 ST 数据合并生成 `is_st`（复用现有 `st_data/jq_stock_st_data.csv` 加工逻辑，但仅取增量 `trade_date` 范围参与 merge）

* 合并股票上市/退市信息（优先读取 `stock_list.parquet`，若不存在则调用 `get_all_stocks_info()` 并落盘缓存）

* 计算 `days_since_list` 与 `trd_days_since_list`

  * `trd_days_since_list` 不再用当前的逐股票循环生成全量长表（过大），改为基于交易日历的 index position 做向量化计算，仅对增量行生成数值

* 过滤 `trade_date >= list_date`

## 计划修改的文件

* `vnpy_tushare_pro/utils.py`

  * 新增 `DataPipelineEnhanced.run_incremental_pipeline`

  * 新增 schema 对齐与交易日推导的内部函数

  * 新增/增强增量复权更新逻辑（优先复用现有 `calculate_rights_adjustment` 的列名与计算口径）

  * 将新增代码补齐 type hints，并保持现有日志风格（loguru）

* `vnpy_tushare_pro/tushare_datafeed.py`（可选，但推荐）

  * 新增 `update_all_stock_history(start_date: str | None = None, end_date: str | None = None, output: Callable = print) -> DataFrame | None`

  * 该方法内部调用 `self.pipeline.run_incremental_pipeline(...)`，并更新 `self.df_all_stock`

## 验证方案（实现后执行）

* 新增一个最小验证脚本（或在交互环境中执行）：
  * 直接在vnpy_tushare_pro/utils.py中新增if __name__ == '__main__':进行测试，我已经新增了一些代码，你继续补充即可，完成以下功能的测试

  * 先加载本地 `df_all_stock.parquet`（若存在）

  * 调用增量接口跑一个很小的窗口（例如最近 1\~3 个交易日）

  * 验证：

    * 输出 DataFrame 列集合与历史 DataFrame 完全一致（`set` 与顺序）

    * `ts_code`/`trade_date` 主键去重后无重复

    * 对随机抽样的若干股票，检查 `close_qfq` 与 `adj_factor`/`qfq_factor` 的一致关系

    * parquet 覆盖写后可正常再次读取

## 风险与处理

* Tushare 接口限频/偶发空数据：延用现有 `_safe_api_call` 重试策略；增量期如出现某接口空数据，仍需保证列结构完整（靠 `merge_all_data` 的“空表补列”机制）。

* 历史回补（start\_date 早于现有最大日期）会触发“跨区间 upsert”：

  * 允许按主键覆盖旧行

  * 可能需要对受影响股票全量复权重算（计划中已包含回退逻辑）

