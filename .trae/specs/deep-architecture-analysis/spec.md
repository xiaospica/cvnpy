# Deep Architecture Analysis Spec

## Why
The previous architecture analysis in `architecture.md` was too superficial. The user requires a deep, code-level analysis of the `vnpy` software implementation principles, uncovering non-obvious details and mechanisms, complete with well-rendered diagrams (UML, sequence diagrams, architecture diagrams).

## What Changes
- Rewrite `architecture.md` to include deep dive analysis into core `vnpy` components.
- Analyze `EventEngine` internals (e.g., thread safety, timer events, specific decoupling mechanisms).
- Analyze `MainEngine` and `OmsEngine` internals (e.g., order lifecycle, offset conversion, memory data structures).
- Analyze `CtaEngine` and `BacktestingEngine` internals (e.g., target position template matching, tick vs bar simulation, stop order local simulation nuances).
- Analyze `QmtGateway` specifics (e.g., async vs sync behavior in A-share trading, remark id handling).
- Provide complex, highly detailed Mermaid diagrams (Class, Sequence, State, Architecture).

## Impact
- Affected specs: Documentation and system understanding.
- Affected code: `architecture.md`

## ADDED Requirements
### Requirement: Deep Architectural Insights
The document SHALL provide non-obvious insights into the system's design.

#### Scenario: Diagram Rendering
- **WHEN** the markdown is viewed
- **THEN** Mermaid diagrams should render correctly and display complex relationships.
