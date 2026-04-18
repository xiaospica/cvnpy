# 二次开发指南

本章面向要在 `vnpy_aggregator` 上做扩展的工程师。目录:

- [1. 添加新的扇出接口](#1-添加新的扇出接口)
- [2. 添加新的透传接口](#2-添加新的透传接口)
- [3. 修复 proxy method 推断](#3-修复-proxy-method-推断-技术债务-1)
- [4. 自定义心跳逻辑](#4-自定义心跳逻辑)
- [5. 加入 Prometheus 监控](#5-加入-prometheus-监控)
- [6. 支持多实例 (Redis pub/sub)](#6-支持多实例-redis-pubsub)
- [7. 加审计日志](#7-加审计日志)
- [8. 多租户 / RBAC](#8-多租户--rbac)
- [9. 测试策略](#9-测试策略)

---

## 1. 添加新的扇出接口

### 场景
前端想要一个 `GET /agg/risks` 汇总所有节点风控状态。

### 步骤

假设节点层已经有 `/api/v1/risk/summary`(见 `vnpy_webtrader/docs/development.md`)。

编辑 [main.py](../main.py) 加一个路由:

```python
@app.get("/agg/risks", response_model=List[FanoutItem])
async def agg_risks(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/risk/summary")
```

就这一行。聚合、error handling、在线判定都被 `fanout_get` 封装了。

---

## 2. 添加新的透传接口

对于单节点写操作,目前已有统一 `proxy` 路由。但如果你想加强类型安全 (例如不让前端瞎拼 path),可以写显式透传:

```python
class RiskTuneBody(BaseModel):
    max_loss: float
    enabled: bool

@app.post("/agg/nodes/{node_id}/risk/tune")
async def tune_risk(
    node_id: str,
    body: RiskTuneBody,
    user: str = Depends(require_user),
):
    client = _reg().get(node_id)
    if not client:
        raise HTTPException(404, "node not found")
    status, result = await client.forward("POST", "/api/v1/risk/tune", body.dict())
    if status >= 400:
        raise HTTPException(status, result)
    return result
```

好处: 前端 TS 类型强, 聚合层文档 (Swagger) 里看得到 schema。

---

## 3. 修复 proxy method 推断 (技术债务 #1)

### 当前问题

[main.py](../main.py) 的 `proxy()`:

```python
method = "POST" if payload else "GET"
```

这推断不准: POST 无 body (例如 `/start`) 会变成 GET, DELETE 会丢失。

### 修复方案

用 `Request` 对象读原生 method + body:

```python
from fastapi import Request

@app.api_route(
    "/agg/nodes/{node_id}/proxy/{path:path}",
    methods=["GET", "POST", "DELETE", "PATCH", "PUT"],
)
async def proxy(
    node_id: str,
    path: str,
    request: Request,
    user: str = Depends(require_user),
) -> Any:
    client = _reg().get(node_id)
    if client is None:
        raise HTTPException(status_code=404, detail="node not found")

    method = request.method
    try:
        body = await request.json() if method in ("POST", "PATCH", "PUT") else None
    except Exception:
        body = None

    target = f"/api/v1/{path}"
    try:
        status_code, resp_body = await client.forward(method, target, body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=resp_body)
    return resp_body
```

顺带在 `NodeClient.forward()` 里也不要依赖 body 推断 method (它已经是参数传入,这点是对的)。

---

## 4. 自定义心跳逻辑

### 场景
你想在心跳失败时发飞书/钉钉告警。

### 方案 A: 在 `NodeRegistry._heartbeat_loop` 里加 hook

改 [registry.py](../registry.py):

```python
class NodeRegistry:
    def __init__(self, config, ws_dispatch=None, on_offline=None):
        ...
        self._on_offline = on_offline

    async def _heartbeat_loop(self):
        while not self._stop.is_set():
            for client in list(self._clients.values()):
                was_online = client.state.online
                await client.heartbeat()
                client.mark_offline_if_needed(self.config.heartbeat_fail_threshold)
                if was_online and not client.state.online and self._on_offline:
                    await self._on_offline(client.config.node_id)
            ...
```

然后 main.py:

```python
async def alert_offline(node_id: str):
    async with httpx.AsyncClient() as http:
        await http.post(WEBHOOK_URL, json={"text": f"node {node_id} offline!"})

_registry = NodeRegistry(_config, ws_dispatch=_hub.dispatch, on_offline=alert_offline)
```

### 方案 B: 独立 cron task 监控 `/agg/nodes`

不改代码,写一个外部脚本每分钟调一次 `/agg/nodes`,检测 online=false 发告警。对聚合层零侵入。

---

## 5. 加入 Prometheus 监控

### 步骤

```bash
pip install prometheus_client prometheus-fastapi-instrumentator
```

在 [main.py](../main.py):

```python
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Gauge, Counter

# 业务指标
nodes_online = Gauge("agg_nodes_online", "Online nodes count")
heartbeat_failures = Counter("agg_heartbeat_failures_total", "Heartbeat failures", ["node_id"])

# HTTP 指标自动
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
```

定期更新业务指标 (在心跳 hook 里):

```python
nodes_online.set(sum(1 for c in _reg().all() if c.state.online))
```

Grafana 看板:

- `agg_nodes_online` — 单值指标 (现在在线几个)
- `rate(agg_heartbeat_failures_total[5m])` — 心跳失败率
- `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` — P95 延迟

---

## 6. 支持多实例 (Redis pub/sub)

### 问题
单 worker 单机扛不住高并发前端 WS 时,想水平扩聚合层实例。但 `WsHub` 在各 worker 内存里独立,无法跨实例广播。

### 方案: Redis pub/sub

1. 每个聚合层实例订阅 Redis 的 `vnpy_events` channel
2. NodeClient 收到节点消息, 先 `redis.publish("vnpy_events", msg)`, 不直接调 `_hub.dispatch`
3. 实例内的 Redis subscriber 收到消息 → `_hub.dispatch(node_id, msg)` → 广播给本实例连着的前端 WS

```python
# registry.py 改
async def _handle_ws(self, node_id, msg):
    msg["node_id"] = node_id
    await redis.publish("vnpy_events", json.dumps(msg))

# main.py 加订阅协程
async def _redis_subscriber_loop():
    async with redis.pubsub() as sub:
        await sub.subscribe("vnpy_events")
        async for msg in sub.listen():
            data = json.loads(msg["data"])
            await _hub.dispatch(data["node_id"], data)
```

同时节点上游 WS 订阅只在一个实例跑 (用 Redis lock 或者只在一个 worker 启动 registry), 避免消息重复。

---

## 7. 加审计日志

### 需求
记录"谁在什么时候启停了哪个策略",用于事后追溯。

### 实现思路

在 `proxy` 路由里加日志:

```python
import logging
audit_logger = logging.getLogger("audit")

@app.api_route("/agg/nodes/{node_id}/proxy/{path:path}", ...)
async def proxy(node_id, path, request, user=Depends(require_user)):
    method = request.method
    ...
    # 仅对写操作记录
    if method in ("POST", "DELETE", "PATCH", "PUT"):
        audit_logger.info("user=%s node=%s method=%s path=%s body=%s",
                          user, node_id, method, path, body)
    ...
```

日志 handler 配置到单独文件 `/var/log/vnpy/aggregator-audit.log`, 加轮转。

---

## 8. 多租户 / RBAC

### 场景
多个团队共用一个聚合层,每个团队只看自己的节点。

### 简单实现

1. 配置文件里给每个节点打 `team: "teamA"` 标签
2. 用户表 `users.yaml`:
   ```yaml
   users:
     - name: alice
       pwd_hash: ...
       roles: [teamA, admin]
     - name: bob
       pwd_hash: ...
       roles: [teamB]
   ```
3. JWT payload 加入 `roles` 列表
4. 路由加 dependency `require_role("teamA")` 或过滤 `/agg/nodes` 只返回用户有权限的节点

### 实现复杂度
中等。需要改 `auth.py` / `config.py` / 所有路由。建议作为 v1.0 milestone 统一做。

---

## 9. 测试策略

### 9.1 单元测试

聚合层大部分逻辑是 IO, 纯逻辑不多。适合测的:

- `config.load_config` 的 fallback 顺序 + 环境变量覆盖
- `auth.create_access_token` / `require_user` 的边界 (过期/无效/正常)
- `WsHub.broadcast` 的 dead connection 清理

Fake httpx client 做 NodeClient 单测:

```python
import httpx, respx, pytest

@respx.mock
async def test_node_client_auto_relogin_on_401():
    from vnpy_aggregator.client import NodeClient
    from vnpy_aggregator.config import NodeConfig

    cfg = NodeConfig("test", "http://test", "u", "p")
    client = NodeClient(cfg)

    respx.post("http://test/api/v1/token").mock(side_effect=[
        httpx.Response(200, json={"access_token":"t1","token_type":"bearer"}),
        httpx.Response(200, json={"access_token":"t2","token_type":"bearer"}),
    ])
    respx.get("http://test/api/v1/account").mock(side_effect=[
        httpx.Response(401),
        httpx.Response(200, json=[{"balance":100}]),
    ])

    data = await client.get_json("/api/v1/account")
    assert data == [{"balance":100}]
    assert client._token == "t2"
```

### 9.2 集成测试

在一个 pytest 里用 asyncio 启:

1. 两个 webtrader FastAPI (TestClient)
2. 一个聚合层 FastAPI (TestClient)
3. 断言 `/agg/accounts` 返回合并结果

### 9.3 端到端 (真 uvicorn)

手动步骤:

```bash
# 1. 起两个假节点 (可以用 httpx mock server 或真的 run_sim.py 挂两个端口)

# 2. 起聚合层
export AGG_JWT_SECRET=dev AGG_ADMIN_PWD=admin
AGG_CONFIG=/tmp/agg-test.yaml python -m uvicorn vnpy_aggregator.main:app --port 9000

# 3. 打流量
TOKEN=$(curl -s -X POST -d "username=admin&password=admin" http://localhost:9000/agg/token | jq -r .access_token)
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/agg/nodes
curl -H "Authorization: Bearer $TOKEN" http://localhost:9000/agg/accounts

# 4. 用 websocat 看 WS
websocat "ws://localhost:9000/agg/ws?token=$TOKEN"
```

---

## 10. 代码规范

遵循工程 [CLAUDE.md](../../CLAUDE.md):

- PEP 8 + 类型提示
- `from __future__ import annotations` 头部统一
- dataclass 优先, 不要用字典当弱类型结构
- 异步函数 `async def` + `await`, 不要 `time.sleep` / `requests.get` 之类阻塞 IO
- 日志用 stdlib logging + 模块级 `logger = logging.getLogger(__name__)`
- commit message 前缀: `feat/fix/refactor/docs/test/chore`

---

## 11. 常见陷阱

1. **阻塞 asyncio loop**: 不要在 FastAPI async handler 里调用 `time.sleep` / `requests.get` / `zmq.recv` 等同步阻塞。会把整个聚合层卡住。
2. **httpx.AsyncClient 生命周期**: 每个 NodeClient 持有一个 AsyncClient,`close()` 时要 `await self._http.aclose()`,否则会泄露连接。
3. **WebSocket 接收超时**: `await websocket.receive_text()` 会一直等,如果前端长时间不发任何东西也不会超时。如果要检测僵尸连接,自己加 `asyncio.wait_for` + 心跳消息。
4. **JWT 过期时间单位**: `AggregatorConfig.token_expire_minutes` 是**分钟**, 别和节点层的名字混了。
5. **重启丢失动态节点**: `POST /agg/nodes` 添加的节点只存在内存, 重启丢。如果需要持久,见 [design.md#已知技术债务](./design.md#12-已知技术债务)。

---

## 12. 参考

- FastAPI async 指南: https://fastapi.tiangolo.com/async/
- httpx 异步: https://www.python-httpx.org/async/
- websockets 客户端: https://websockets.readthedocs.io/en/stable/
- 同级工程: [../../vnpy_webtrader/docs/](../../vnpy_webtrader/docs/)
