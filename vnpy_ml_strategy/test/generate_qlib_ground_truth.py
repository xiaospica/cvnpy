"""为 vnpy E2E 测试生成 qlib backtest ground truth.

用 D:/vnpy_data/qlib_data_bin 作为 provider_uri (与 vnpy 推理同源) 跑 qlib
TopkDropoutStrategy backtest, 输出 vnpy 实盘回放 E2E 测试需要的 ground truth:
  - {OUT_DIR}/pred.pkl                  qlib 端推理结果 (与 vnpy bit-equal)
  - {OUT_DIR}/positions_normal_1day.pkl 每日 Position 对象 (含 amount/price/weight)
  - {OUT_DIR}/report_normal_1day.pkl    每日 account/return/cash/turnover

被以下 vnpy 测试消费:
  - test_topk_e2e_d_drive.py          (持仓/权重 E2E 等价)
  - test_topk_e2e_equity_curve.py     (累积收益率/日收益率)
  - diagnose_holdings_diverge.py      (持仓 diverge 诊断)
  - diagnose_weight_offset.py         (weight 偏差归因实验)
  - plot_equity_curve_comparison.py   (累积收益率对比图)

⚠️ 仅 E2E 验证用, 不在训练路径上:
  - 不影响 qlib_strategy_dev 的 tushare_hs300_rolling_train.py (训练入口)
  - 不影响 qlib_strategy_dev 的 multi_segment_records.py (默认 deal_price=close)
  - 不影响任何 mlflow run / production 输出
deal_price="$open" 只用于让 qlib backtest 撮合层与 vnpy_qmt_sim (raw_open 撮合)
数学等价 (撮合后 amount × adj = floor(value/raw_open/100)×100 = vnpy amount)。

跨工程依赖 (脚本本身在 vnpy 仓库, 但需要 qlib_strategy_dev 仓库的 vendor + bundle):
  - qlib_strategy_dev/vendor/qlib_strategy_core (predict_from_bundle 入口)
  - qlib_strategy_dev/qs_exports/rolling_exp/{run_id} (bundle 含 task.json + params.pkl)
  - qlib_strategy_dev (factor_factory.alphas.* 跨工程 import)

运行 (用 inference_python 因为有 qlib 重型依赖):
  PYTHONPATH="f:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core;f:/Quant/code/qlib_strategy_dev" \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe \
  f:/Quant/vnpy/vnpy_strategy_dev/vnpy_ml_strategy/test/generate_qlib_ground_truth.py
"""
from __future__ import annotations

import copy
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"f:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core")
sys.path.insert(0, r"f:/Quant/code/qlib_strategy_dev")  # 让 factor_factory.alphas.* 能导入

# 安装 legacy path finder (与 vnpy 推理子进程一致)
from qlib_strategy_core._compat import install_finder  # noqa: E402
install_finder()

import qlib  # noqa: E402
from qlib_strategy_core.inference import predict_from_bundle  # noqa: E402

# BUNDLE_DIR 须与 vnpy 端 run_ml_headless.py 的当前活跃 bundle 一致, 否则 qlib
# backtest pred 与 vnpy 推理 pred 不同源, e2e 测试无意义。
# 可通过环境变量 BUNDLE_DIR 覆盖。
import os as _os
BUNDLE_DIR = Path(
    _os.getenv(
        "BUNDLE_DIR",
        r"f:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/c38e6cfdf549446fbb0d637549e4a245",
    )
)
PROVIDER_URI = r"D:/vnpy_data/qlib_data_bin"
OUT_DIR = Path(r"C:/Users/richard/AppData/Local/Temp/qlib_d_backtest")

# 与训练时 PortAnaRecord config 对齐 (reproduce.stdout.log:76-94)
BACKTEST_KWARGS = {
    "account": 1_000_000,
    # D:/vnpy_data/qlib_data_bin 不含指数代码 (000300.SH)，daily_ingest 只 dump csi300
    # 成分股数据。benchmark 仅用于算 excess return，我们对比 positions/sells/buys，
    # 用 csi300 内任意股代替即可不影响 backtest 决策。
    "benchmark": "600519.SH",  # 茅台，csi300 大盘股
    "exchange_kwargs": {
        # qlib 默认 deal_price="close" — 但 vnpy_qmt_sim 用 raw_open 撮合 (贴近实盘
        # 09:30 开盘建仓), 数学上不能等价。改 deal_price="$open" (hfq open) 后:
        #   qlib amount × adj_factor = floor(value × adj / hfq_open / 100) × 100
        #                            = floor(value / raw_open / 100) × 100
        #                            = vnpy amount
        # 两边撮合层严格等价 (仅整百取整 < 1 手误差)
        "deal_price": "$open",
        "freq": "day",
        "limit_threshold": 0.095,
        "open_cost": 0.0005,
        "close_cost": 0.0015,
        "min_cost": 5,
    },
}
STRATEGY_CONFIG = {
    "class": "TopkDropoutStrategy",
    "module_path": "qlib.contrib.strategy",
    "kwargs": {
        "topk": 7,
        "n_drop": 1,
        "only_tradable": True,
        # signal 在下面动态填充为 pred_df
    },
}
EXECUTOR_CONFIG = {
    "class": "SimulatorExecutor",
    "module_path": "qlib.backtest.executor",
    "kwargs": {
        "time_per_step": "day",
        "generate_portfolio_metrics": True,
        "verbose": False,
    },
}

