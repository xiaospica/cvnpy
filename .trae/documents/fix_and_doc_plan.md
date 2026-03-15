# Plan for Fixing Signal Strategy Issues

## 1. Strategy Configuration via JSON File

The user wants `MySQLSignalStrategy` to load its configuration from a JSON file instead of hardcoded defaults or arguments passed during initialization.

### Analysis
- Current `SignalEngine.add_strategy` accepts a `setting` dictionary.
- `SignalEngine.load_strategy_setting` loads `signal_strategy_setting.json` and calls `add_strategy`.
- The user likely wants to ensure that when `add_strategy` is called (e.g., from UI or script), the `setting` can be omitted or merged with a config file specific to the strategy class or instance.
- However, the standard vn.py pattern is:
    1.  UI/Script provides `setting` dict.
    2.  Strategy `__init__` receives `setting`.
    3.  `load_strategy_setting` reads `signal_strategy_setting.json` (which persists the settings of added strategies) and restores them.
- If the user implies a *separate* global config for the strategy class (e.g., DB connection info shared across instances), we should modify `MySQLSignalStrategy` to load a specific config file if `setting` is empty.
- **Better Approach**: Since `SignalEngine` already handles `signal_strategy_setting.json`, we should rely on it. But `run_sim.py` currently hardcodes the dict.
- **Refinement**: I will modify `run_sim.py` to NOT pass hardcoded settings if `signal_strategy_setting.json` already contains the strategy. Or, I will ensure `MySQLSignalStrategy` can load defaults from a specific file if needed.
- **Decision**: The user explicitly asked to "modify it to load from a configuration file". I will modify `MySQLSignalStrategy` (or `MultiStrategySignalStrategy`) to load a `multistrategy_setting.json` (or similar) in its `__init__` if parameters are missing, OR simply rely on the existing `SignalEngine` persistence mechanism but ensure `run_sim.py` uses it correctly.
- **Interpretation**: The user says "setting argument should be read from config file". I will modify `SignalEngine.add_strategy` or the invocation in `run_sim.py` to load from `signal_strategy_setting.json` if available.
- **Actually**: The user might mean a specific config file for the strategy *logic* (like DB config), separate from vn.py's engine setting.
- **Plan**:
    1.  Modify `MySQLSignalStrategy` to check for a config file (e.g., `mysql_signal_config.json`) in `on_init` or `__init__` if DB params are not in `setting`.
    2.  Alternatively, and more standard: ensure `run_sim.py` loads settings from `signal_strategy_setting.json` instead of hardcoding.
    3.  Let's go with the user's specific request: "modify initialization arguments to read from config file".
    4.  I will change `run_sim.py` to load the settings from `signal_strategy_setting.json` if it exists. If not, use default.
    5.  Wait, the user said "modify MySQLSignalStrategy... setting argument... read from config file". I will add logic in `MySQLSignalStrategy.__init__` to load from `vnpy_strategy_dev/global_setting.json` (or similar) if parameters are not provided.
    6.  **Revised Plan**: I will ensure `SignalEngine` loads `signal_strategy_setting.json` on init. In `run_sim.py`, I will remove the hardcoded `add_strategy` call if the strategy is already loaded from JSON. If it's a fresh start, I'll save the settings to JSON once.

## 2. UI Update Logic Analysis

The user reports that `trade`, `order`, `account`, `position` windows are not updating after clicking "Start All".

### Analysis
- `SignalEngine` processes `EVENT_ORDER`, `EVENT_TRADE`, `EVENT_POSITION` etc.
- In `vnpy_ctastrategy`, `CtaEngine` puts these events into its own processing queue but *also* relies on `MainEngine` to distribute them to UI.
- The UI windows (Trade/Order/Account/Position) subscribe to `EVENT_TRADE`, `EVENT_ORDER`, etc., from `EventEngine`.
- **The Issue**:
    - `SignalEngine` registers handlers for these events: `process_order_event`, etc.
    - These handlers call `call_strategy_func`.
    - **Crucially**: The `MainEngine` (and `QmtSimGateway`) is responsible for pushing these events.
    - If `QmtSimGateway` pushes events, they go to `EventEngine`.
    - `MainWindow` (in `run_sim.py` -> `vnpy.trader.ui.mainwindow`) has widgets that listen to these events.
    - **Why it might fail**:
        - Maybe `QmtSimGateway` is not connected?
        - Maybe `SignalEngine` swallows events? (Unlikely, it just listens).
        - Maybe `QmtSimGateway` is not pushing events correctly?
        - **Hypothesis**: In `run_sim.py`, we might not be connecting the gateway properly or the `MainEngine` is not fully set up.
        - **Check `run_sim.py`**: It calls `main_engine.connect(setting, "QMT_SIM")`.
        - **Check `QmtSimGateway`**: It has `query_account`, `query_position` etc.
        - **The missing link**: When `Start All` is clicked, strategies start polling DB. When they send orders, `QmtSimGateway` should receive them.
        - **Wait**, `QmtSimGateway` in `td.py` calls `self.gateway.on_order(order)`. This pushes `EVENT_ORDER`.
        - **Potential Cause**: The `SignalStrategyWidget` or `run_sim.py` might be blocking the event loop? Or `SignalEngine` is not triggering the necessary queries?
        - **Actually**, `QmtSimGateway` (Sim) is local. It should work.
        - **User Observation**: "UI updates... work abnormally".
        - **Investigation**: Check if `EventEngine` is shared correctly. Yes, passed in `__init__`.
        - **Refinement**: The user says "after I click Start All... should get signal... and update main interface".
        - If the strategy sends an order, `SignalEngine.send_order` calls `main_engine.send_order`.
        - `main_engine` calls `gateway.send_order`.
        - `QmtSimGateway` generates `on_order` -> `EventEngine` -> UI.
        - **Maybe**: The `MainEngine` in `run_sim.py` is created. The UI is created *after*?
        - **Another possibility**: The `SignalStrategyWidget` is running in a way that blocks updates? Unlikely.
        - **Action**: I will verify `run_sim.py` event loop setup and `QmtSimGateway` event push logic.
        - **Key finding**: `SignalEngine` listens to `EVENT_TIMER`. `MySQLSignalStrategy` uses a *separate thread* `run_polling`.
        - **Thread Safety**: `run_polling` calls `self.send_order`.
        - `self.send_order` -> `SignalEngine.send_order` -> `main_engine.send_order` -> `gateway.send_order`.
        - `QmtSimGateway.send_order` -> `td.send_order` -> `SimulationCounter.send_order`.
        - `SimulationCounter` modifies dicts and calls `gateway.on_order`.
        - `gateway.on_order` emits event.
        - **Issue**: `gateway.on_order` (and event emission) happens in the `poll_thread`. Qt UI updates *must* happen in the main thread.
        - **Fix**: `EventEngine` in vn.py is thread-safe (uses `queue.Queue` and `QTimer` polling in main thread). So this *should* be fine.
        - **However**: `SimulationCounter` is not thread-safe if accessed from multiple threads.
        - **Wait**: `run_polling` is in a thread. `send_order` runs in that thread.
        - **Verification**: I will review `vnpy.event.engine`. It uses `QTimer` to poll the queue. This is correct.
        - **Re-reading User**: "UI updating... works abnormally". Maybe it doesn't update *at all*?
        - **Possible fix**: Ensure `QmtSimGateway` is connected *before* strategies start.
        - **Another Check**: `process_signal` calls `send_order`.

