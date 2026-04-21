"""vnpy_common — 跨 app 通用工具包.

不是 vnpy app (不注册进 MainEngine), 只是一个普通 Python 包, 放那些既不属于
某个具体业务 app (tushare / ml_strategy / signal_strategy / ...) 又在多个 app
之间被需要的基础能力.

目前包含:
- ``scheduler`` — APScheduler 包装, 支持 HH:MM 日频 cron 作业. tushare_pro 的
  20:00 日更 + ml_strategy 的 21:00 推理都用它. 搬进来前位于
  ``vnpy_tushare_pro.scheduler``, 造成 ``ml_strategy → tushare_pro`` 的反向
  依赖, 已解开.
"""