# 新 bundle f60174 test segment 起点 2026-01-26, vnpy 回放第一笔 trade 在 2026-01-28
# (回放 day 1 = 1-27, prev_day_pred=None 不 rebalance; day 2 = 1-28 用 1-27 pred 建仓)
# qlib backtest 是 T-1 决策, START_TIME=2026-01-28 让两边第一天用同一份 pred (1-27)
START_TIME = "2026-01-28"
END_TIME = "2026-04-29"  # qlib calendar 末日 4-30 但 backtest 需要 t+1 next step → 用 4-29


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 用 D:/vnpy_data/qlib_data_bin 初始化 qlib
    # kernels=2 限制 joblib worker 数量, 避免 30+ worker 各 200MB 拉爆 Windows page file
    # (DLL load failed: ImportError _zpropack 页面文件太小). 默认 NUM_USABLE_CPU 在多核机器
    # 上会一次性 spawn 30+ 进程导致内存不足.
    qlib.init(provider_uri=PROVIDER_URI, region="cn", kernels=2)

    # 2. 用 bundle 推理拿 pred (live_end=END_TIME, lookback 大点 cover 整个回放区间)
    # filter_parquet 必须用 D:/vnpy_data 的最新 snapshot, 否则 task.json 里
    # 固化的训练时 filter 截止 2026-01-28 → pred 只覆盖到 1-28 → backtest 只 1 天
    print(f"=== Step 1: predict_from_bundle ({START_TIME} ~ {END_TIME}) ===")
    pred_df, task = predict_from_bundle(
        bundle_dir=BUNDLE_DIR,
        live_end=pd.Timestamp(END_TIME),
        lookback_days=160,  # 160 天回看, 覆盖整个回放区间
        handler_overrides={
            "filter_parquet": r"D:/vnpy_data/snapshots/filtered/csi300_filtered_20260430.parquet",
        },
    )
    pred_df.to_pickle(OUT_DIR / "pred.pkl")
    print(f"  pred shape={pred_df.shape}, "
          f"date range=[{pred_df.index.get_level_values(0).min()}, "
          f"{pred_df.index.get_level_values(0).max()}]")

    # 3. 用 pred + qlib backtest 跑 TopkDropoutStrategy
    print(f"\n=== Step 2: qlib backtest with TopkDropoutStrategy ===")
    from qlib.backtest import backtest as normal_backtest
    from qlib.workflow.record_temp import fill_placeholder

    strategy = copy.deepcopy(STRATEGY_CONFIG)
    strategy = fill_placeholder(strategy, {"<PRED>": pred_df})
    if "signal" not in strategy["kwargs"]:
        strategy["kwargs"]["signal"] = pred_df

    # backtest segment = 重叠的回放区间
    pred_dates = pred_df.index.get_level_values(0)
    bt_start = max(pd.Timestamp(START_TIME), pred_dates.min())
    bt_end = min(pd.Timestamp(END_TIME), pred_dates.max())

    portfolio_metric, indicator = normal_backtest(
        executor=EXECUTOR_CONFIG,
        strategy=strategy,
        start_time=bt_start,
        end_time=bt_end,
        **BACKTEST_KWARGS,
    )

    # 4. 落盘
    print(f"\n=== Step 3: dump artifacts → {OUT_DIR} ===")
    for freq, (report, positions) in portfolio_metric.items():
        with open(OUT_DIR / f"report_normal_{freq}.pkl", "wb") as f:
            pickle.dump(report, f)
        with open(OUT_DIR / f"positions_normal_{freq}.pkl", "wb") as f:
            pickle.dump(positions, f)
        n_dates = len([k for k in positions if isinstance(k, pd.Timestamp)])
        print(f"  positions: {n_dates} dates")

    # 5. 打印每日持仓 sample
    pos_1day = portfolio_metric["1day"][1]
    sample_dates = sorted([k for k in pos_1day if isinstance(k, pd.Timestamp)])[:3]
    for d in sample_dates:
        holdings = {
            k: v for k, v in pos_1day[d].position.items()
            if k not in ("cash", "now_account_value") and isinstance(v, dict)
        }
        print(f"  {d.date()}: holdings={list(holdings.keys())}")

    print(f"\n[OK] qlib backtest with D:/vnpy_data/qlib_data_bin done.")
    print(f"     ground truth dir: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
