# Windows server 部署指南

把 vnpy_ml_strategy + mlearnweb + 训练管道部署到生产 Windows server, 跑实盘策略.
本文档假定零基础, 给出从空机到生产的**完整 checklist**.

> **状态**: 部分待决议. 跨章节决策点 (凭证管理 / 服务化方案 / 备份策略) 详见
> [`docs/deployment_windows.md`](../../docs/deployment_windows.md) §6 待决议清单.

---

## 1. 服务器规格

| 资源 | 最小要求 | 推荐 | 原因 |
|---|---|---|---|
| CPU | 4 核 | 8 核 | 推理子进程 lightgbm 多线程; vnpy + mlearnweb 主进程 |
| RAM | 16 GB | 32+ GB | qlib 推理峰值 4-5 GB; 多策略并发时 N×5GB; 还要留给 OS / IO 缓存 |
| 磁盘 | 200 GB SSD | 500 GB SSD | qlib_data_bin (30GB+) / snapshots (年量 ~20GB) / ml_output / logs / 备份 |
| 网络 | 中国大陆 | 同 | tushare API / 券商 miniqmt RPC / mlearnweb 前端访问 |
| OS | Windows 10/11 Pro / Server 2019+ | Server 2022 | miniqmt 不支持 Linux; PowerShell 5.1+ 需要 |

⚠️ **盘符约定**: 默认 `D:` 数据盘 (qlib bin / snapshots / ml_output / state),
`F:` 程序盘 (vnpy 工程). 改动需同步改 [`run_ml_headless.py`](../../run_ml_headless.py)
中 env 默认值 + 各 setting 路径.

---

## 2. 部署 checklist (按顺序)

### Step 1. 安装 Python 双版本

| Python | 版本 | 用途 | 安装路径 (本仓库默认) |
|---|---|---|---|
| vnpy 主 Python | 3.13 | vnpy 主进程 / 撮合 / 策略 | `F:/Program_Home/vnpy/python.exe` |
| 推理 Python | 3.11 | qlib + lightgbm + mlflow | `E:/ssd_backup/.../python-3.11.0-amd64/python.exe` |

```powershell
# vnpy 主 Python (3.13)
# 从 https://www.python.org/downloads/windows/ 装到 F:\Program_Home\vnpy
# 或用 vnpy 官方虚拟环境

# 推理 Python (3.11)
# 装到 E:\ssd_backup\... 或自定义, 然后 setx
setx INFERENCE_PYTHON "E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe" /M
```

### Step 2. 拉代码

```powershell
# vnpy 工程 (主仓库 + ml_data_build submodule)
cd F:\Quant\vnpy
git clone --recursive <vnpy-strategy-dev-repo-url> vnpy_strategy_dev
cd vnpy_strategy_dev
git submodule update --init --recursive

# qlib 工程 (跨工程依赖, vendor + 训练侧)
cd F:\Quant\code
git clone --recursive <qlib-strategy-dev-repo-url> qlib_strategy_dev
cd qlib_strategy_dev
git submodule update --init --recursive
```

### Step 3. 安装依赖

```powershell
# vnpy 主进程依赖
cd F:\Quant\vnpy\vnpy_strategy_dev
.\install.bat F:\Program_Home\vnpy\python.exe

# 推理子进程依赖 (qlib + lightgbm + mlflow + sklearn + pandas + pyarrow)
E:\ssd_backup\...\python.exe -m pip install -r F:\Quant\code\qlib_strategy_dev\requirements.txt
# 包括: pyqlib, lightgbm, mlflow, scikit-learn 等

# mlearnweb (后端 + 前端)
cd F:\Quant\code\qlib_strategy_dev\mlearnweb
E:\ssd_backup\...\python.exe -m pip install -r backend/requirements.txt
cd frontend
npm install
npm run build
```

### Step 4. 配置 vt_setting.json (vnpy 全局设置)

```powershell
# 默认路径: C:\Users\{user}\.vntrader\vt_setting.json
# 关键字段:
{
  "datafeed.name": "tushare_pro",
  "datafeed.username": "test",
  "datafeed.password": "<your tushare token>",   # ← 必填!
  "log.active": true,
  "log.level": 20,
  "log.file": true,
  ...
}
```

⚠️ **凭证管理**: tushare token 不要 commit. 部署机一次性复制,
权限限定到运行账户. 详见 [operations.md §凭证安全](operations.md).

### Step 5. 配置数据目录 + env

