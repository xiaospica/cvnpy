# 修改与评估计划（MySQL配置生效 + UI评估 + QSS加载测试）

## 一、需求拆解与目标

### 1) MySQLSignalStrategy 配置读取与成员变量修正
- 目标A：确保 `mysql_signal_setting.json` 的配置真实生效，不再被硬编码默认值“遮蔽”。
- 目标B：将数据库连接相关字段改为**实例成员变量**（对象级），避免作为类级静态默认值在继承链上产生歧义。

### 2) PyQtAds 替换可行性评估（仅评估，不写业务代码）
- 目标：评估 vn.py 当前 UI 是否可替换为 PyQtAds，并保持现有布局效果一致（功能与交互行为尽量等价）。
- 交付：输出中文评估文档，给出改造点、风险、兼容性与建议路线。

### 3) UI 美化机制与外部 QSS 能力评估 + run_sim.py 加载测试
- 目标A：评估 vn.py 现有美化机制（qdarkstyle 与局部样式）及外部 QSS 的可接入性。
- 目标B：在 `run_sim.py` 增加一段可控的外部 QSS 加载测试代码，验证是否可生效。

---

## 二、实施步骤（代码与文档）

### 步骤1：修复 JSON 配置未生效根因
1. 检查并调整策略名初始化时序：
   - 当前根因是子类 `MultiStrategySignalStrategy` 在 `super().__init__()` 之后才设置 `strategy_name`，导致 `MySQLSignalStrategy.__init__()` 内部读取 JSON 时无法按目标 key 命中。
2. 方案：
   - 将子类策略名改为**类属性**定义（例如 `strategy_name = "multistrategy-v5.2.1"`），保证父类初始化期间即可读取到正确策略名。
3. 验证：
   - 通过启动日志确认命中对应 key（优先策略名，其次 default）。

### 步骤2：将 MySQL 配置字段改为实例成员变量
1. 在 `MySQLSignalStrategy.__init__` 中显式初始化：
   - `self.db_host/self.db_port/self.db_user/self.db_password/self.db_name/self.poll_interval`。
2. 保留 `parameters` 作为参数映射清单（类属性），但参数值来源转为实例属性。
3. `load_external_setting()` 调用 `update_setting()` 后，确保实例属性被覆盖并用于 `connect_db()`。
4. 验证：
   - 在连接数据库前输出（脱敏）配置摘要，确认使用的是 JSON 值。

### 步骤3：补充配置读取鲁棒性
1. 支持并记录以下场景：
   - 命中 `strategy_name` 配置；
   - 命中 `default` 配置；
   - 文件不存在/JSON 解析失败（给出明确日志）。
2. 安全性处理：
   - 日志中不输出明文密码。

### 步骤4：输出 PyQtAds 可替换性评估文档（中文）
1. 新增文档（建议）：`docs/ui_pyqtads_assessment.md`。
2. 文档结构：
   - 现状架构（QMainWindow + QDockWidget + saveState/restoreState）；
   - 与 PyQtAds 的功能映射；
   - 需要改造的核心点（Dock创建、tabify、布局持久化迁移）；
   - 兼容性风险（PySide6版本、状态迁移、行为回归）；
   - 分阶段落地建议（PoC -> 适配层 -> 灰度开关）。

### 步骤5：评估并测试外部 QSS 加载
1. 在 `run_sim.py` 中于 `qapp = create_qapp()` 后加入“可选外部 QSS”加载逻辑：
   - 读取如 `custom.qss`；
   - 采用“叠加模式”到现有 qdarkstyle（`qapp.styleSheet() + extra_qss`）。
2. 兼容处理：
   - 文件不存在时不报错，仅记录提示日志。
3. 验证：
   - 提供最小 `custom.qss` 示例并观察窗口控件样式变化，确认加载链路生效。

### 步骤6：校验与回归
1. 语法检查：
   - 至少对改动文件执行 `python -m py_compile`。
2. 运行检查：
   - 启动 `run_sim.py`，观察：
     - 策略配置读取日志；
     - UI 正常打开；
     - 外部 QSS 叠加是否生效。
3. 输出结果说明：
   - 逐项对应您的三个需求，给出“已完成/验证方式/残余风险”。

---

## 三、交付清单

1. 代码修改：
- `vnpy_signal_strategy/mysql_signal_strategy.py`
- `vnpy_signal_strategy/strategies/multistrategy_signal_strategy.py`
- `run_sim.py`

2. 文档新增：
- `docs/ui_pyqtads_assessment.md`（PyQtAds替换评估，中文）

3. 验证输出：
- 语法检查结果
- 运行与日志验证结果（配置命中、QSS加载）

---

## 四、注意事项
- 严格不改变现有业务交易逻辑，仅修复配置读取链路与UI评估/样式加载测试。
- 保持当前 UI 布局与交互不变（本轮不进行 PyQtAds 替换实现）。
- 密码等敏感信息不在日志中明文输出。
