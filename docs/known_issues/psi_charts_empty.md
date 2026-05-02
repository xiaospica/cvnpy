# Issue: 策略监控 tab 中 PSI 相关图表无数据

## 现状

策略监控 tab 5 张主图：

| 图表 | 状态 | 数据源 |
|---|---|---|
| IC 时序 | ✅ 有数据 | ml_metric_snapshots.ic (auto backfill) |
| Rank IC 时序 | ✅ 有数据 | ml_metric_snapshots.rank_ic (auto backfill) |
| 预测分数直方图（最新）| ✅ 有数据 | endpoint 字段级 fallback 到 SQLite |
| **PSI_mean 时序** | ❌ 空白 | ml_metric_snapshots.psi_mean 全 None |
| **PSI top-10 特征** | ❌ "暂无 PSI 特征详情" | ml_metric_snapshots.psi_by_feature_json 全空 |

## 根因

PSI（Population Stability Index）算的是"特征分布相对训练时点 baseline 的漂移"，
需要每日的**完整特征矩阵** (Alpha158 = 158 列特征)。

但当前 vnpy 推理子进程 (`run_inference.py` batch 模式) 只 dump
`predictions.parquet`（单列 score）+ `diagnostics.json`，**不存特征矩阵**。
故 mlearnweb 端无法独立算 PSI（除非自己重新加载 qlib + bundle + 重建 dataset
= 数百 MB 内存 + 工作量大）。

## 单日推理模式有 PSI

`run_inference.py` **single-day 模式** (`--live-end`) 算 PSI 写 metrics.json
（cli/run_inference.py:256-262）:

```python
features_df = dataset.prepare(["test"], col_set="feature")[0]
psi_by_feature = compute_psi_by_feature(features_df, baseline_df)
metrics["psi_by_feature"] = psi_by_feature
metrics.update(summarize_psi(psi_by_feature))
```

但 vnpy 回放走 batch 模式（一次性跑整段日期），cli/run_inference.py:206 注释
明确："不写 metrics.json — 简化版"，跳过 PSI 计算。

## 修复方案（按工作量排序）

**方案 1（轻量，推荐）— vnpy batch 也写 PSI**：
扩展 `vendor/qlib_strategy_core/qlib_strategy_core/cli/run_inference.py` 的
batch loop（line 195-238），按日切片 features_df 后调 compute_psi_by_feature
+ summarize_psi 写入 metrics.json。

工作量：~30 行代码 + 在 batch loop 里多加一次 PSI 计算（features_df 已经
prepare 过，复用即可，性能开销小）。

**方案 2（重量）— mlearnweb 端独立算**：
让 mlearnweb backend 加载 qlib + bundle + 重建 dataset, scan
predictions.parquet 反推特征 → 算 PSI → UPDATE db。

工作量：~150 行 + 加载 qlib (~500MB 内存) + 重型依赖 (mlearnweb 当前避免引入 qlib)。

## 临时妥协

保留前端"暂无 PSI 特征详情"的提示，PSI_mean 时序图保持空白即可（不报错）。

## 优先级

中。IC 是模型监控的主要指标已经能看到，PSI 是次要指标（当 ICIR 衰减时辅助
判断是数据漂移还是模型失效）。建议方案 1 实现。

## 相关 commit

- 7f3ddd0: ml-monitoring 自动 backfill (IC + 直方图 + pred_stats)
- 9d87b5b: prediction/latest/summary 字段级 fallback 修直方图空白
EOF
)
