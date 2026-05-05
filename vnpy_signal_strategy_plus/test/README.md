# 端到端回归测试套件

用于验证整条 **Redis Stream → MySQL → vnpy 策略 → sim 网关** 链路的逻辑正确性，
对账 sim 模拟撮合结果与原始历史交易记录的差异。

## 1. 链路图

```
[csv_to_redis_replay] ─xadd─→ Redis Stream ─xreadgroup─→ [bridge] ─INSERT─→ MySQL stock_trade
                                                                                    │
                                                                                    ↓ poll 50ms
[run_sim_e2e]  EventEngine + MainEngine ←─send_order─ [EtfIntraTestStrategy]  process_signal
   ├ QmtSimGateway                                            ↑
   │  ├ md (merged_parquet 行情源)                            │
   │  └ td (撮合 + sim_QMT_SIM.db 持久化)                    │
   ├ SignalStrategyPlusApp                                    │
   │  └ EtfIntraTestStrategy (回放控制器：跨日 settle / refresh_tick / _replay_now)
   └ WebTraderApp (RpcServer + uvicorn HTTP API @ 18001)

[run_e2e_test 编排器]  cleanup → 启动 bridge → 注入 csv → 等消化 → 调 reconcile

[reconcile_trades]  读 sim_QMT_SIM.db + position.csv + transaction.csv
   └ 三维对账：市值占比（主） / 持仓股数（参考） / 拒单原因
```

## 2. 文件清单

| 文件                          | 职责                                                    |
| ----------------------------- | ------------------------------------------------------- |
| `test_setting.json`           | 全局配置（CSV 路径 / 凭证 / Redis / MySQL / sim / 端口）|
| `csv_to_redis_replay.py`      | CSV 成交流水 → Redis Stream 注入                        |
| `reconcile_trades.py`         | 三维对账（市值占比 + 股数 + 拒单）                      |
| `run_sim_e2e.py`              | 命令行启动器：sim 网关 + 策略 + WebTrader（无 GUI）     |
| `run_e2e_test.py`             | 端到端编排器（清理→bridge→注入→等待→对账）              |
| `purge_test_strategy.py`      | 清残留（mysql / redis stream / sim db / 端口扫描）      |

## 3. 配置

[test_setting.json](test_setting.json) 关键字段：

```json
{
  "strategy_name": "etf_intra_test",     // 三处一致：stream_key / target_stg / vnpy strategy_name
  "initial_capital": 1000000.0,           // sim 起始资金（应 ≥ 单笔最大金额，dry-run 会打印推荐值）

  "csv": {
    "transaction_path": "...",            // 历史成交流水
    "position_path":   "..."              // 历史持仓快照（最后一日用于对账）
  },

  "redis": {
    "host": "...", "password": "REPLACE_ME",
    "stream_key": "etf_intra_test",       // = strategy_name
    "trim_before_replay": true            // XTRIM 清空旧消息
  },

  "replay": {
    "rebase_remark_to_today": false,      // false=保留原始日期走回放控制器
    "date_range": ["2026-04-15", "2026-04-29"],   // 只回放该范围信号（merged_parquet 覆盖区）
    "idle_settle_seconds": 30             // 静默期触发最后一日 settle
  },

  "mysql": {
    "host": "...", "password": "REPLACE_ME",
    "purge_before_replay": true           // 清 stock_trade WHERE stg=etf_intra_test
  },

  "sim": {
    "account_id": "QMT_SIM",
    "db_dir": "D:/vnpy_data/state",       // sim_QMT_SIM.db 位置
    "delete_db_before_replay": false,
    "connect_setting": {
      "模拟资金": 1000000.0,
      "行情源": "merged_parquet",
      "merged_parquet_merged_root": "D:/vnpy_data/snapshots/merged",
      "merged_parquet_reference_kind": "today_open",
      "merged_parquet_fallback_days": 10
    }
  },

  "webtrader": {
    "enable": true,
    "rep_address": "tcp://127.0.0.1:12014",   // +10000 偏移，避免抢生产 webtrader (2014)
    "pub_address": "tcp://127.0.0.1:14102",
    "http_port": "18001"                       // = 8001 + 10000
  },

  "reconcile": {
    "volume_tolerance": 100,                  // 股数维度容差（参考）
    "ratio_tolerance": 0.01,                  // 市值占比维度容差 ±1%（主判定）
    "output_dir": "..."
  },

  "wait": { "max_seconds": 120, "poll_interval": 2 }
}
```

