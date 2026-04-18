# 架构说明

## 1. 设计目标与约束

`vnpy_webtrader` 的目标是在**不侵入交易逻辑**的前提下,把一台 vnpy 交易进程的能力暴露给外部调用者,并满足:

| 目标 | 落地手段 |
|---|---|
| 交易进程低延迟 | 交易逻辑线程与 Web I/O 线程物理隔离 (两进程) |
| 安全对外 | Web 进程做 JWT 鉴权, 不直接暴露交易进程 |
| 多策略引擎支持 | `StrategyEngineAdapter` 抽象层, 按 `APP_NAME` 注册 |
| 实时事件推送 | ZeroMQ PUB/SUB + WebSocket, 无需前端轮询 |
| 单机自治可用 | 不依赖聚合层也能独立运行 (直接用浏览器访问) |
| 易扩展 | 新路由/新引擎 Adapter 都是插件式增量, 核心不改 |

---

## 2. 上下文视图 (谁调我, 我调谁)

```mermaid
flowchart TB
    subgraph External[外部]
        Browser[浏览器 / 前端 SPA]
        Agg[vnpy_aggregator 聚合层]
        Script[运维脚本 / curl / httpx]
    end

    subgraph Node[节点主机]
        subgraph WebProc[Web 进程<br/>uvicorn + FastAPI]
            FastAPI[FastAPI app]
            Routes[routes_*<br/>REST Handlers]
            WS[WebSocket Hub]
            RpcCli[RpcClient<br/>ZeroMQ REQ+SUB]
        end

        subgraph TradeProc[交易进程<br/>Python MainEngine]
            MainEngine[MainEngine]
            WebEngine[WebEngine<br/>RpcServer]
            StrategyEngines[策略引擎池<br/>Cta / SignalPlus / ...]
            Gateways[Gateway 池<br/>QMT / CTP / ...]
            EventEngine[EventEngine]
        end
    end

    Browser -- HTTPS REST --> FastAPI
    Browser -- WSS --> WS
    Agg -- HTTPS REST/WS --> FastAPI
    Script -- HTTP/S REST --> FastAPI

    FastAPI -- in-proc call --> Routes
    Routes --> RpcCli
    WS --> RpcCli
    RpcCli -- ZeroMQ REQ/REP --> WebEngine
    RpcCli -- ZeroMQ SUB/PUB --> WebEngine

    WebEngine -- 方法调用 --> MainEngine
    WebEngine -- 方法调用 --> StrategyEngines
    WebEngine -- event_engine.register --> EventEngine
    MainEngine --- Gateways
    MainEngine --- StrategyEngines
```

要点:

- **进程边界 = 安全边界**: 交易进程不开任何对外端口,所有外部流量先到 Web 进程。
- **RPC 是唯一耦合**: Web 进程只通过 RPC 拿到交易能力,不共享内存。
- **事件单向**: 交易进程 PUB → Web 进程 SUB, 不存在反向推送。

---

## 3. 进程模型

### 3.1 运行态

```mermaid
sequenceDiagram
    participant U as 用户 (Qt MainWindow)
    participant TW as WebManager Widget
    participant WE as WebEngine (in TradeProc)
    participant WP as Web 进程 (uvicorn)

    U->>TW: 点击"启动"
    TW->>WE: start_server(req_addr, sub_addr)
    WE->>WE: build_adapters(main_engine)
    WE->>WE: server.start(REP, PUB)
    TW->>WP: QProcess(uvicorn vnpy_webtrader.web:app)
    WP->>WP: @startup: RpcClient.start(req, sub)
    WP->>WE: list_strategy_engines() (RPC)
    WE-->>WP: [{app_name, event_type, ...}]
    WP->>WP: 构建 _STRATEGY_TOPIC_MAP
    Note over WP: Ready for REST/WS
```

### 3.2 拓扑

本工程实际运行态可能是下面三种之一:

