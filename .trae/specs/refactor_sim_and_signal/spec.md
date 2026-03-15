# Refactor QMT Sim and Signal Strategy Spec

## Why
The user has two modules, `vnpy_qmt_sim` and `vnpy_signal_strategy`, which need to be aligned with standard `vnpy` patterns for better testing and usability. 
1. `vnpy_qmt_sim` currently lacks the full interface parity with `vnpy_qmt`, making it unsuitable for transparent upper-layer logic verification.
2. `vnpy_signal_strategy` uses an ad-hoc UI for strategy creation which doesn't support the standard class-based strategy workflow found in `vnpy_ctastrategy`.

## What Changes

### 1. `vnpy_qmt_sim` Refactoring
- **Structure**: Split `gateway.py` into `gateway.py`, `md.py`, and `td.py` to mirror `vnpy_qmt`.
- **Interface**:
  - `QmtSimGateway` must instantiate `QmtSimMd` and `QmtSimTd`.
  - Implement all methods present in `QmtGateway` (e.g., `query_order`, `query_trade`, `query_account`, `query_position`).
- **Simulation**:
  - `SimulationCounter` must support all queries (returning mocked data).
  - Ensure `MdApi` and `TdApi` in sim delegate calls to `SimulationCounter` or return mock responses matching `vnpy_qmt` behavior.

### 2. `vnpy_signal_strategy` Refactoring
- **Strategy Loading**:
  - Update `SignalEngine` to load strategy **classes** from the `strategies` directory (like `CtaEngine`), rather than ad-hoc loading or relying on generic classes.
- **UI Overhaul**:
  - Refactor `SignalStrategyWidget` to mimic `CtaManager`.
  - **Remove**: Simple "Input Name -> Add" flow.
  - **Add**: "Select Class -> Input Name -> Configure Parameters -> Add" flow.
  - Support `init_all`, `start_all`, `stop_all` if applicable, or at least per-strategy controls matching CTA style.
  - Persist strategy settings to `signal_strategy_setting.json` (implied by "load via UI" standard behavior).

## Impact
- **Affected Specs**: None.
- **Affected Code**: 
  - `vnpy_qmt_sim/`
  - `vnpy_signal_strategy/`

## ADDED Requirements
### Requirement: QMT Sim Interface Parity
The `vnpy_qmt_sim` SHALL expose `md` and `td` attributes on the gateway instance, and support all public methods of `vnpy_qmt.QmtGateway`.

#### Scenario: Upper Layer Access
- **WHEN** upper layer accesses `gateway.td.query_position()`
- **THEN** it should execute without error and return simulated positions.

### Requirement: Signal Strategy Standard Workflow
The `vnpy_signal_strategy` SHALL allow users to define strategy classes in files, and instantiate them via UI with custom parameters.

#### Scenario: Add Strategy
- **WHEN** user opens "Add Strategy" dialog
- **THEN** user sees list of available strategy classes found in `strategies/` folder.