```powershell
# 数据根 (统一 D:)
mkdir D:\vnpy_data
mkdir D:\vnpy_data\snapshots\merged
mkdir D:\vnpy_data\snapshots\filtered
mkdir D:\vnpy_data\stock_data
mkdir D:\vnpy_data\state           # replay_history.db (A1/B2)
mkdir D:\vnpy_data\models          # bundle 部署目录
mkdir D:\vnpy_data\jq_index        # 聚宽成分股 CSV
mkdir D:\ml_output                 # 策略每日产物

# 关键 env (Machine scope)
setx QS_DATA_ROOT "D:/vnpy_data" /M
setx ML_DAILY_INGEST_ENABLED "1" /M  # 关键! 启 20:00 cron
setx ML_OUTPUT_ROOT "D:/ml_output" /M
setx VNPY_MODEL_ROOT "D:/vnpy_data/models" /M
setx INFERENCE_PYTHON "E:/ssd_backup/.../python-3.11.0-amd64/python.exe" /M
```

⚠️ env 改动后**重启所有 PowerShell** 才生效.

### Step 6. 准备聚宽成分股 CSV

CSI300 成分股动态变化, 需要带历史调入调出的 CSV. 默认路径
`D:/vnpy_data/jq_index/hs300_*.csv`. 由聚宽 / Wind / Tushare pro index_member
导出. 详见 [`vnpy_tushare_pro/ml_data_build/data_source.py:OfflineIndexDataSource`](../../vnpy_tushare_pro/ml_data_build/data_source.py).

### Step 7. 首次拉数据 + dump qlib bin

```powershell
# 手动跑一次 daily ingest (而不是等 20:00 cron)
F:\Program_Home\vnpy\python.exe -c "
from vnpy_tushare_pro import TushareDatafeedPro
dp = TushareDatafeedPro()
dp.daily_ingest_pipeline.set_filter_chain_specs({
    'csi300_no_suspend_min_90_days_in_csi300': {
        'schema_version': 1,
        'universe': 'csi300',
        'filter_id': 'csi300_no_suspend_min_90_days_in_csi300',
        'filter_chain': [
            {'name': 'no_suspend',  'class': 'SuspendFilter',          'params': {}},
            {'name': 'min_90_days', 'class': 'NewStockFilter',         'params': {'min_days': 90}},
            {'name': 'in_csi300',   'class': 'IndexConstituentFilter', 'params': {'index_code': '000300.SH'}},
        ],
        'training_filter_parquet_basename': 'csi300_custom_filtered.parquet',
    }
})
result = dp.daily_ingest_pipeline.ingest_today('20260430')
print(result)
"
# 期望: stages_done = ['fetch', 'filter', 'by_stock', 'dump']
# 检查 D:/vnpy_data/qlib_data_bin/calendars/day.txt 末尾日期
```

### Step 8. rsync bundle 到部署机

训练机产出 bundle 后:
```bash
# 训练机 (qlib_strategy_dev)
rsync -avz qs_exports/rolling_exp/<run_id>/ user@deploy_host:D:/vnpy_data/models/<run_id>/
# 或用 SCP / WinSCP
```

bundle 含: `params.pkl, task.json, manifest.json, filter_config.json` (5 个文件).

### Step 9. 改 run_ml_headless.py 配置

```python
# F:/Quant/vnpy/vnpy_strategy_dev/run_ml_headless.py 顶部改:

# 9a. STRATEGIES 中 bundle_dir 指向 Step 8 拷过来的目录
STRATEGIES = [
    {
        "strategy_name": "csi300_live",
        "strategy_class": "QlibMLStrategy",
        "gateway_name": "QMT",   # 或 "QMT_SIM_xxx"
        "setting_override": {
            "bundle_dir": r"D:/vnpy_data/models/<run_id>",  # ← 改这里
            "topk": 7, "n_drop": 1,
            "trigger_time": "21:00",
        },
    },
]

# 9b. GATEWAYS 选实盘 / 模拟 / 双轨 (详见 dual_track.md §3)
```

### Step 10. 配置 miniqmt (实盘必须)

```
1. 安装迅投极速交易终端 (券商提供, 比如 国金 / 国泰君安)
2. 客户端登录后, userdata_mini 目录会被创建, 默认 E:\迅投极速交易终端\userdata_mini
3. 把路径填到 run_ml_headless.py QMT_SETTING.客户端路径
4. QMT_SETTING.资金账号 填券商账户号
```

实盘 + 模拟双轨时这步必须. 全模拟 (kind=sim) 跳过.

### Step 11. 启动 vnpy 主进程

```powershell
# 前台跑 (开发 / 测试)
F:\Program_Home\vnpy\python.exe F:\Quant\vnpy\vnpy_strategy_dev\run_ml_headless.py

# 期望日志:
# [headless] add_gateway kind=live name=QMT class=QmtGateway
# [headless] connecting gateway QMT...
# [headless] webtrader RPC server started on tcp://127.0.0.1:2014 / 4102
# [headless] webtrader HTTP server (uvicorn) spawned pid=... on http://127.0.0.1:8001
# [headless] DailyIngestPipeline.filter_chain_specs 已注入 1 个 filter_id
# [headless] adding strategy csi300_live (QlibMLStrategy) -> gateway=QMT
# [headless] 1 个策略已就绪: ['csi300_live']
```

