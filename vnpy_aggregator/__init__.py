"""vnpy_aggregator: 多节点 vnpy 交易进程的聚合中控层.

提供:
    - 节点注册表与心跳
    - 跨节点 REST 扇出 (账户/持仓/委托/成交/策略)
    - WebSocket 汇流, 补齐 node_id 字段
    - 独立的 JWT 登录

部署示例::

    uvicorn vnpy_aggregator.main:app --host 0.0.0.0 --port 9000
"""

__version__ = "0.1.0"