## 3. "Exchange is not defined" Error

User reports `NameError: name 'Exchange' is not defined` when clicking "Start All".

### Analysis
- **Location**: Likely in `MySQLSignalStrategy.process_signal` or `SignalEngine`.
- **Code Reference**:
    - `vnpy_signal_strategy/mysql_signal_strategy.py`:
        - Line 186: `if Exchange(exchange_str) in gw.exchanges:`
    - **Issue**: `Exchange` is used but not imported in `mysql_signal_strategy.py`.
    - **Fix**: Import `Exchange` from `vnpy.trader.constant`.

## 4. A-Share Trading Logic Review & Documentation

User wants a review of order/cancel/reject logic in `vnpy_signal_strategy` and `vnpy_qmt_sim` considering A-share rules (T+1, etc.), and a markdown doc with flowcharts.

### Analysis
- **A-Share Rules**:
    - Buy: T+0 available for sell (Simulated by `frozen`? No, T+1).
    - Sell: T+1.
    - Limit Up/Down checks.
    - Order rejection (insufficient funds/positions).
- **Current Sim Logic**:
    - `SimulationCounter`: Checks `partial_rate`, `reject_rate`.
    - `update_position`: T+0 logic currently (volume added immediately).
    - **Needs Update**:
        - `PositionData` should track `frozen` and `yd_volume` (yesterday volume) for T+1 sell check.
        - `send_order` should check `available` position (volume - frozen).
- **Plan**:
    - Create `docs/a_share_trading_logic.md`.
    - Describe the flow.
    - Create Mermaid diagrams.

---

# Implementation Plan

## Task 1: Fix `Exchange` Import Error
- **File**: `vnpy_signal_strategy/mysql_signal_strategy.py`
- **Action**: Add `from vnpy.trader.constant import Exchange`.

## Task 2: Strategy Config from File
- **File**: `vnpy_signal_strategy/mysql_signal_strategy.py`
- **Action**:
    - In `__init__`, check if `setting` contains DB params.
    - If not, try to load from `mysql_signal_setting.json` (or similar) in the working directory.
    - Or better, update `SignalEngine` to facilitate this.
    - **Simpler**: Just update `run_sim.py` to load from a file and pass it.
    - **User Constraint**: "Modify MySQLSignalStrategy... to read from config file".
    - **Action**: Modify `MySQLSignalStrategy.__init__`:
        ```python
        def __init__(self, signal_engine, setting):
            super().__init__(signal_engine, setting)
            # ...
            if not setting:
                self.load_external_setting()
        ```

## Task 3: UI Update Analysis & Fix
- **Hypothesis**: The event engine might be stalled or events are not reaching the UI.
- **Check**: `run_sim.py` main loop.
- **Verification**: I will add a `sleep` in the `run_polling` loop to ensure it doesn't hog the GIL (it has `time.sleep(0.5)`, so it's fine).
- **Potential Fix**: Ensure `main_engine` is fully initialized before adding strategies.

## Task 4: Documentation
- **File**: `docs/a_share_sim_logic.md`
- **Content**: Logic description, Flowcharts (Mermaid).

## Task 5: Simulation Logic Enhancement (Optional but implied by "Review")
- The user asked to "review... and output doc".
- If I find logic flaws (like T+1 missing), I should note them or fix them if requested. The prompt says "output separate md document", implying documentation first.
- **Refinement**: I will document the *current* logic and highlight gaps for A-share trading.

## Step-by-Step
1.  **Fix NameError**: Import `Exchange` in `mysql_signal_strategy.py`.
2.  **Config Loading**: Implement `load_external_setting` in `MySQLSignalStrategy`.
3.  **Docs**: Create `docs/a_share_sim_logic.md`.
4.  **UI Check**: Verify `run_sim.py` setup.

