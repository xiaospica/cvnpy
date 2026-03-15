# Tasks

- [x] Task 1: Refactor `vnpy_qmt_sim` structure
  - [x] SubTask 1.1: Create `vnpy_qmt_sim/md.py` and `vnpy_qmt_sim/td.py` implementing `QmtSimMd` and `QmtSimTd` classes with interfaces matching `vnpy_qmt`.
  - [x] SubTask 1.2: Update `vnpy_qmt_sim/gateway.py` to use `QmtSimMd` and `QmtSimTd`, and initialize them in `__init__`.
  - [x] SubTask 1.3: Enhance `SimulationCounter` in `gateway.py` (or move to separate file if needed) to support `query_order`, `query_trade`, `query_position`, `query_account` returning mocked data.

- [x] Task 2: Refactor `vnpy_signal_strategy` Engine
  - [x] SubTask 2.1: Update `SignalEngine` in `vnpy_signal_strategy/engine.py` to implement `load_strategy_class` (scanning `strategies/` folder) and `load_strategy_setting`/`save_strategy_setting`.
  - [x] SubTask 2.2: Update `SignalEngine` to manage strategy instances (add, init, start, stop, remove) similar to `CtaEngine`.

- [x] Task 3: Refactor `vnpy_signal_strategy` UI
  - [x] SubTask 3.1: Rewrite `SignalStrategyWidget` in `vnpy_signal_strategy/ui/widget.py` to match `CtaManager` layout and functionality.
  - [x] SubTask 3.2: Implement `StrategyManager` and `SettingEditor` classes for `SignalStrategy` (can be adapted from CTA version).
  - [x] SubTask 3.3: Ensure "Add Strategy" dialog allows selecting class and editing parameters.

- [x] Task 4: Fix Position Lookup in `MySQLSignalStrategy`
  - [x] SubTask 4.1: Modify `process_signal` to use gateway-aware `vt_positionid`.

# Task Dependencies
- Task 2 must be done before Task 3 (UI depends on Engine capabilities).
