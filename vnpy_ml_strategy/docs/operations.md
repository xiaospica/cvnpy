# 运维手册

vnpy_ml_strategy 生产环境的日常运维 / 监控 / 故障排查 / 升级流程. 本文档是
[deployment.md](deployment.md) 之后, [developer.md](developer.md) 之前的层次 —
"机器已经跑起来了, 怎么持续看着它健康跑下去".

---

## 1. 日常监控

### 1.1 关键日志位置

| 组件 | 默认位置 | 作用 |
|---|---|---|
| vnpy 主进程 | NSSM `AppStdout/AppStderr` (`D:/vnpy_logs/vnpy_headless.log`) | 启动 / cron 触发 / send_order / on_trade |
| vnpy 主进程 (loguru) | `C:/Users/{user}/.vntrader/log/vt_YYYYMMDD.log` | 框架级 log (gateway / event_engine) |
| 推理子进程 | `D:/ml_output/{strategy}/{yyyymmdd}/diagnostics.json` 的 `error_message` 字段 | qlib + lightgbm subprocess 异常 |
| webtrader uvicorn | NSSM (`D:/vnpy_logs/vnpy_webtrader_http.log`) | HTTP 请求日志 |
| mlearnweb research | NSSM (`D:/vnpy_logs/mlearnweb_research.log`) | /api/training-records / experiments 请求 |
| mlearnweb live | NSSM (`D:/vnpy_logs/mlearnweb_live.log`) | /api/live-trading 请求 + sync_loop |

