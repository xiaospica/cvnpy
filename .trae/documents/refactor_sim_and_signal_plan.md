# Refactor Plan: QMT Sim Position Fix & Signal Strategy UI

## 1. Fix Position Lookup Issue

The user reported that `MySQLSignalStrategy` fails to find existing positions when processing a SELL signal in simulation. This is likely due to incorrect `vt_positionid` construction in the strategy or simulation.

### Steps:
1.  **Locate `MySQLSignalStrategy`**: Find the file defining this class (likely `vnpy_signal_strategy/mysql_signal_strategy.py` or similar).
2.  **Update `process_signal`**:
    -   Modify the position lookup logic.
    -   Retrieve the `gateway_name` associated with the contract (e.g., `QMT_SIM`).
    -   Construct the correct `vt_positionid` format: `{gateway_name}.{symbol}.{exchange}.{Direction.LONG.value}`.
    -   Use `main_engine.get_position(vt_positionid)` instead of the current `f"{vt_symbol}.LONG"` lookup.
    -   Add debug logging to print the constructed `vt_positionid` and the result of the query.
3.  **Verify Simulation Counter**:
    -   Review `vnpy_qmt_sim/td.py`.
    -   Ensure `update_position` correctly creates and stores `PositionData`.
    -   Ensure `query_position` correctly pushes position events to the main engine.

## 2. Refactor Signal Strategy UI

The user requested `vnpy_signal_strategy` UI to match the design of `vnpy_ctastrategy`, supporting class-based strategy management.

### Steps:
1.  **Update `SignalEngine`**:
    -   Ensure it supports:
        -   `load_strategy_class`: Loading classes from `strategies/` folder.
        -   `init_all_strategies`, `start_all_strategies`, `stop_all_strategies`.
        -   `edit_strategy`: Updating strategy settings.
        -   `remove_strategy`: Removing strategy instance and setting.
        -   `get_strategy_class_parameters`: Retrieving default parameters for UI.
2.  **Implement `SignalManager` (Main Widget)**:
    -   Create `vnpy_signal_strategy/ui/widget.py`.
    -   Implement `SignalManager` class inheriting `QtWidgets.QWidget`.
    -   Add `StrategyManager` list view (ScrollArea).
    -   Add global control buttons: "Add Strategy", "Init All", "Start All", "Stop All", "Clear Log".
    -   Add `LogMonitor` (and `StopOrderMonitor` if applicable).
3.  **Implement `StrategyManager` (Instance Widget)**:
    -   Create `StrategyManager` class inheriting `QtWidgets.QFrame`.
    -   Display strategy status (Inited, Trading), parameters, and variables.
    -   Add individual control buttons: "Init", "Start", "Stop", "Edit", "Remove".
4.  **Implement `SettingEditor`**:
    -   Create `SettingEditor` dialog for creating/editing strategies.
    -   Dynamically generate fields based on strategy parameters.

## 3. Refactor QMT Sim Structure (Verification)

Ensure the previous refactoring of `vnpy_qmt_sim` (splitting into `gateway.py`, `md.py`, `td.py`) is complete and correct.

### Steps:
1.  Verify `vnpy_qmt_sim/gateway.py` imports and uses `QmtSimMd` and `QmtSimTd`.
2.  Verify `vnpy_qmt_sim/md.py` implements `QmtSimMd`.
3.  Verify `vnpy_qmt_sim/td.py` implements `QmtSimTd` and `SimulationCounter`.