## 4. 密码处理（脱敏惯例）

仓库里 `test_setting.json` 模板含 `REPLACE_ME` 占位符。本地真实密码用：

```powershell
# 复制模板为 .local.json，里面填真实密码（已加 .gitignore）
copy test_setting.json test_setting.local.json /Y
# 编辑 .local.json 填真实密码，然后启动时指向它
F:/Program_Home/vnpy/python.exe ... --config <path>/test_setting.local.json
```

或不动文件名，让 git 忽略你的本地修改：

```powershell
git update-index --skip-worktree vnpy_signal_strategy_plus/test/test_setting.json
git update-index --skip-worktree vnpy_signal_strategy_plus/scripts/redis_bridge_setting.json
```

## 5. 端到端运行（标准流程）

### 5.1 准备

1. 在 `test_setting.json` 填真实密码（或用 `.local.json` 副本）。
2. 准备 sim 行情数据（`D:/vnpy_data/snapshots/merged/daily_merged_*.parquet`）。
3. 确认 `csv.transaction_path` / `csv.position_path` 文件存在。
4. （可选）跑 `purge_test_strategy.py` 清残留。

### 5.2 启动 sim 端

**终端 1**（保持运行）：

```powershell
F:/Program_Home/vnpy/python.exe `
  F:\Quant\vnpy\vnpy_strategy_dev\vnpy_signal_strategy_plus\test\run_sim_e2e.py `
  --config F:\Quant\vnpy\vnpy_strategy_dev\vnpy_signal_strategy_plus\test\test_setting.json
```

启动成功后日志末尾显示：

```
[boot] WebEngine RpcServer 启动 REP=tcp://127.0.0.1:12014 PUB=tcp://127.0.0.1:14102
[boot] uvicorn 子进程 pid=NNN -> http://127.0.0.1:18001/docs
[main] 主循环就绪；按 Ctrl+C 关停
```

如果 `replay.date_range` 设置了范围，还会看到：

```
[seed] 用 2026-04-14 的快照引导 sim 持仓 (回放起点=2026-04-15)
[seed] 注入 14 个持仓; 占用资金 997,248; 剩余 capital=2,752; OMS 已同步
```

可选 `--no-webtrader` 跳过 WebTrader 启动（纯后台 sim+strategy）。

### 5.3 跑端到端编排器

**终端 2**：

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.test.run_e2e_test `
    --config vnpy_signal_strategy_plus/test/test_setting.json
```

执行步骤：

1. 前置检查（mysql / redis / sim db）
2. 清理：`DELETE FROM stock_trade WHERE stg=etf_intra_test`、`XTRIM <stream> MAXLEN 0`
3. 启动 bridge subprocess（stdout 重定向到独立日志，避免 PIPE 阻塞）
4. `csv_to_redis_replay.replay()` 注入信号到 Redis Stream
5. 轮询等待 `stock_trade.processed=1` 比例（默认最多 120 秒）
6. 停止 bridge subprocess
7. `reconcile_trades.reconcile()` 输出三份 CSV 报告

完成后 stdout 显示对账结果，例如：

```
[reconcile-pos-ratio] 市值占比维度 19 个标的，1 FAIL (容差=±1.0%) -> FAIL
sim_total=1,004,206 csv_total≈1,090,196
[E2E] FAIL 市值占比维度有 1 个标的超容差 1.0%
```

### 5.4 查看输出

```
vnpy_signal_strategy_plus/test/output/
├── reconcile_position_ratio.csv    # 主对账：市值占比维度
├── reconcile_position.csv          # 参考：股数维度
├── reconcile_trades.csv            # 成交流水按 (date,symbol,dir) 聚合
├── reconcile_rejects.csv           # 拒单原因汇总（如有）
└── bridge_setting_e2e.json         # 编排器派生的 bridge 临时配置
```

