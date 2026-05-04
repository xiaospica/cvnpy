# A1 (B2 解耦) + P1-1 (trigger_time 校验) + P2-1 (实盘/模拟双轨) 实施计划

> **状态**：实施前评审稿。本文档把 [deployment_windows.md](deployment_windows.md) 中决议为优先的三条工作合并成一份**可直接动手**的清单。
> 决议依据见 [deployment_windows.md §6 待决议清单](deployment_windows.md)。

---

## 一、A1 / B2 — `vnpy_strategy_dev` 与 `mlearnweb.db` 解耦

### 0. 现状再确认

vnpy 端 [mlearnweb_writer.py](../vnpy_ml_strategy/mlearnweb_writer.py) 直接写 mlearnweb.db **三张表**：

| 表 | vnpy 写入函数 | mlearnweb 是否已能从 vnpy_webtrader 拉？ |
|---|---|---|
| `ml_metric_snapshots` | `write_replay_ml_metric_snapshot` | ✅ 已有 [`/api/v1/ml/strategies/{name}/metrics?days=30`](../vnpy_webtrader/routes_ml.py#L37) + mlearnweb [`ml_snapshot_loop`](../../code/qlib_strategy_dev/mlearnweb/backend/app/services/vnpy/ml_monitoring_service.py) + [`historical_metrics_sync_service`](../../code/qlib_strategy_dev/mlearnweb/backend/app/services/vnpy/historical_metrics_sync_service.py) |
| `ml_prediction_daily` | `write_replay_ml_metric_snapshot` (同上) | ✅ 已有 `/strategies/{name}/prediction/latest/summary` |
| `strategy_equity_snapshots` (source=`replay_settle`) | `write_replay_equity_snapshot` | ❌ 当前**没有** vnpy_webtrader endpoint，必须新增 |

⇒ B2 落地分两步：**Step 1 直接删冗余双写**；**Step 2 给 strategy_equity_snapshots 加 endpoint + 拉取 service**。

### 1. Step 1 — 删除已有 endpoint 覆盖的双写代码（XS）

**改动**：
- 删除 [mlearnweb_writer.py](../vnpy_ml_strategy/mlearnweb_writer.py) 的 `write_replay_ml_metric_snapshot` 函数。
- 删除 [template.py:1556-1600](../vnpy_ml_strategy/template.py#L1556) 调用 `write_replay_ml_metric_snapshot` 的代码块。
- 保留 `MLEngine.publish_metrics`（这条路是经 EventEngine + MetricsCache + vnpy_webtrader endpoint 暴露的，不动）。

**验证（无实盘需求）**：
1. 跑回放 30 天
2. mlearnweb 前端 metrics 历史曲线应该仍正常显示（来源是 ml_snapshot_loop 拉 + historical_metrics_sync_service）
3. 检查 mlearnweb.db 的 `ml_metric_snapshots` / `ml_prediction_daily` 行数与回放天数一致

**风险**：mlearnweb ml_snapshot_loop 是按 wall-clock 拉，回放期间它可能错过快速变化的 trade_date。但 [historical_metrics_sync_service.py:5-15](../../code/qlib_strategy_dev/mlearnweb/backend/app/services/vnpy/historical_metrics_sync_service.py#L5) 已声明它每 5 分钟回头拉 30 天历史比对 → 回放产生的历史值会被回填。等一个 sync 周期后再校验。

### 2. Step 2 — 新增 replay equity endpoint + mlearnweb fanout 拉取（M）

#### 2a. vnpy 端：本地 SQLite + WAL

**新文件** `vnpy_ml_strategy/replay_history.py`：

```python
"""本地回放权益历史 SQLite. 替代直接写 mlearnweb.db.

路径: $QS_DATA_ROOT/state/replay_history.db (默认 D:/vnpy_data/state/replay_history.db)

schema = mlearnweb.db.strategy_equity_snapshots 字段子集，由 vnpy_webtrader
endpoint 暴露给 mlearnweb，由 mlearnweb 端的 sync service UPSERT 到 mlearnweb.db.
"""
import os, sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_equity_snapshots (
    strategy_name TEXT NOT NULL,
    ts TEXT NOT NULL,            -- ISO datetime (回放逻辑日 15:00)
    strategy_value REAL NOT NULL,
    account_equity REAL NOT NULL,
    positions_count INTEGER NOT NULL DEFAULT 0,
    raw_variables_json TEXT,
    inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_name, ts)
);
CREATE INDEX IF NOT EXISTS idx_inserted_at ON replay_equity_snapshots(inserted_at);
"""

def _db_path() -> Path:
    root = Path(os.getenv("QS_DATA_ROOT", "D:/vnpy_data"))
    return root / "state" / "replay_history.db"

def write_snapshot(strategy_name, ts, strategy_value, ...):
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.executescript("PRAGMA journal_mode=WAL; " + SCHEMA)
    # UPSERT (PRIMARY KEY 冲突时 REPLACE)
    conn.execute("""INSERT OR REPLACE INTO replay_equity_snapshots ... """, ...)
    conn.commit(); conn.close()

def list_snapshots(strategy_name, since_iso=None, limit=10000):
    """vnpy_webtrader endpoint 调用入口."""
    ...
```

**改动**：
- [template.py:1417](../vnpy_ml_strategy/template.py#L1417) 把 `from .mlearnweb_writer import write_replay_equity_snapshot` 替换为 `from .replay_history import write_snapshot`。
- 删 `mlearnweb_writer.py` 整个文件（两个函数都搬走或废弃）。
- 删 `MLEARNWEB_DB` env 引用所有处。

#### 2b. vnpy_webtrader 端：新增 endpoint

**新增** [routes_ml.py](../vnpy_webtrader/routes_ml.py)：

```python
@router.get("/strategies/{name}/replay/equity_snapshots")
def ml_replay_equity_snapshots(
    name: str,
    since: Optional[str] = Query(None, description="ISO datetime, 仅返回 inserted_at >= since"),
    limit: int = Query(10000, ge=1, le=100000),
    access: bool = Depends(get_access),
) -> List[Dict[str, Any]]:
    """回放权益快照 (本地 replay_history.db).

    mlearnweb 端 replay_equity_sync_service 每 N 分钟轮询本端点，按 since 增量拉 +
    UPSERT 到 mlearnweb.db.strategy_equity_snapshots (source_label='replay_settle').
    """
    from vnpy_ml_strategy.replay_history import list_snapshots
    return list_snapshots(name, since_iso=since, limit=limit)
```

#### 2c. mlearnweb 端：新增 sync service

**新文件** `mlearnweb/backend/app/services/vnpy/replay_equity_sync_service.py`：

```python
"""每 5 分钟 fanout 拉 vnpy 节点的 replay equity snapshots, UPSERT 到本地 db.

设计照搬 historical_metrics_sync_service: 增量同步，无 INSERT 风暴。
"""
SYNC_POLL_INTERVAL_SECONDS = 300  # 5 min
SYNC_INCREMENTAL_MARKER = "since"  # 用本地 max(inserted_at) 作 since 增量

async def sync_one_node(client, node_id, strategies):
    for strat in strategies:
        # 本地最大 inserted_at
        since = session.query(func.max(StrategyEquitySnapshot.inserted_at))...
        # 远端拉
        rows = await client.per_node[node_id].get_replay_equity_snapshots(strat, since=since, limit=10000)
        for r in rows:
            session.execute(upsert_stmt(...))
```

**接入** [live_main.py](../../code/qlib_strategy_dev/mlearnweb/backend/app/live_main.py) lifespan，与现有 `snapshot_loop` 并列：
```python
asyncio.create_task(replay_equity_sync_loop())
```

#### 2d. 校验流（无实盘需求）

```bash
# 1. 端到端冒烟（vnpy + mlearnweb 同机）
F:\Program_Home\vnpy\python.exe run_ml_headless.py        # 触发回放
# 等回放跑完 (csi300 ~30 天约 ~1.5h，可临时把 replay_end 设短到 5 天)

# 2. 验证本地 SQLite 写入正确
sqlite3 D:/vnpy_data/state/replay_history.db "SELECT COUNT(*) FROM replay_equity_snapshots WHERE strategy_name='csi300_lgb_headless'"
# 期望: 与回放天数一致（5 / 30）

# 3. 验证 vnpy_webtrader endpoint
curl http://localhost:8001/api/v1/ml/strategies/csi300_lgb_headless/replay/equity_snapshots?limit=10 \
     -H "Authorization: Bearer <jwt>"
# 期望: 返回 5 / 30 条 JSON

# 4. 等 5 分钟让 replay_equity_sync_loop 跑一次，验证 mlearnweb.db
sqlite3 mlearnweb.db "SELECT COUNT(*) FROM strategy_equity_snapshots WHERE source_label='replay_settle' AND strategy_name='csi300_lgb_headless'"
# 期望: 与 vnpy 端一致

# 5. 跨机模拟（核心验收）
# vnpy 机：QS_DATA_ROOT=D:/vnpy_data, mlearnweb 不可达
# mlearnweb 机：vnpy_nodes.yaml 指向 vnpy 机的 8001 端口
# 验证：跨机也能拉到完整 replay equity 曲线 → B2 真正生效

# 6. 删 mlearnweb_writer.py 后 vnpy 启动不应该 import 失败
grep -r "mlearnweb_writer" F:/Quant/vnpy/vnpy_strategy_dev/  # 应该 0 行
```

#### 2e. 决策点 — Step 1 / Step 2 是否绑定一起做？

- **绑定（推荐）**：一次完成解耦，mlearnweb_writer.py 整个文件删干净
- **拆开**：先 Step 1（XS 风险）观察一周 → 再 Step 2

**倾向绑定**：Step 1 风险极小（只是删冗余写入，读取链路不变）。

---

## 二、P1-1 — 多策略 trigger_time 错峰校验

### 实施

**改 [run_ml_headless.py](../run_ml_headless.py)**:

1. 在 STRATEGIES 注释块加强约定（位置：[L132](../run_ml_headless.py#L132) 附近）:
```python
# ─── 策略列表 ──────────────────────────────────────────────────────────
# ⚠️  多策略 trigger_time 必须错开（推荐间隔 ≥10 分钟）
#     单策略推理峰值 4-5 GB；同 trigger_time 多策略并发会触发 swap / OOM
#     启动期 _validate_trigger_time_unique 会校验冲突直接 raise
#     escape hatch: 设环境变量 ALLOW_TRIGGER_TIME_COLLISION=1 跳过校验
#                   (仅在确认机器内存充足且实测过并发场景时使用)
```

2. 加校验函数 + 接入 `_validate_startup_config`:
```python
def _validate_trigger_time_unique() -> None:
    """启动期硬校验：避免多策略同 trigger_time 触发推理 OOM。

    escape hatch: env ALLOW_TRIGGER_TIME_COLLISION=1 跳过（仅限确认机器
    内存能扛得住的场景）。
    """
    if os.getenv("ALLOW_TRIGGER_TIME_COLLISION") == "1":
        print("[headless] WARN: ALLOW_TRIGGER_TIME_COLLISION=1, skipping trigger_time uniqueness check")
        return
    seen: dict[str, str] = {}
    for s in STRATEGIES:
        t = (s.get("setting_override") or {}).get("trigger_time") \
            or STRATEGY_BASE_SETTING.get("trigger_time", "21:00")
        if t in seen:
            raise ValueError(
                f"策略 {s['strategy_name']!r} 与 {seen[t]!r} 同 trigger_time={t!r}; "
                "推理峰值 4-5GB，多策略并发会 OOM。请错开 ≥10 min "
                "或设 env ALLOW_TRIGGER_TIME_COLLISION=1 跳过校验。"
            )
        seen[t] = s["strategy_name"]


def _validate_startup_config() -> None:
    # ... 已有代码 ...
    _validate_trigger_time_unique()    # ← 新增
```

3. 例配置（顺手在 STRATEGIES 给个错峰示例）：
```python
STRATEGIES = [
    {
        "strategy_name": "csi300_lgb_headless",
        ...
        "setting_override": {
            "bundle_dir": ...,
            "trigger_time": "21:00",      # ← 显式
            "topk": 7, "n_drop": 1,
        },
    },
    {
        "strategy_name": "csi300_lgb_headless_2",
        ...
        "setting_override": {
            "bundle_dir": ...,
            "trigger_time": "21:15",      # ← 错开 15 min
            "topk": 7, "n_drop": 1,
        },
    },
]
```

### 验收

```bash
# 1. 同 trigger_time 应 raise
F:\Program_Home\vnpy\python.exe run_ml_headless.py
# 期望: ValueError "csi300_lgb_headless 与 csi300_lgb_headless_2 同 trigger_time='21:00'..."

# 2. 错开后正常启动
# 改 STRATEGIES 让两个策略 trigger_time 不同
F:\Program_Home\vnpy\python.exe run_ml_headless.py
# 期望: 启动正常

# 3. escape hatch 生效
$env:ALLOW_TRIGGER_TIME_COLLISION="1"
F:\Program_Home\vnpy\python.exe run_ml_headless.py
# 期望: print warn + 跳过校验
```

---

## 三、P2-1 — 实盘 / 模拟双轨架构（信号同步）

### 1. 架构设计

**目标**：同进程同时跑 1 条实盘策略（gateway=`QMT`）+ N 条模拟策略（gateway=`QMT_SIM_*`），两者**复用同一份信号产出**（`selections.parquet`），仅撮合走不同 gateway，权益曲线在 mlearnweb 各自呈现。

**改造点（4 处）**：

#### 1.1. [run_ml_headless.py](../run_ml_headless.py) 删 `USE_GATEWAY_KIND` 单选 → 每条 GATEWAYS 自带 `kind`

```python
# 旧
USE_GATEWAY_KIND = "QMT_SIM"
GATEWAYS = [{"name": "QMT_SIM_csi300", "setting": ...}]

# 新
GATEWAYS = [
    {"kind": "live", "name": "QMT",                  "setting": QMT_SETTING},
    {"kind": "sim",  "name": "QMT_SIM_csi300_paper", "setting": QMT_SIM_BASE_SETTING},
    {"kind": "sim",  "name": "QMT_SIM_csi300_shadow", "setting": QMT_SIM_BASE_SETTING},
]

# main() 里
for gw in GATEWAYS:
    if gw["kind"] == "live":
        from vnpy_qmt import QmtGateway as _Cls
    elif gw["kind"] == "sim":
        from vnpy_qmt_sim import QmtSimGateway as _Cls
    else:
        raise ValueError(f"unknown gateway kind: {gw['kind']!r}")
    main_engine.add_gateway(_Cls, gateway_name=gw["name"])
```

#### 1.2. `_validate_startup_config` 改成按每条 gateway 各自的 `kind` 校验

```python
def _validate_startup_config() -> None:
    from vnpy_common.naming import validate_gateway_name

    # 实盘 gateway 至多 1 条 (miniqmt 单进程单账户约束)
    n_live = sum(1 for g in GATEWAYS if g["kind"] == "live")
    if n_live > 1:
        raise ValueError(f"GATEWAYS 含 {n_live} 个 live gateway，miniqmt 单进程单账户约束只允许 1 个")

    gw_names: set[str] = set()
    for gw in GATEWAYS:
        validate_gateway_name(gw["name"], expected_class=gw["kind"])
        if gw["name"] in gw_names:
            raise ValueError(f"GATEWAYS 中 name={gw['name']!r} 重复")
        gw_names.add(gw["name"])

    # 策略 gateway_name 必须存在
    for s in STRATEGIES:
        if s["gateway_name"] not in gw_names:
            raise ValueError(f"策略 {s['strategy_name']!r} 引用了未注册的 gateway_name={s['gateway_name']!r}")

    _validate_trigger_time_unique()
```

#### 1.3. `signal_source_strategy` 共享信号

**改 [template.py](../vnpy_ml_strategy/template.py)** 加 setting：

```python
parameters = [..., "signal_source_strategy"]   # 新增
signal_source_strategy: str = ""               # 默认空 = 自己跑推理

def run_daily_pipeline(self, as_of_date=None):
    today = as_of_date or date.today()
    if self.signal_source_strategy:
        # 影子策略：复用上游 selections.parquet，跳过推理 subprocess
        return self._copy_selections_from_upstream(today)
    return self._run_own_inference(today)

def _copy_selections_from_upstream(self, today):
    """硬链接（NTFS hardlink）上游策略的 selections.parquet 到本策略 output_root.
    
    用 hardlink 而非 copy:
    - 零额外存储
    - 上游产物覆盖 = 影子产物自动同步（同 inode）
    - 单机 same-volume 才能 hardlink；跨盘场景 fallback 到 shutil.copy2
    """
    src_dir = Path(self.output_root) / self.signal_source_strategy / today.strftime("%Y%m%d")
    dst_dir = Path(self.output_root) / self.strategy_name / today.strftime("%Y%m%d")
    dst_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("selections.parquet", "predictions.parquet", "diagnostics.json", "metrics.json"):
        src = src_dir / fname
        if not src.exists():
            continue
        dst = dst_dir / fname
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)         # NTFS hardlink
        except OSError:
            shutil.copy2(src, dst)
    self.write_log(f"[shadow] linked selections from {self.signal_source_strategy}")
```

**约束**：影子策略的 `bundle_dir` / `topk` / `n_drop` / `trigger_time` 等必须与上游一致 — 启动期校验：

```python
def _validate_signal_source_consistency() -> None:
    """影子策略与上游必须 bundle_dir/topk/n_drop 一致 (否则信号语义错位)."""
    by_name = {s["strategy_name"]: s for s in STRATEGIES}
    for s in STRATEGIES:
        sso = s["setting_override"].get("signal_source_strategy") or ""
        if not sso:
            continue
        if sso not in by_name:
            raise ValueError(f"策略 {s['strategy_name']!r} signal_source_strategy={sso!r} 不存在")
        upstream = by_name[sso]
        for f in ("bundle_dir", "topk", "n_drop"):
            if s["setting_override"].get(f) != upstream["setting_override"].get(f):
                raise ValueError(
                    f"影子策略 {s['strategy_name']!r} 的 {f} 与上游 {sso!r} 不一致 — 信号会错位"
                )
        # trigger_time 不要冲突，因为影子不跑推理（不会争内存），但 rebalance 仍会发单
```

#### 1.4. mlearnweb 前端 mode badge（plan Phase 3 已设计）

按 plan Phase 3A 落地：strategy summary 加 `mode: "live"|"sim"` + `gateway_name` 字段；前端 LiveTradingPage 卡片 Badge + 详情页 Banner。

---

### 2. ⚠️ 无实盘环境的验收方案（用户核心顾虑）

**风险类别**（双轨架构可能出错的地方）：

| 风险 | 描述 | 严重度 |
|---|---|---|
| R1 | 多 Gateway 同 EventEngine 互相干扰（事件串味、订阅冲突） | 高 |
| R2 | `send_order` 路由错误 — 实盘策略发的单跑到模拟 gateway 或反之 | 致命 |
| R3 | `on_order` / `on_trade` 回报路由错误 — 持仓 / 权益落到错的策略 | 致命 |
| R4 | 影子策略 `selections.parquet` 与上游不严格等价 | 高 |
| R5 | 实盘 gateway 连接失败时，影响模拟 gateway 启动顺序 | 中 |
| R6 | mlearnweb 前端两条曲线混淆 / mode badge 错 | 低 |

**没有真实 miniqmt 账户怎么办？三层验证策略：**

#### 验收层 V1 · 双 SIM Gateway 验证多 Gateway 架构（无实盘最关键的）

**目标**：用两个 sim gateway 替代 live + sim，验证 R1/R2/R3 — 因为多 Gateway 路由逻辑与 gateway 类型无关，sim+sim 能跑通就证明 live+sim 也能跑通。

```python
# tests/test_dual_gateway_routing.py (新)
GATEWAYS = [
    {"kind": "sim", "name": "QMT_SIM_a", "setting": ...},
    {"kind": "sim", "name": "QMT_SIM_b", "setting": ...},
]
STRATEGIES = [
    {"name": "strat_a", "gateway_name": "QMT_SIM_a", "bundle_dir": bundle_X},
    {"name": "strat_b", "gateway_name": "QMT_SIM_b", "bundle_dir": bundle_Y},
]
# 启动 + 触发 strat_a 回放
# 断言:
#   sim_QMT_SIM_a.db trades 表 ≥ 1 行 (strat_a 的)
#   sim_QMT_SIM_b.db trades 表 == 0 行 (strat_b 没动)
#   strat_a 的 reference 字段 == "strat_a:..." (B 的不应该出现)
```

✅ 验证 R1/R2/R3 — 通过后说明多 Gateway 路由架构本身正确。

#### 验收层 V2 · `FakeQmtGateway` 替身（验证命名 validator + start-up 流）

**目标**：让 GATEWAYS 配置成 `[live, sim, sim]` 但不实际接 miniqmt，验证启动流程与命名校验都过。

**新文件** `tests/fakes/fake_qmt_gateway.py`：

```python
"""零实盘环境下的 QmtGateway 替身.

挂着 'QMT' 名字（命名约定 → live），内部撮合复用 QmtSimGateway.
专用 vnpy_qmt 包未安装 / 用户没有 miniqmt 账户的研发机.

⚠️ 部署机不应该有此文件 — 通过 deploy/install_services.ps1 跳过 tests/.
"""
from vnpy_qmt_sim.gateway import QmtSimGateway

class FakeQmtGateway(QmtSimGateway):
    """冒充 QmtGateway 接口的模拟柜台.

    对外 default_name='QMT' 让 vnpy_common.naming.classify_gateway 识别为 live;
    内部撮合复用 QmtSimGateway → 没有真实下单风险.
    """
    default_name = "QMT"

    def __init__(self, event_engine, gateway_name: str = "QMT"):
        # 跳过 QmtSimGateway 自己的 validate_gateway_name 校验（它要求 QMT_SIM_* 前缀）
        # 直接调祖父类
        from vnpy.trader.gateway import BaseGateway
        BaseGateway.__init__(self, event_engine, gateway_name)
        # 复用 QmtSimGateway 其余初始化逻辑
        self._sim_init_minus_name_validation()

    def _sim_init_minus_name_validation(self):
        # ... 复制 QmtSimGateway.__init__ 后半段到这里
        pass
```

**测试用例** `tests/test_dual_track_with_fake_live.py`：

```python
GATEWAYS = [
    {"kind": "live", "name": "QMT",                   "setting": ...},   # FakeQmt
    {"kind": "sim",  "name": "QMT_SIM_csi300_shadow", "setting": ...},
]
STRATEGIES = [
    {"name": "csi300_live",   "gateway_name": "QMT",                   "bundle_dir": bundle_X},
    {"name": "csi300_shadow", "gateway_name": "QMT_SIM_csi300_shadow", "bundle_dir": bundle_X,
     "signal_source_strategy": "csi300_live"},
]
# 启动:
#   - validate_gateway_name("QMT", expected_class="live") OK
#   - validate_gateway_name("QMT_SIM_csi300_shadow", expected_class="sim") OK
# 触发回放:
#   - csi300_live 跑推理 → selections.parquet
#   - csi300_shadow 不跑推理（hardlink 上游 selections.parquet）
# 断言:
#   - csi300_live 与 csi300_shadow 的 selections.parquet **byte-equal**
#   - 两策略每日 sells/buys instrument 集合**严格相等**
#   - sim_QMT.db (FakeQmt) vs sim_QMT_SIM_csi300_shadow.db trades 表内容相似
#     (注意：撮合都走 QmtSimGateway 内部逻辑，行为应一致)
```

✅ 验证 R4（信号字节级等价）+ R6（mode 区分）

#### 验收层 V3 · 真券商仿真账户（**TODO 待测试**）

> ⏳ **状态：TODO 待测试**。用户已开通券商仿真账户，但仿真柜台**仅在交易时间段**(09:30-15:00 工作日)可用,本仓库当前在交易时段外验证 V1+V2 闭环;V3 留待下个交易日开盘后跑一次确认。

**目标**：用券商提供的 miniqmt 仿真账户（不是 vnpy_qmt_sim，是券商真盘 miniqmt 但账户是仿真的）跑真 QmtGateway，验证 connect / send_order 接口与实盘等价。

**前置**：用户已开通仿真账户（已就绪）;实施时需在交易时段进行。

**测试入口**：把 `GATEWAYS` 中 live 项的类从 `FakeQmtGateway` 换成真 `QmtGateway`,`QMT_SETTING` 填仿真账号 + 客户端路径。**这是开发完后的最后一公里验收**,V1+V2 已能覆盖 R1-R6 全部代码 bug 风险。

**TODO 验收清单**（盘中执行,记录结果到本文档表格）：

- [ ] V3.1 真 QmtGateway 在 connect 阶段成功 (gateway.connected=True)
- [ ] V3.2 send_order 经 RPC 发到券商,broker 回 OrderID 正常
- [ ] V3.3 on_order / on_trade 回报路由到 QlibMLStrategy 正确
- [ ] V3.4 双轨混部时 FakeQmt 路径与真 QmtGateway 路径互不干扰
- [ ] V3.5 mlearnweb 前端实盘 badge (live 红色) 正确显示

---

### 3. 完整验收清单（可执行）

```bash
# === V1 多 Gateway 路由 ===
F:/Program_Home/vnpy/python.exe -m pytest tests/test_dual_gateway_routing.py -v
# 期望: 5/5 passed

# === V2 FakeQmt 双轨 + 信号同步 ===
F:/Program_Home/vnpy/python.exe -m pytest tests/test_dual_track_with_fake_live.py -v
# 期望:
#   test_startup_with_live_and_sim PASSED
#   test_signal_source_byte_equal PASSED        ← 核心：影子 selections == 上游 selections
#   test_dual_isolation_db PASSED               ← sim_QMT.db vs sim_QMT_SIM_*.db 物理隔离
#   test_validate_gateway_naming_dual PASSED

# === E2E 端到端 ===
# 改 run_ml_headless.py STRATEGIES：双轨配置（FakeQmt + sim shadow，bundle 同）
F:/Program_Home/vnpy/python.exe run_ml_headless.py
# 期望: 启动正常，回放期间两策略权益曲线
# 浏览器 http://localhost:5173/live-trading
# 期望: 看到两条曲线，badges 区分 live (红) / sim (绿)；走势接近但不完全 bit-equal

# === 信号同步硬验证 ===
# 回放跑完后 diff 两个策略的 selections.parquet:
python -c "
import pandas as pd, hashlib
days = ['20260102', '20260105', ...]
for d in days:
    a = open(f'D:/ml_output/csi300_live/{d}/selections.parquet', 'rb').read()
    b = open(f'D:/ml_output/csi300_shadow/{d}/selections.parquet', 'rb').read()
    print(d, 'EQUAL' if a == b else 'DIFFER (BUG)')
"
# 期望: 全部 EQUAL

# === V3 (TODO 待测试 — 需在交易时段 09:30-15:00 跑) ===
# 把 GATEWAYS 中 live 项的类从 FakeQmtGateway 换成真 QmtGateway
# QMT_SETTING 填券商仿真账号 + 客户端路径
# F:/Program_Home/vnpy/python.exe run_ml_headless.py
# 走真 miniqmt RPC，验证 connect + send_order 链路 (盘中)
```

---

### 4. 实施次序（推荐 commit 划分）

| Commit | 内容 | 估时 |
|---|---|---|
| 1 | A1 Step 1 — 删 `write_replay_ml_metric_snapshot` 双写 + 验证 | 0.5d |
| 2 | A1 Step 2a — `replay_history.py` 本地 SQLite + template.py 切过去 | 0.5d |
| 3 | A1 Step 2b — vnpy_webtrader endpoint + tests | 0.5d |
| 4 | A1 Step 2c — mlearnweb sync service + lifespan 接入 + tests | 0.5d |
| 5 | A1 Step 2d — 端到端 E2E 验证 + 删 mlearnweb_writer.py | 0.5d |
| 6 | P1-1 — `_validate_trigger_time_unique` + escape hatch + 单测 | 0.5d |
| 7 | P2-1.1 — `USE_GATEWAY_KIND` 解开 + GATEWAYS kind 字段 + V1 测试 | 0.5d |
| 8 | P2-1.2 — `signal_source_strategy` 实现 + 一致性校验 + V2 测试 | 1d |
| 9 | P2-1.3 — `FakeQmtGateway` 替身 + tests/fakes 目录 + 测试 | 0.5d |
| 10 | mlearnweb Phase 3 mode badge（plan 已有设计，照搬即可） | 1d |
| 11 | 整体 E2E 双轨 + 文档更新 | 0.5d |

**总计 ~6.5 工作日**。前 5 个 commit (A1) 可独立交付；P2-1 5 个 commit 需要按顺序。

---

## 四、决策点摘要

请逐条 ✅ / ❌ 决议：

- [ ] **A1.1**：Step 1 与 Step 2 绑定一起做？还是先 Step 1 观察 1 周？
- [ ] **A1.2**：`replay_history.db` 路径 `D:/vnpy_data/state/replay_history.db` OK？
- [ ] **A1.3**：mlearnweb sync service 5 分钟轮询周期 OK？是否要加手动 trigger endpoint？
- [ ] **P1-1**：escape hatch env 名 `ALLOW_TRIGGER_TIME_COLLISION=1` OK？
- [ ] **P2-1.A**：`signal_source_strategy` 用 NTFS hardlink；跨盘 fallback 到 copy2 — 接受？
- [ ] **P2-1.B**：是否实施 `FakeQmtGateway`？（不用它则放弃 V2 层验证，仅靠 V1 双 sim）
- [x] **P2-1.C**：~~用户后续会去券商开通仿真账户跑 V3 吗？~~ → **已开通**,但仿真柜台仅交易时段可用,V3 留 TODO 待下一交易日盘中跑(详见 §三.2 V3 章节)
- [ ] **commit 划分顺序**：按上面 11 个 commit 顺序还是合并？

---

## 五、附录：关键文件改动一览

### 删除 / 缩减
| 文件 | 处置 |
|---|---|
| [vnpy_ml_strategy/mlearnweb_writer.py](../vnpy_ml_strategy/mlearnweb_writer.py) | 整个删除 |

### 新增
| 文件 | 内容 |
|---|---|
| `vnpy_ml_strategy/replay_history.py` | 本地 replay_history.db 读写 |
| `tests/fakes/fake_qmt_gateway.py` | FakeQmtGateway (双轨 V2 验证) |
| `tests/test_dual_gateway_routing.py` | 双 sim gateway 路由测试 (V1) |
| `tests/test_dual_track_with_fake_live.py` | live + sim 双轨 + 信号同步 (V2) |
| `mlearnweb/backend/app/services/vnpy/replay_equity_sync_service.py` | mlearnweb fanout 拉 replay equity |

### 改动
| 文件 | 改动 |
|---|---|
| [run_ml_headless.py](../run_ml_headless.py) | 删 USE_GATEWAY_KIND；GATEWAYS 加 kind；加 trigger_time 校验 + signal_source_strategy 一致性校验 |
| [vnpy_ml_strategy/template.py](../vnpy_ml_strategy/template.py) | 加 signal_source_strategy 参数 + `_copy_selections_from_upstream` 实现；切 mlearnweb_writer → replay_history |
| [vnpy_webtrader/routes_ml.py](../vnpy_webtrader/routes_ml.py) | 加 `/strategies/{name}/replay/equity_snapshots` endpoint |
| [vnpy_webtrader/strategy_adapter.py](../vnpy_webtrader/strategy_adapter.py) | 暴露 `gateway` 字段（plan Phase 3 已规划） |
| `mlearnweb/backend/app/live_main.py` | lifespan 接入 replay_equity_sync_loop |
| `mlearnweb/backend/app/services/vnpy/client.py` | 加 `get_replay_equity_snapshots` 方法 |
| `mlearnweb/backend/app/schemas/schemas.py` | StrategySummary 加 mode + gateway_name |