| 场景 | 说明 | 适用 |
|---|---|---|
| **单机 GUI 模式** | 通过 `run_sim.py` 启动 MainWindow, 用户在 Web 服务面板点"启动" | 开发调试 |
| **单机无头模式** | 启动脚本直接 `web_engine.start_server(...)` + `uvicorn.run(...)` | 生产 (Linux/Windows Server) |
| **多节点 + 聚合层** | 多台机器各跑一个节点, 前置 `vnpy_aggregator` 聚合 | 正式生产 |

---

## 4. 模块组成

### 4.1 静态结构

```mermaid
classDiagram
    class WebTraderApp {
        +app_name = "RpcService"
        +engine_class = WebEngine
        +widget_name = "WebManager"
    }

    class WebEngine {
        +server: RpcServer
        +adapters: Dict~str, Adapter~
        +node_id: str
        +init_server()
        +register_event()
        +start_server(req, pub)
        +get_node_info()
        +list_strategies()
        +add/init/start/stop/remove_strategy()
    }

    class StrategyEngineAdapter {
        <<abstract>>
        +app_name: str
        +event_type: str
        +capabilities: Set
        +list_strategies() List~StrategyInfo~
        +add_strategy(req) OpResult
        +init_strategy(name) OpResult
        +start/stop/remove_strategy(name) OpResult
    }

    class CtaStrategyAdapter
    class SignalStrategyPlusAdapter
    class LegacySignalStrategyAdapter

    class FastAPI_App {
        +include_router(node_router)
        +include_router(strategy_router)
        +@startup: init RpcClient
    }

    class RouteNode
    class RouteStrategy
    class Deps {
        +get_access()
        +to_dict()
        +get_rpc_client()
        +unwrap_result()
    }

    WebTraderApp --> WebEngine
    WebEngine "1" --> "*" StrategyEngineAdapter
    StrategyEngineAdapter <|-- CtaStrategyAdapter
    StrategyEngineAdapter <|-- SignalStrategyPlusAdapter
    StrategyEngineAdapter <|-- LegacySignalStrategyAdapter
    FastAPI_App --> RouteNode
    FastAPI_App --> RouteStrategy
    RouteNode --> Deps
    RouteStrategy --> Deps
```

### 4.2 模块职责

| 模块 | 职责 | 对外契约 |
|---|---|---|
| `__init__.py` | 声明 `WebTraderApp`, 让 `main_engine.add_app` 找到引擎 | `WebTraderApp` 类 |
| `engine.py` | 启动 RPC Server, 订阅事件, 挂载 adapters, 暴露统一策略方法 | RPC 方法签名 |
| `strategy_adapter.py` | 屏蔽不同策略引擎的差异, 产出 `StrategyInfo` / `StrategyOpResult` | 抽象基类 + 注册表 |
| `web.py` | FastAPI 入口, 交易类路由 + WS Hub + lifecycle | HTTP `/api/v1/*`, WS `/api/v1/ws` |
| `deps.py` | 鉴权工具 + RPC 客户端持有 + 序列化 | `Depends(get_access)` |
| `routes_node.py` | 节点自描述路由 | `/api/v1/node/{info,health}` |
| `routes_strategy.py` | 通用策略管理路由 | `/api/v1/strategy/*` |
| `ui/widget.py` | Qt UI, 启停 Web 进程子进程 | MainWindow 内置面板 |

---

## 5. 数据流

### 5.1 REST 请求 (以"查询账户"为例)

```mermaid
sequenceDiagram
    participant B as 浏览器
    participant F as FastAPI
    participant D as deps.get_access
    participant C as RpcClient
    participant W as WebEngine (RpcServer)
    participant M as MainEngine

    B->>F: GET /api/v1/account (Bearer JWT)
    F->>D: Depends(get_access)
    D-->>F: 通过
    F->>C: rpc_client.get_all_accounts()
    C->>W: ZeroMQ REQ
    W->>M: main_engine.get_all_accounts()
    M-->>W: List[AccountData]
    W-->>C: ZeroMQ REP
    C-->>F: list
    F->>F: to_dict() 序列化
    F-->>B: 200 [{accountid,...}]
```

### 5.2 策略写请求 (以"启动策略"为例)