### 5.5 通过 WebTrader HTTP API 实时看业务数据

```bash
# 拿 token（默认账号 vnpy/vnpy）
TOKEN=$(curl -s -X POST http://127.0.0.1:18001/api/v1/token \
  -d "username=vnpy&password=vnpy&grant_type=password" | jq -r .access_token)

curl -s http://127.0.0.1:18001/api/v1/account   -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:18001/api/v1/position  -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:18001/api/v1/trade     -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:18001/api/v1/order     -H "Authorization: Bearer $TOKEN"
```

或浏览器打开 http://127.0.0.1:18001/docs 看 Swagger UI。

## 6. 工具脚本

### 6.1 dry-run 看 csv 解析（不发 redis）

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.test.csv_to_redis_replay `
    --config vnpy_signal_strategy_plus/test/test_setting.json --dry-run
```

输出包含：
- 解析行数 / 跳过统计
- 单笔最大金额 + 推荐 `initial_capital`
- 前 3 条 payload 预览
- 是否启用 `rebase_remark_to_today`

### 6.2 单独跑对账（不重新注入）

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.test.reconcile_trades `
    --config vnpy_signal_strategy_plus/test/test_setting.json
```

### 6.3 清残留

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.test.purge_test_strategy
```

会清：mysql `stock_trade` 中 `stg=etf_intra_test` 的行 + redis stream + sim db 文件
+ 扫描端口占用给警告（不主动杀进程）。

### 6.4 编排器子选项

```powershell
# 只跑对账（已有 sim db 数据）
... run_e2e_test --reconcile-only

# 跳过 cleanup（保留 mysql/redis 残留）
... run_e2e_test --skip-cleanup

# 跳过 bridge 启动（你已在另一终端手动跑了 bridge）
... run_e2e_test --skip-bridge
```

## 7. 回放控制器与持仓引导

[etf_intra_test_strategy.py](../strategies/etf_intra_test_strategy.py) 重写了
`run_polling`，作为按 `remark` 升序的虚拟交易日回放控制器：

- 启动时 `gateway.enable_auto_settle(False)`，禁用自然日 settle
- 每条信号前 `md.refresh_tick(vt, as_of_date=sig_day)`
- 设置 `td.counter._replay_now = sig.remark`（trade.datetime 用回放时间）
- 跨日时 `td.counter.settle_end_of_day(prev_day)` 让 yd_volume 滚动 → SELL 解锁
- 静默期到达后 settle 最后一日

**持仓引导（窄 date_range 专用）**：
`on_init` 时如果 `replay.date_range` 设置了范围，自动从 `csv.position_path` 读
date_range[0] 前一日的持仓快照，注入 sim 的 14 个 LONG 持仓 + 扣减相应现金 + 同步
OMS。这样 sim 4/29 累计 = csv 4/14 起始 + 4/15-29 增量 = csv 4/29 终态。

**资金口径修正**：seed 后 sim 现金被扣到几千元，策略原本用 `account.balance`（现金）
算 vol_int 会全部 0 股不下单。本类 override `get_account_asset` 返回**总权益** =
balance + 持仓市值，与 CSV pct 的 equity_total 口径对齐。

## 8. 对账维度

### 8.1 市值占比（主判定）

绕过 fallback 价导致的股数失真。CSV 占比从 `position.csv` 的"仓位占比"列读，sim
占比 = `volume * price / (sum(market_value) + cash)`。容差 `ratio_tolerance` 默认
±1%。

### 8.2 持仓股数（参考）

只在行情数据精确（`merged_parquet` 直接覆盖）时才能 PASS。窄 date_range 内只会
有少量 PASS，宽 date_range 几乎全 FAIL。

### 8.3 拒单原因

汇总 `sim_orders` 中 `status='REJECTED'` 的 `status_msg`。回放模式下应该 0 拒单
（T+1 settle 让 SELL 单成功）；如果有拒单，前 10 行会打到 stdout 帮定位。

## 9. 端口约定

测试用端口比生产 WebTrader 默认 +10000 偏移，避免抢占：

| 用途         | 测试 sim_e2e | 生产 webtrader |
| ------------ | ------------ | -------------- |
| ZMQ REQ/REP  | **12014**    | 2014           |
| ZMQ SUB/PUB  | **14102**    | 4102           |
| HTTP REST/WS | **18001**    | 8001           |

uvicorn 子进程通过环境变量 `VNPY_WEB_REQ_ADDRESS` / `VNPY_WEB_SUB_ADDRESS` 接收
测试端口，不读 `.vntrader/web_trader_setting.json`，所以不会污染生产配置。

## 10. 已知限制

- **行情数据范围**：`merged_parquet` 默认目录只覆盖最近几个交易日。回放期超出该
  范围时 sim 退化用合成 tick（last=10.0），股数会失真但市值占比维度仍正确（前提
  是 seed 持仓引导生效）。
- **完整跨日精确对账**：需要写 `qlib_bar_source` 适配器，让 sim 用
  `D:/vnpy_data/qlib_data_bin/`（已覆盖 2025-08~2026-04）作为行情源。属后续工程。
- **mysql_signal_strategy.py 的 process_signal 资金口径**：父类原生用
  `account.balance` 算 vol_int；测试场景下 `EtfIntraTestStrategy.get_account_asset`
  override 返回总权益。生产策略保持原行为不受影响。

## 11. 故障排查

### 11.1 sim_e2e 启动 30 秒后看不到 "主循环就绪"

最常见：12014/14102/18001 端口被旧进程占用。

```powershell
# 看占用
foreach ($p in 12014, 14102, 18001) {
    $r = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($r) { Write-Output "  $p PID=$($r.OwningProcess)" } else { Write-Output "  $p free" }
}