⚠️ 当前 loguru 默认无 rotation, 几周后会塞满磁盘. 详见 [§5 已知运维不足](#5-已知运维不足).

### 1.2 周期性人工检查

每天早上 09:00 (开盘前):

```powershell
# 1. 看昨晚 21:00 推理是否成功
F:\Program_Home\vnpy\python.exe -c "
import json, glob
from pathlib import Path
from datetime import date, timedelta
yest = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
for diag_path in Path('D:/ml_output').rglob(f'{yest}/diagnostics.json'):
    diag = json.loads(diag_path.read_text(encoding='utf-8'))
    print(f\"{diag_path.parts[2]}: status={diag['status']} rows={diag.get('rows',0)} error={diag.get('error_message','')}\")
"
# 期望: 每个策略 status='ok', rows > 200

# 2. 看今天的 daily_ingest 是否跑过 (实盘机 20:00 cron)
ls D:/vnpy_data/snapshots/merged/daily_merged_$(date +%Y%m%d).parquet
ls D:/vnpy_data/snapshots/filtered/csi300_*_$(date +%Y%m%d).parquet

# 3. 看 vnpy 主进程 / mlearnweb 是否还活着
nssm status vnpy_headless
nssm status mlearnweb_research
nssm status mlearnweb_live

# 4. 看是否有 ERROR (loguru log 末尾 200 行)
Get-Content C:\Users\$env:USERNAME\.vntrader\log\vt_*.log | Select-Object -Last 200 | Select-String "ERROR"
```

09:30 开盘后 (确认 09:26 cron 跑了 + 撮合 OK):

```powershell
# 1. 看今天发单情况
sqlite3 F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state\sim_QMT_SIM_csi300.db "SELECT COUNT(*), MIN(insert_time), MAX(insert_time) FROM sim_orders WHERE DATE(insert_time)=DATE('now')"

# 2. 实盘端: miniqmt 客户端看委托 + 成交

# 3. 前端
# 浏览器: http://localhost:5173/live-trading
# 期望: 策略卡片 PnL 更新中, 持仓刷新
```

15:00 收盘后:

```powershell
# 1. 看 settle 是否成功
F:\Program_Home\vnpy\python.exe -c "
import sqlite3
con = sqlite3.connect(r'F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state/sim_QMT_SIM_csi300.db')
row = con.execute('SELECT last_settle_date FROM sim_accounts').fetchone()
print('last_settle_date =', row[0])
"
# 期望: 今日日期

# 2. 看 mlearnweb 拉到数据
sqlite3 F:\Quant\code\qlib_strategy_dev\mlearnweb\backend\mlearnweb.db "SELECT strategy_name, COUNT(*) FROM strategy_equity_snapshots WHERE DATE(ts)=DATE('now') GROUP BY strategy_name"
```

### 1.3 推荐监控告警 (尚未实现)

以下还没有现成实现, 强烈建议接入:

- 20:00 daily_ingest 失败 → 邮件 / 微信告警 (event `EVENT_DAILY_INGEST_FAILED` 已发, 缺出口)
- 21:00 推理 raise → 同上 (`last_status='failed'` 在 strategy variables)
- 09:26 send_order 拒单率 > 阈值 → 同上
- 磁盘剩余 < 50 GB → 系统监控
- 内存峰值 > 28 GB → swap 风险

接入路径: mlearnweb 加 `/api/health` 端点 + Healthchecks.io / Uptime Kuma 5 min
心跳. 详见 [`docs/deployment_windows.md`](../../docs/deployment_windows.md) §P1-3.

---

## 2. 故障排查

### 2.1 vnpy 启动失败

```
[headless] adding strategy csi300_live (QlibMLStrategy) -> gateway=QMT...
ValueError: 策略 'csi300_live' 引用了未注册的 gateway_name='QMT'
```

→ GATEWAYS 里没有 name='QMT' 那条. 检查 `run_ml_headless.py` GATEWAYS 数组.

```
ValueError: GATEWAYS 含 2 个 kind=live gateway, miniqmt 单进程单账户约束只允许 1 个
```

→ miniqmt 单进程单账户约束. 多账户必须跨进程, 详见 [dual_track.md §Q5](dual_track.md).

```
RuntimeError: bundle ... 未注册到 ModelRegistry 或缺 filter_config
```

→ bundle 缺 `filter_config.json`. 老 bundle 没生成此文件, 跑迁移脚本:
```powershell
cd F:/Quant/code/qlib_strategy_dev
E:/ssd_backup/.../python.exe scripts/backfill_filter_config.py --apply
```

```
FilterParquetError: filter_chain_specs 为空
```

→ ModelRegistry 没拿到 filter_config, 或 run_ml_headless 没注入. 检查启动期 log:
```
[headless] DailyIngestPipeline.filter_chain_specs 已注入 N 个 filter_id
```

### 2.2 推理失败

```
diagnostics.json:
  "status": "failed",
  "error_message": "ImportError: No module named 'qlib'"
```

→ 推理 Python (3.11) 没装 pyqlib. `E:/.../python.exe -m pip install pyqlib`.

```
"error_message": "No data found for live_end=2026-04-30, lookback=60"
```

→ qlib_data_bin 数据不够 (当日没拉 / lookback 窗口缺历史). 检查
`D:/vnpy_data/qlib_data_bin/calendars/day.txt` 末尾日期. 跑过 `daily_ingest`?

```
"error_message": "filter_parquet not found: ..."
```

→ filter snapshot 缺. 检查 `D:/vnpy_data/snapshots/filtered/` 有没有当日的
`{filter_id}_T.parquet`. 跑 daily_ingest 重生.

### 2.3 09:26 rebalance 不发单

策略详情页持仓刷新但 sim_trades 没新行?

```python
# 看 strategy variables
F:/Program_Home/vnpy/python.exe -c "
# 通过 webtrader HTTP 拉
import requests
r = requests.post('http://localhost:8001/api/v1/token', data={'username':'vnpy','password':'vnpy'})
token = r.json()['access_token']
# ... 看 strategy.last_status / last_error 字段
"
```

常见原因:
- selections.parquet 不存在 (上一交易日 21:00 cron 没跑)
- gateway 未 connected (live 模式 miniqmt 客户端断开?)
- enable_trading=False (干跑模式)
- replay_status='running' (回放还没跑完, 真实 cron 被暂停)
- 当日是非交易日 (节假日识别)

### 2.4 mlearnweb 前端权益曲线断档

- 实时数据缺 → snapshot_loop / vnpy_webtrader endpoint 故障. 检查 mlearnweb live
  日志的 `[vnpy.client] node=... failed` warning.
- 历史回放数据缺 → replay_equity_sync_loop 没拉. 立即手动触发:
  ```powershell
  curl -X POST http://localhost:8100/internal/replay_equity_sync
  # 或调 sync_all() 函数
  ```

### 2.5 双轨场景: 影子策略不动

```
[shadow] day 2026-04-30 上游 'csi300_live' 产物未就绪
```

→ 上游 selections.parquet 没生成. 看上游策略 last_status:
- `failed` → 上游推理失败, 影子也跟着停 (设计如此, 同信号)
- `empty` → 上游推理成功但没数据 (filter 太严 / 当日数据缺)
- `ok` 但 selections.parquet 不存在 → persist 失败, 检查磁盘 / 权限

修复后影子下次 cron 自动跟上.

### 2.6 sim_db 损坏 (SQLite 锁死 / 进程崩溃)

```
sqlite3.OperationalError: database is locked
```

→ 异常退出后 lockfile 残留. 当前 `vnpy_qmt_sim/persistence.py` 用
`msvcrt.locking` 自动 OS 释放, 一般重启后好. 如果不行:
```powershell
nssm stop vnpy_headless
del F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state\*.lock
nssm start vnpy_headless
```

⚠️ 不要直接删 .db 文件, 会丢失所有持仓 / 资金状态. 删之前先备份.

---

## 3. 备份 / 恢复

### 3.1 关键数据 (按重要度)

| 数据 | 路径 | 损失影响 |
|---|---|---|
| **bundle** | `D:/vnpy_data/models/{run_id}/` | 必须能从训练机重新 rsync 恢复; 否则需要重训 |
| **mlearnweb.db** | `F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db` | 训练记录 + 回放历史 + 权益曲线; 损失 → 前端图表空白 |
| **sim_<gateway>.db** | `vnpy_qmt_sim/.trading_state/` | 模拟柜台状态; 损失 → 模拟权益曲线断档, 但实盘不受影响 |
| **replay_history.db** | `D:/vnpy_data/state/replay_history.db` | 回放权益历史 (A1/B2); mlearnweb 端可重新 sync, 但要等 5 min 周期 |
| **.vntrader/database.db** | `C:/Users/{user}/.vntrader/database.db` | vnpy bar database; 损失 → 历史 K 线缺 |
| **vt_setting.json** | `C:/Users/{user}/.vntrader/vt_setting.json` | 包含 tushare token / 节点路径; 必须重新填 |

### 3.2 推荐备份方案

```powershell
# deploy/daily_backup.ps1 (任务计划程序 02:00 触发)
$today = Get-Date -Format "yyyyMMdd"
$backup_root = "D:\backups\$today"
mkdir $backup_root -Force

# 1. 数据库
copy F:\Quant\code\qlib_strategy_dev\mlearnweb\backend\mlearnweb.db $backup_root\
copy D:\vnpy_data\state\*.db $backup_root\
copy F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state\*.db $backup_root\
copy C:\Users\$env:USERNAME\.vntrader\database.db $backup_root\
copy C:\Users\$env:USERNAME\.vntrader\vt_setting.json $backup_root\

# 2. bundle 元数据 (params.pkl 太大跳过, 训练机有备份)
Get-ChildItem D:\vnpy_data\models\*\manifest.json,filter_config.json,task.json | Copy-Item -Destination $backup_root -Force

# 3. 压缩 + 上传 NAS / S3
7z a -t7z -mx9 D:\backups\daily_$today.7z $backup_root
# rclone copy D:\backups\daily_$today.7z my-s3:vnpy-backup/

# 4. 保留 30 天, 老备份清理
Get-ChildItem D:\backups\*.7z | Where { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item
```

### 3.3 恢复流程

```powershell
# 1. 先停所有服务
nssm stop vnpy_headless
nssm stop mlearnweb_research
nssm stop mlearnweb_live

# 2. 解压备份
7z x D:\backups\daily_<yyyymmdd>.7z -oD:\backups\restore

# 3. 选择性恢复 (按需)
# 实盘机数据:
copy D:\backups\restore\<yyyymmdd>\mlearnweb.db F:\Quant\code\qlib_strategy_dev\mlearnweb\backend\
copy D:\backups\restore\<yyyymmdd>\sim_*.db F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state\
copy D:\backups\restore\<yyyymmdd>\vt_setting.json C:\Users\$env:USERNAME\.vntrader\

# 4. 启动
nssm start vnpy_headless
nssm start mlearnweb_research
nssm start mlearnweb_live
```

---

## 4. 升级流程

### 4.1 模型升级 (新 bundle 上线)

```powershell
# 1. 训练机产出新 bundle (run_id_new)
# 2. rsync 到部署机
rsync -avz user@train_host:qs_exports/rolling_exp/<run_id_new>/ D:/vnpy_data/models/<run_id_new>/

# 3. 干跑验证 (双轨架构利好场景)
# 改 run_ml_headless.py 配影子策略 + 新 bundle:
#   GATEWAYS 加 {"kind": "sim", "name": "QMT_SIM_new", "setting": ...}
#   STRATEGIES 加新策略, gateway=QMT_SIM_new, signal_source_strategy 不写, bundle_dir=<run_id_new>
# 重启 vnpy_headless
nssm restart vnpy_headless

# 4. 跑 ~1 周影子, 看 mlearnweb 前端两条曲线对比 (老 vs 新)

# 5. 切换实盘
# 改 STRATEGIES 把实盘策略 bundle_dir 指向 run_id_new
# 删旧影子 (或保留为对照)
nssm restart vnpy_headless
```

### 4.2 vnpy_ml_strategy 代码升级

```powershell
# 1. 拉新代码
cd F:\Quant\vnpy\vnpy_strategy_dev
git pull
git submodule update --recursive

# 2. 看 docs/deployment_a1_p21_plan.md / CHANGELOG 有无破坏性变更

# 3. 跑 P2-1 测试 (~10s)
F:\Program_Home\vnpy\python.exe -m pytest \
  vnpy_ml_strategy/test/test_replay_history.py \
  vnpy_ml_strategy/test/test_template_replay_persist.py \
  vnpy_ml_strategy/test/test_signal_source_strategy.py \
  vnpy_ml_strategy/test/test_dual_gateway_routing.py \
  vnpy_ml_strategy/test/test_dual_track_with_fake_live.py
# 期望: 34 passed

# 4. 平滑重启 (vnpy_headless 暂时下线 → 上线, 中间 1 分钟 mlearnweb 看到节点离线)
nssm stop vnpy_headless
nssm start vnpy_headless

# 5. 验证策略恢复 (sim 模式 sim_db 持久化自动 restore, live 模式 miniqmt 重连)
```

### 4.3 mlearnweb 升级

```powershell
cd F:\Quant\code\qlib_strategy_dev\mlearnweb
git pull

# 后端依赖
E:\ssd_backup\...\python.exe -m pip install -r backend\requirements.txt

# 前端
cd frontend
npm install
npm run build

# 重启
nssm restart mlearnweb_research
nssm restart mlearnweb_live
```

---

## 5. 已知运维不足

(详见 [`docs/deployment_windows.md`](../../docs/deployment_windows.md) P1)

### 5.1 日志无 rotation

loguru 默认不滚动, NSSM stdout/stderr 也不. 几周后磁盘满.

**解决 (TODO)**: vnpy 主入口加:
```python
from loguru import logger
logger.add("D:/vnpy_logs/vnpy_headless_{time:YYYY-MM-DD}.log",
           rotation="100 MB", retention="14 days", compression="zip")
```

### 5.2 监控告警空白

EVENT_DAILY_INGEST_FAILED / EVENT_ML_METRICS / 拒单率没出口.

**解决 (TODO)**: mlearnweb 加 `/api/health` 端点 + Healthchecks.io / Uptime Kuma
心跳每 5 min ping.

### 5.3 NTP 时钟漂移

Windows server 默认 NTP 在某些网段不可靠, A 股 09:26 时间窗严.

**解决**:
```powershell
w32tm /config /manualpeerlist:"ntp.ntsc.ac.cn,0x9" /syncfromflags:manual /update
w32tm /resync
# 监控偏差
w32tm /query /status
```

### 5.4 多策略推理并发 OOM

P1-1 已加 `_validate_trigger_time_unique` 启动期校验, 但:
- 错峰 < 10 min 时仍可能撞峰
- 不限制别的进程跑大事 (mlearnweb / 备份 / 杀毒) 占内存

**解决**: 设 NSSM AppPriority `BELOW_NORMAL_PRIORITY_CLASS` 让推理子进程不抢主流程优先级.

---

## 6. 应急联系 / 升级路径

(占位, 项目方填具体值)

| 问题级别 | 联系 | SLA |
|---|---|---|
| P0 (实盘亏钱) | <on-call phone> | 15 min 响应 |
| P1 (mlearnweb 监控断) | <oncall mail> | 1 h 响应 |
| P2 (前端 UI bug) | <issue tracker> | 24 h 响应 |

---

## 7. 进一步阅读

- [deployment.md](deployment.md) — 部署 checklist
- [dual_track.md](dual_track.md) — 双轨架构 (升级 / 切换流程)
- [`../test/README.md`](../test/README.md) — 测试体系 (升级前回归)
- [`docs/deployment_windows.md`](../../docs/deployment_windows.md) — 运维不足 + 改进路线图
