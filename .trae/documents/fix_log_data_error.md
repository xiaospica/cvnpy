# Fix LogData Access Error Plan

## 1. Fix `TypeError: 'LogData' object is not subscriptable`

The error occurs in `LogMonitor.insert_new_row` method in `vnpy_signal_strategy/ui/widget.py`. The code attempts to access `gateway_name` from `data` (which is a `LogData` object) using dictionary syntax `data["gateway_name"]`.

### Steps:
1.  **Locate Code**: `vnpy_signal_strategy/ui/widget.py`, line 377.
2.  **Modify Code**: Change `data["gateway_name"]` to `data.gateway_name`.

## 2. Verification

### Steps:
1.  **Run Simulation**: Execute `python run_sim.py` again to ensure the error is resolved and logs are displayed correctly.