# 强杀所有 vnpy python
Get-Process python | Where-Object { $_.Path -like '*F:\Program_Home\vnpy*' } |
    Stop-Process -Force
```

或直接跑 `purge_test_strategy.py` 看端口占用警告。

### 11.2 sim db lock 残留

```
QMT_SIM | 持久化初始化失败: 账户 'QMT_SIM' 的持久化文件已被另一进程占用
```

```powershell
Remove-Item D:\vnpy_data\state\sim_QMT_SIM*.* -Force
```

### 11.3 e2e_test 等待超时（120s）

bridge 写 mysql 速度慢（远程单条 commit ~300ms），169 笔信号大概需要 50~80 秒。
窄 date_range 19 笔信号大概 10 秒。如果超时：

- 看 `logs/redis_bridge_e2e/bridge_*.log` 确认 bridge 是否还在写
- 检查 mysql 是否被防火墙限速 / 锁

调大 `wait.max_seconds` 或缩小 `replay.date_range`。

### 11.4 strategy 日志显示 "下单数量为 0"

```
[etf_intra_test] 账户总资产(权益口径): 2752.0
[etf_intra_test] 下单数量为 0 (计算后: 0)，忽略信号: 0.096696
```

资金口径问题。如果**没有**走持仓引导（`date_range=null`），sim 起始 capital 应该
跟 `initial_capital` 一致；如果走了引导，应该看到 `[equity] cash=... + positions_mv=...
= equity=...` 日志，equity 应≈ `initial_capital`。如果 equity 错得离谱，检查：

- seed 是否成功（`[seed] 注入 N 个持仓` 日志）
- OMS 是否同步（`[seed] OMS 已同步`）
- `_get_sim_gateway` 是否找到 sim 网关

### 11.5 reconcile 报 "position.csv parser error"

聚宽导出的 position.csv 持仓行 17 列、header 16 列。代码已用
`skiprows=1 + names=POSITION_COLS17` 显式 17 列解析。如果用了别的导出格式，
需要在 [reconcile_trades.py](reconcile_trades.py#L13) 调整 `POSITION_COLS17`。

## 12. 相关文档

- [`../scripts/README.md`](../scripts/README.md) - 生产 Redis→MySQL bridge
- [`../mysql_signal_strategy.py`](../mysql_signal_strategy.py) - 策略基类（process_signal / run_polling 实盘版）
- 主仓 [run_sim.py](../../run_sim.py) - 生产 GUI 启动器（vs 本目录的无 GUI run_sim_e2e.py）