### Step 12. 启动 mlearnweb

```powershell
# 默认双进程 (research:8000 + live:8100) + 前端 (5173)
cd F:\Quant\code\qlib_strategy_dev
.\start_mlearnweb.bat E:\ssd_backup\...\python.exe
# 浏览器打开 http://localhost:5173
```

### Step 13. 配置 mlearnweb 节点

```yaml
# F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/vnpy_nodes.yaml
nodes:
  - node_id: local
    base_url: http://127.0.0.1:8001
    username: vnpy
    password: vnpy
    enabled: true
    mode: live          # 默认安全偏 sim, 实盘机改 live
```

mode 字段决定前端 mode badge 颜色 (live 红 / sim 绿). 改动需重启 mlearnweb live_main.

### Step 14. 服务化 (Windows Service)

详见 [operations.md §服务化](operations.md). 推荐 NSSM:

```powershell
# 安装 NSSM
choco install nssm  # 或下 https://nssm.cc/

# 注册 vnpy_headless 服务
nssm install vnpy_headless F:\Program_Home\vnpy\python.exe F:\Quant\vnpy\vnpy_strategy_dev\run_ml_headless.py
nssm set vnpy_headless AppStdout D:\vnpy_logs\vnpy_headless.log
nssm set vnpy_headless AppStderr D:\vnpy_logs\vnpy_headless.err
nssm start vnpy_headless

# 同样注册 mlearnweb_research / mlearnweb_live
```

---

## 3. 部署后验收 checklist

### 3.1 vnpy 端

- [ ] `F:\Program_Home\vnpy\python.exe run_ml_headless.py` 启动无 raise
- [ ] [`docs/deployment_a1_p21_plan.md §六 验证 cmd`](../../docs/deployment_a1_p21_plan.md) 全跑过
- [ ] `D:/vnpy_data/state/replay_history.db` 在跑回放后有数据 (查询: `sqlite3 ... "SELECT COUNT(*) FROM replay_equity_snapshots"`)
- [ ] `D:/ml_output/{strategy}/{T}/selections.parquet` 21:00 后存在
- [ ] sim_db 在 `vnpy_qmt_sim/.trading_state/sim_<gateway>.db` 存在 (sim 模式)
- [ ] miniqmt connected (live 模式, 检查日志 `gateway.connected=True`)

### 3.2 mlearnweb 端

- [ ] http://localhost:5173/live-trading 看到所有策略卡片
- [ ] 卡片右上角 mode badge (live 红 / sim 绿) 与 vnpy_nodes.yaml mode 一致
- [ ] 详情页权益曲线非空, 时间戳更新中
- [ ] http://localhost:5173/ TrainingRecordsPage 看到 deployment chip 关联训练记录
- [ ] mlearnweb 后端 8000 / 8100 端口 listening, 无 ERROR 日志

### 3.3 数据流

- [ ] T 日 20:00 cron 自动跑 daily_ingest, calendar 推进到 T
- [ ] T 日 21:00 cron 自动推理, selections.parquet 生成
- [ ] T+1 日 09:26 cron 自动 rebalance, send_order 发出
- [ ] 真券商成交回报到 on_trade, vnpy_qmt_sim 撮合产生 sim_trades 行
- [ ] mlearnweb sync_loop 拉到数据, 前端实时更新

---

## 4. 部署相关已知不足 (deployment_windows.md 详述)

- **凭证明文**: vt_setting.json tushare token 明文; miniqmt 资金账号写在 setting
- **路径硬编码**: run_ml_headless.py 多处 F:/E:/D: 绝对路径
- **日志无 rotation**: loguru 默认 / NSSM stdout 都不滚动
- **监控告警空白**: 20:00 ingest 失败 / 21:00 推理 raise / 09:26 拒单全静默
- **服务化未自动化**: deploy/install_services.ps1 还没写, 当前手动 NSSM
- **备份策略空**: bundle / mlearnweb.db / sim_db 都没备份
- **NTP 时钟漂移**: A 股 09:26 时间窗严, 默认 Windows NTP 不一定可靠
- **跨工程 mlearnweb.db 历史耦合** ← 已修 (A1/B2)
- **trigger_time 错峰** ← 已加硬校验 (P1-1)
- **实盘/模拟混部** ← 已实现 (P2-1)

详见 [`docs/deployment_windows.md`](../../docs/deployment_windows.md) §1-3 (P0/P1/P2 分级).

---

## 5. 进一步阅读

- [operations.md](operations.md) — 监控 / 故障排查 / 升级
- [`docs/deployment_windows.md`](../../docs/deployment_windows.md) — 部署评估
- [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) — A1+P2-1 实施 + 验证
- [dual_track.md](dual_track.md) — 双轨架构 (实盘 + 影子)
