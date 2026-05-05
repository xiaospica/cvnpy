# Redis→MySQL 信号中转 Bridge

常驻进程：实时消费 Redis Stream 上的下单信号，写入 MySQL `stock_trade` 表，供
`vnpy_signal_strategy_plus` 中基于 MySQL 轮询的策略消费。

## 1. 架构

```
[策略发布端]                  [bridge 进程]                  [vnpy 主进程]
  生产策略    --xadd-->  Redis Stream  --xreadgroup-->  bridge  --INSERT-->  MySQL stock_trade
                              ^                              |
                              |<------- xack(成功) ---------|
                              ↓ 失败留 PEL 重试
                                                                                ↑
                                                 [vnpy 主进程] mysql_signal_strategy_plus
                                                                每 50ms 轮询 stock_trade
                                                                processed=False AND stg=策略名
                                                                → process_signal → send_order
```

**关键性质**：

- **写库时序**：`INSERT → commit → xack` 严格顺序。MySQL 写失败则不 ack，消息留
  在 Redis Consumer Group 的 PEL，下次重启或下次消费由 bridge 端补偿。
- **多策略并发**：每个 `subscription` 一个守护线程，独立 `xreadgroup` 阻塞拉取。
- **字段透传**：Redis JSON 的 `code`（如 `518880.SH`）直接写入 MySQL；策略层
  `convert_code_to_vnpy_type` 负责剥后缀转 vnpy 格式。
- **stg 字段覆盖**：用配置中的 `target_stg` 覆盖 payload 里的 `stg`，支持 Redis
  端策略名与 MySQL 端策略 key 解耦（迁移期常见）。

## 2. 配置文件

[redis_bridge_setting.json](redis_bridge_setting.json)：

```json
{
  "redis": {
    "host": "...", "port": 6888, "password": "REPLACE_ME", "db": 0,
    "consumer_group": "order_group",
    "consumer_name": "bridge-1",
    "block_ms": 60000,
    "count": 3
  },
  "mysql": {
    "host": "...", "port": 3306,
    "user": "root", "password": "REPLACE_ME", "db": "mysql"
  },
  "subscriptions": [
    { "stream_key": "etf_rotation_basic", "target_stg": "etf_rotation_basic" },
    { "stream_key": "mcap_v3",            "target_stg": "mcap-v3"           }
  ],
  "log": { "dir": "logs/redis_bridge", "level": "INFO" }
}
```

**生产环境密码处理**：模板里写 `REPLACE_ME`，本地复制为 `redis_bridge_setting.local.json`
填真实密码（已加 `.gitignore`）；启动时用 `--config` 指向 `.local.json`。

## 3. 信号字段映射

Redis Stream 的 `xadd` payload 是 dict（fields/values 全字符串）：

| Redis 字段 | MySQL 列            | 说明                                                |
| ---------- | ------------------- | --------------------------------------------------- |
| `code`     | `code`              | 透传（如 `518880.SH`）                              |
| `pct`      | `pct`               | 百分比（0~1 小数）                                  |
| `type`     | `type`              | `BUY_LST` / `SELL_LST` / `BUY_FIXED` / `SELL_FIXED` |
| `price`    | `price`             | 参考价（fallback）                                  |
| `stg`      | `stg`               | 用配置 `target_stg` 覆盖，不读 payload              |
| `remark`   | `remark` (DateTime) | `'YYYY-MM-DD HH:MM:SS'` 字符串解析                  |
| `amt`      | —                   | 当前 stock_trade 表无对应列，bridge 静默丢弃        |
| `empty`    | —                   | 同上                                                |

## 4. 启动

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.scripts.redis_to_mysql_bridge `
    --config vnpy_signal_strategy_plus/scripts/redis_bridge_setting.json
```

或绝对路径：

```powershell
F:/Program_Home/vnpy/python.exe `
  F:\Quant\vnpy\vnpy_strategy_dev\vnpy_signal_strategy_plus\scripts\redis_to_mysql_bridge.py `
  --config F:\Quant\vnpy\vnpy_strategy_dev\vnpy_signal_strategy_plus\scripts\redis_bridge_setting.json
```

**Dry-Run 模式**（不写 MySQL，只 log 收到的信号，用于排错/抓包）：

```powershell
F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.scripts.redis_to_mysql_bridge `
    --config <配置> --dry-run
```

## 5. 关停

按 `Ctrl+C`（SIGINT）或发 `SIGTERM`。bridge 已注册 signal handler，会：
1. 设置 stop event
2. 停止所有消费线程
3. 让出已 ack 的消息、未 ack 的留在 PEL（下次重启自动续）

```powershell
# 找进程并终止
Get-Process python | Where-Object { $_.CommandLine -like '*redis_to_mysql_bridge*' } |
    Stop-Process
```

## 6. 日志位置

- 默认：`logs/redis_bridge/bridge_YYYYMMDD.log`（按日期）
- 同时输出到 stdout
- 内容：每条信号 `recv id=... payload=...` + `[mysql] insert ok id=N` + `xack` 状态

## 7. 故障排查

### 7.1 MySQL 连不上

```
[mysql] insert 失败: (pymysql.err.OperationalError) (2003, ...)
```

- 检查 `mysql.host/port` 是否可达（`telnet host port` 或 `Test-NetConnection`）
- 检查密码（`mysql -h <host> -u <user> -p`）
- 防火墙白名单

bridge 行为：`commit` 失败 → `rollback` → 不 ack → 消息留 PEL，连接恢复后重消费。

### 7.2 Redis 连不上

```
[bridge] redis ping 失败: ConnectionError
```

bridge 启动失败直接退出。修复网络后重启。

### 7.3 消息积压

```bash
# 查 PEL（pending entries list）
redis-cli -h <host> -p <port> -a <pwd> XPENDING <stream> order_group
```

如果有大量 pending 但消费慢，原因可能是：
- MySQL 写入太慢（远程 + 单条 commit 大概 200~500ms）
- bridge 进程被卡住（看 log 末尾时间戳）

### 7.4 重启不丢消息

Redis Consumer Group 的语义保证：bridge 重启时未 ack 的消息会被新消费者重新读到
（用 `xreadgroup ... ">"` 自动从 PEL 拉新消息；用 `id="0"` 还能拿历史 PEL）。
本 bridge 默认 `id=">"`，**只消费新到的消息**；如需消费历史 PEL，重启时手动用：

```bash
redis-cli XAUTOCLAIM <stream> order_group bridge-1 0 0-0
```

### 7.5 stock_trade 表 schema 漂移

bridge 自带的 `Stock` ORM 与 [mysql_signal_strategy.py](../mysql_signal_strategy.py#L28)
中的 `Stock` 必须保持字段一致。表结构变更时**两处同步修改**，否则 INSERT 会因
列不匹配 SQLAlchemy 校验失败。

## 8. 相关文件

| 文件                         | 作用                                |
| ---------------------------- | ----------------------------------- |
| `redis_to_mysql_bridge.py`   | 主进程（multi-thread consumer）     |
| `redis_bridge_setting.json`  | 配置模板（密码占位 `REPLACE_ME`）   |
| `__init__.py`                | 包标记                              |

## 9. 测试链路（开发期参考）

如需在本地端到端验证 bridge 行为，参考 [`../test/`](../test/) 目录的测试套件——
那里有 csv→redis 注入器和 mysql/redis 残留清理工具。