```mermaid
sequenceDiagram
    participant B as 浏览器
    participant R as routes_strategy
    participant C as RpcClient
    participant W as WebEngine
    participant A as Adapter (CtaStrategy)
    participant E as CtaEngine

    B->>R: POST .../SignalStrategyPlus/instances/x/start
    R->>C: start_strategy("SignalStrategyPlus","x")
    C->>W: RPC: start_strategy(engine, name)
    W->>A: adapters["SignalStrategyPlus"].start_strategy("x")
    A->>E: engine.start_strategy("x")
    E-->>A: None
    A-->>W: StrategyOpResult(ok=True)
    W-->>C: dict
    C-->>R: dict
    R-->>B: 200 {"ok":true, "message":"started"}
```

### 5.3 事件推送

```mermaid
sequenceDiagram
    participant E as StrategyEngine
    participant EE as EventEngine
    participant WE as WebEngine
    participant RS as RpcServer (PUB)
    participant RC as RpcClient (SUB)
    participant WS as WebSocket
    participant B as 浏览器

    E->>EE: event_engine.put(EVENT_SIGNAL_STRATEGY_PLUS, data)
    EE->>WE: process_strategy_event(event)
    WE->>RS: server.publish(topic, data)
    RS-->>RC: ZeroMQ PUB→SUB
    RC->>WS: _rpc_callback(topic, data)
    WS->>WS: _map_topic() => {topic:"strategy", engine:"..."}
    WS->>WS: json.dumps()
    WS-->>B: WS text frame
```

---

## 6. 技术选型与理由

| 选型 | 替代方案 | 选择理由 |
|---|---|---|
| FastAPI | Flask / Starlette | 自带 OpenAPI、Pydantic 校验、WS 一流支持 |
| ZeroMQ RPC | gRPC / HTTP | vnpy 内置 `vnpy.rpc`, 与 EventEngine 无缝, 零新依赖 |
| JWT (PyJose) | Session Cookie | 无状态, 更适合前后端分离 + 多进程 |
| passlib sha256 | bcrypt / argon2 | passlib 内置, 无需额外编译依赖 |
| 原生 WebSocket | SSE / Socket.IO | 双向, 标准, 跨语言; SSE 单向不够 |
| 两进程分离 | 单进程 | 交易进程 I/O 隔离, Web 进程可独立重启 |

---

## 7. 边界条件与限制

- **QMT Gateway 仅支持 Windows**(依赖 `xtquant`),云端需用 Windows 云主机。非 QMT 网关可以跑 Linux。
- **RPC 超时**: `RpcClient` 默认 30s,若交易进程阻塞会级联 Web 超时。Adapter 的 `init_strategy` 对 Future 也是 30s 超时。
- **WS 无回压**: 当前实现对 WS 不做队列/回压处理,极端高频推送 (万级 tick/秒) 会阻塞事件循环。真要跑 HFT 建议前端不订阅 tick。
- **单机单实例**: 一个交易进程只能对应一个 Web 进程 (端口冲突),多实例需启动多套配置。
- **配置文件位置**: `.vntrader/web_trader_setting.json`,由 `vnpy.trader.utility.get_file_path` 决定, Windows 下是 `%USERPROFILE%\.vntrader\`。

---

## 8. 扩展点 (Extension Points)

| 扩展点 | 入口 | 说明 |
|---|---|---|
| 新增策略引擎 | `strategy_adapter.py` → `ADAPTER_REGISTRY` | 写 Adapter 子类 + 注册 |
| 新增 REST 路由 | 新建 `routes_xxx.py` + `app.include_router` | 复用 `deps.get_access` |
| 新增 WS topic | `web.py:_BASE_TOPIC_MAP` 或 `_STRATEGY_TOPIC_MAP` | 同时在 WebEngine 订阅事件 |
| 替换鉴权方式 | `deps.py` 重写 `get_access` | 例如改成 mTLS / API Key |
| 替换前端 | `static/index.html` | 放 SPA 构建产物即可 |

详见 [development.md](./development.md)。
