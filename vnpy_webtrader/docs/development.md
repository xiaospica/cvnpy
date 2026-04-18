# 二次开发指南

本章面向希望在 `vnpy_webtrader` 上做增量开发的工程师。任务清单:

- [添加新 REST 路由](#1-添加新-rest-路由)
- [新增一个策略引擎 Adapter](#2-新增一个策略引擎-adapter)
- [扩展 WebSocket topic](#3-扩展-websocket-topic)
- [替换鉴权机制](#4-替换鉴权机制)
- [本地调试](#5-本地调试)
- [测试策略](#6-测试策略)
- [代码规范](#7-代码规范)

---

## 1. 添加新 REST 路由

### 场景
你想加一个 `GET /api/v1/risk/summary`,读取某风控引擎的摘要。

### 步骤

#### 1.1 在交易进程侧暴露 RPC 方法

假设风控引擎名为 `RiskEngine`,已经 `main_engine.add_app(RiskApp)`:

编辑 [engine.py](../engine.py) 的 `WebEngine.init_server()`:

```python
def init_server(self):
    ...
    risk_engine = self.main_engine.get_engine("Risk")
    if risk_engine:
        self.server.register(risk_engine.get_summary)
```

或者写一个包装方法 (推荐, 方便加日志/鉴权逻辑):

```python
def get_risk_summary(self) -> dict:
    engine = self.main_engine.get_engine("Risk")
    if not engine:
        return {"ok": False, "message": "Risk engine not loaded"}
    return {"ok": True, "data": engine.get_summary()}
```

然后 `self.server.register(self.get_risk_summary)`。

#### 1.2 Web 进程侧新建路由文件

**新建** `vnpy_webtrader/routes_risk.py`:

```python
from fastapi import APIRouter, Depends
from .deps import get_access, get_rpc_client, unwrap_result

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])


@router.get("/summary")
def get_summary(access: bool = Depends(get_access)) -> dict:
    return unwrap_result(get_rpc_client().get_risk_summary())
```

#### 1.3 在 `web.py` 挂载

```python
from .routes_risk import router as risk_router
app.include_router(risk_router)
```

#### 1.4 重启 Web 进程,`GET /docs` 应能看到新路由。

---

## 2. 新增一个策略引擎 Adapter

见 [strategy_adapter.md#新增一个自定义引擎的-adapter](./strategy_adapter.md#6-新增一个自定义引擎的-adapter)。关键步骤:

1. 继承 `StrategyEngineAdapter`,写 Adapter 子类
2. 用 `@register_adapter` 装饰,或直接改 `ADAPTER_REGISTRY` 字典
3. 确保启动脚本 `import` 了你的模块
4. 写单测覆盖 add/init/start/stop/remove/edit

---

## 3. 扩展 WebSocket topic

### 场景
新增一个"风控告警"事件 `EVENT_RISK_ALERT`, 前端想通过 WS topic=`risk` 收到。

### 步骤

#### 3.1 交易进程订阅新事件

编辑 `WebEngine.register_event()`:

```python
from vnpy_risk.base import EVENT_RISK_ALERT

def register_event(self):
    ...
    self.event_engine.register(EVENT_RISK_ALERT, self.process_risk_event)

def process_risk_event(self, event):
    self.server.publish(event.type, event.data)
```

#### 3.2 Web 进程配置 topic 映射

编辑 [web.py](../web.py):

```python
_BASE_TOPIC_MAP = {
    EVENT_TICK: "tick",
    ...
    "eRiskAlert": "risk",           # 或在 _STRATEGY_TOPIC_MAP 里配
}
```

#### 3.3 验证

用 websocat 连 WS,触发风控告警,应收到:

```json
{"topic":"risk","node_id":"...","ts":..., "data":{...}}
```

---

## 4. 替换鉴权机制

默认是 OAuth2 Password + JWT。如果你想换成:

- **API Key**: 客户端 header `X-API-Key: xxx`,服务端 lookup key → user
- **mTLS**: 在 Nginx 终止 TLS 时做客户端证书校验,反代加一个 header `X-Client-CN`,FastAPI 读这个 header 判断

### 实现方式

改 [deps.py](../deps.py) 的 `get_access`:

```python
from fastapi import Header

async def get_access(x_api_key: str = Header(None)) -> bool:
    if not x_api_key or x_api_key != os.environ["VNPY_API_KEY"]:
        raise HTTPException(status_code=401, detail="invalid api key")
    return True
```

所有已有路由不需改 (都是 `Depends(get_access)`)。

### 注意 WS 鉴权
`get_websocket_access` 是独立的,也需要同步修改。

---

## 5. 本地调试

### 5.1 VSCode launch.json 示例

```json
{
  "name": "vnpy sim",
  "type": "python",
  "request": "launch",
  "program": "${workspaceFolder}/run_sim.py",
  "python": "F:/Program_Home/vnpy/python.exe",
  "cwd": "${workspaceFolder}",
  "env": {
    "VNPY_WEB_SECRET": "dev-secret-do-not-use-in-prod",
    "VNPY_NODE_ID": "local-dev"
  },
  "console": "integratedTerminal"
}
```

### 5.2 独立调试 Web 进程

当交易进程已经起在另一个 shell 里(RpcServer 已启动):

```bash
# 直接跑 uvicorn 调试, 不经 QProcess
"F:/Program_Home/vnpy/python.exe" -m uvicorn vnpy_webtrader.web:app \
    --host 127.0.0.1 --port 8000 --reload
```

改 `web.py` / `routes_*.py` 会自动重启。

### 5.3 Mock RPC 单测 web 路由

```python
# tests/test_route_node.py
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from vnpy_webtrader import web

def test_node_info_requires_auth():
    client = TestClient(web.app)
    r = client.get("/api/v1/node/info")
    assert r.status_code == 401

def test_node_info_ok():
    fake = MagicMock()
    fake.get_node_info.return_value = {"node_id":"t", "display_name":"t", "uptime":0, "gateways":[], "engines":[], "strategy_engines":[]}
    web.set_rpc_client(fake)
    # 还需要 mock JWT, 略...
```

---

## 6. 测试策略

### 6.1 单元测试

- **Adapter 层**: 用 Fake Engine,覆盖各种签名差异。已有 [tests/test_strategy_adapter.py](../../tests/test_strategy_adapter.py) 9 个用例可作模板。
- **路由层**: 用 `TestClient` + mock `RpcClient`,覆盖 401/404/正常路径。
- **WebEngine**: 可以不测,纯 glue code;如要覆盖,mock `MainEngine` + `EventEngine`。

### 6.2 集成测试

- 在 CI 里用 `pytest-asyncio` 启一个真的 EventEngine + WebEngine + uvicorn,用 httpx async client 打 REST,用 websockets 连 WS,验证事件推送。
- 本工程已有 `tests/test_signal_strategy_*.py` 的集成测试风格可参考。

### 6.3 运行测试

```bash
"F:/Program_Home/vnpy/python.exe" -m pytest tests/test_strategy_adapter.py -v
```

全量 (跳过要 `polars` 的):

```bash
"F:/Program_Home/vnpy/python.exe" -m pytest tests/ --ignore=tests/test_alpha101.py -q
```

---

## 7. 代码规范

遵循工程 [CLAUDE.md](../../CLAUDE.md):

- **PEP 8** + 类型提示
- 类 `CamelCase`,函数/变量 `snake_case`,常量 `UPPER_CASE`
- 公共函数加 docstring (Google 或 NumPy 风格)
- `from __future__ import annotations` 让类型注解延迟求值,支持 `Dict[str, X]` 类写法在 Python 3.10-

特别约定:

- **路由 handler 薄** (只做参数校验 + RPC 调用 + 序列化), 业务逻辑放 Adapter 或 WebEngine。
- **Adapter 方法必须返回 `StrategyOpResult`**,不要抛原始异常到 RPC 层。
- **新增 RPC 方法名用 `snake_case`**,避免与 MainEngine 已有方法冲突。

---

## 8. Commit / PR 规范

- Commit message 前缀: `feat:` / `fix:` / `refactor:` / `docs:` / `test:` / `chore:`
- 涉及 Web 接口变更必须同步更新 [../../docs/api.md](../../docs/api.md)
- 新增 Adapter 必须带测试
- 修改鉴权 / 配置相关代码的 PR 需要标注 `security` 标签, 走双人 review

---

## 9. 常见陷阱

1. **`add_app` 顺序**: `WebTraderApp` 必须在所有策略 App 之后 add,否则 `build_adapters` 时漏掉。当前实现 (`start_server` 延迟构建) 已经规避这个问题,但新代码要延续这个约定。
2. **`RpcClient` 不是线程安全的**: 不要在多个线程同时调用同一个 `rpc_client.xxx`。FastAPI 的 async handler 默认在线程池跑 sync 函数,如果出现并发问题,可以加 `asyncio.Lock`。
3. **循环 import**: 不要让 `deps.py` 去 import `engine.py` 或 `web.py`。deps 是最底层。
4. **硬编码常量**: 不要在路由里写死 `"SignalStrategyPlus"` 这种字符串;用 Adapter 的 `app_name`。
5. **JWT secret 泄露**: 配置文件里的 `secret_key = "change-me"` 提交到 git 的版本控制是红线。用 `.gitignore` 把 `.vntrader/` 整个排除。

---

## 10. 有用的参考

- FastAPI 文档: https://fastapi.tiangolo.com/
- vnpy 源码: `vnpy/trader/engine.py`, `vnpy/event/engine.py`, `vnpy/rpc/*`
- 本工程同级文档: [../../docs/api.md](../../docs/api.md), [../../docs/frontend_requirements.md](../../docs/frontend_requirements.md)
- 聚合层工程文档: [../../vnpy_aggregator/docs/](../../vnpy_aggregator/docs/)
