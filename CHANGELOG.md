# OpenOPC Changelog

All notable changes to OpenOPC will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] - 2026-07-21

### Added

#### 1. Shadow Mode Execution Engine (`opc.layer3_agent.adapters.human_adapter`)
- **Event-Driven Human Contractor Pipeline**: Seamlessly pause company execution DAG on tasks assigned to human contractors and transition task sub-state to `AWAITING_HUMAN_DELIVERABLE`.
- **Reactive Event Bus (Zero-Polling)**: Integrated Layer 0 `MessageBus` reactive event model using `await message_bus.subscribe_once(f"deliverable_completed:{task.id}")` to block natively until a deliverable is uploaded via the contractor portal.

#### 2. Identity & Access Management Gateway (`opc.core.auth` & `opc.database.store`)
- **Zero-Dependency Security**: Built-in JWT token issuance/validation and PBKDF2 HMAC SHA256 password hashing.
- **Unified Employee Schema**: Extended `EMPLOYEES` table with `username`, `hashed_password`, `is_human` (boolean), and `access_level` (`admin` vs `worker`).
- **Contractor Seeding CLI**: CLI utility (`scripts/seed_contractor.py`) for administrative seeding of human contractor accounts.

#### 3. Temporal Organizational Knowledge & Performance Tracker
- **Time-Aware Vector Metadata (`opc.layer5_memory.memory_manager`)**: Injects `timestamp`, `year_month`, `epoch_week`, and `epoch_time` into all embedded documents, trajectories, and playbooks.
- **Time-Machine Memory Retrieval**: Added `query_temporal_memory(query, start_date, end_date)` to enable time-filtered knowledge queries.
- **Unified `ORG_CHANGELOGS` Table (`opc.database.store`)**: Created `org_changelogs` table storing `id`, `timestamp`, `event_type`, `actor_id`, `description`, `impact_score`, and `metadata`.
- **Observability Integration (`opc.layer6_observability.opc_logger`)**: Wired `OPCLogger` to write DAG trajectory steps and organizational milestones directly to `ORG_CHANGELOGS`.
- **3-Level Relational Performance Aggregation**: Implemented `store.get_temporal_performance(interval="weekly")` rolling up performance metrics across:
  1. `global`: Whole organization completion rates and active task volume over time.
  2. `team`: Role and team task completion velocity over time.
  3. `individual`: Contractor/employee event count and cumulative impact score over time.

#### 4. Streamlit Contractor Portal & Time-Machine Analytics Dashboard (`opc.presentation.human_portal`)
- **State & Refresh Persistence**: Preserves JWT authentication tokens across browser reloads using `st.query_params["token"]`.
- **Task Queue Tab**: Contractor task selector, deliverable summary text area, and multi-format file uploader (`.py`, `.md`, `.json`, etc.).
- **Time-Machine Analytics Tab**: Interactive visualizations for global velocity, team performance slices, individual contractor performance metrics, and searchable real-time organizational changelog feed.
