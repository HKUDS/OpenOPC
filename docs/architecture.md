<h1 align="center">OpenOPC Architecture Specification</h1>

<p align="center">
  <b>English</b> | <a href="architecture.zh-CN.md">简体中文</a>
</p>

Welcome to the **OpenOPC** architecture specification. This document provides an exhaustive, maintainable, and 100% accurate architectural reference for developers and open-source contributors. It details OpenOPC's 7-layer cognitive architecture, global system state flow, perception pipeline, work-item state machine, agent broker adapters, tool governance, memory evolution, persistence model, and real-time presentation topology.

---

## 1. System Architecture Overview

OpenOPC is an autonomous AI agent collaboration framework designed to build, run, and evolve a **One-Person Company (OPC)**. The platform is organized around a **7-Layer Cognitive Architecture** paired with infrastructure modules for persistence, LLM abstractions, talent markets, and user interfaces.

```mermaid
flowchart TB
    subgraph Presentation ["Presentation & Channels Layer"]
        CLI["CLI Suite (opc.cli.app)"]
        OfficeUI["Office UI Web App (opc.plugins.office_ui)"]
        CLIBoard["TUI Board (opc.plugins.cli_board)"]
        Channels["Multi-Channel Gateway (opc.channels)"]
    end

    subgraph Layer0 ["Layer 0: Interaction"]
        MsgBus["Message Bus (opc.layer0_interaction.message_bus)"]
    end

    subgraph Layer1 ["Layer 1: Perception"]
        CtxAssembler["Context Assembler (opc.layer1_perception.context_assembler)"]
        CtxLoader["Context Loader (opc.layer1_perception.context_loader)"]
        TaskRouter["Task Router (opc.layer1_perception.task_router)"]
    end

    subgraph Layer2 ["Layer 2: Organization Engine"]
        CompanyMode["Company Mode Orchestrator (opc.layer2_organization.company_mode)"]
        CompanyRuntime["Company Runtime (opc.layer2_organization.company_runtime)"]
        OrgEngine["Org Engine (opc.layer2_organization.org_engine)"]
        Recruiter["Recruiter & Staffing (opc.layer2_organization.recruiter)"]
        Approval["Governance & Approval (opc.layer2_organization.approval)"]
        Comms["Communication & Comms (opc.layer2_organization.comms)"]
        TurnMode["Turn Mode Manager (opc.layer2_organization.turn_mode)"]
    end

    subgraph Layer3 ["Layer 3: Agent & Execution"]
        NativeAgent["Native Agent Engine (opc.layer3_agent.native_agent)"]
        RuntimeV2["Agent Runtime V2 (opc.layer3_agent.runtime_v2)"]
        ExternalBroker["External Agent Broker (opc.layer3_agent.external_broker)"]
        Adapters["CLI/SDK Adapters (Codex, Claude, Cursor, OpenCode)"]
        PromptHarness["Prompt Harness Builder (opc.layer3_agent.prompt_harness)"]
    end

    subgraph Layer4 ["Layer 4: Tools & Capabilities"]
        BrowserTools["Browser Tools (Playwright)"]
        ShellTools["Shell & Python Exec"]
        FileTools["File & Git Ops"]
        CollabTools["Collaboration RPC"]
        SearchTools["Web Search & Todo"]
    end

    subgraph Layer5 ["Layer 5: Memory & Evolution (Self-Grown)"]
        MemManager["Memory Manager (opc.layer5_memory.memory_manager)"]
        EmpEvolution["Employee Evolution & Playbooks (opc.layer5_memory.employee_evolution)"]
        SkillLib["Skill Library & Importer (opc.layer5_memory.skill_library)"]
    end

    subgraph Layer6 ["Layer 6: Observability"]
        CostTracker["Cost Tracker (opc.layer6_observability.cost_tracker)"]
        Logger["Structured Logger (opc.layer6_observability.opc_logger)"]
    end

    subgraph CoreInfra ["Core & Storage Infrastructure"]
        Config["Core Config & Models (opc.core)"]
        DBStore["Async SQLite Store (opc.database.store)"]
        LLMProvider["LLM Provider / LiteLLM (opc.llm.provider)"]
        MarketRegistry["Talent Market & Presets (opc.market)"]
    end

    %% Flow connections
    Presentation --> Layer0
    Layer0 --> Layer1
    Layer1 --> Layer2
    Layer2 --> Layer3
    Layer3 --> Layer4
    Layer3 --> Layer5
    Layer2 --> Layer6
    Layer3 --> LLMProvider
    Layer2 --> DBStore
    Layer5 --> DBStore
```

---

## 2. Global System State Flow & Project Lifecycle

The global system lifecycle governs the macro-level state transitions of an OpenOPC project—from initial prompt submission to company staffing, work item DAG execution, governance pauses, deliverable review, and final trajectory learning.

```mermaid
stateDiagram-v2
    [*] --> IDLE : Session Created / Workspace Initialized

    IDLE --> STAFFING : Goal Submitted / Staffing Recruiter Triggered

    state STAFFING {
        [*] --> DerivingOrgChart : Recruiter Analyzes Goal
        DerivingOrgChart --> QueryingTalentMarket : Evaluate Presets & Past Hires
        QueryingTalentMarket --> AssigningSeats : Bind Roles to Employees / Contractors
        AssigningSeats --> RosterReady : Seat State Synchronized
    }

    STAFFING --> RUNNING : Org Roster & Work-Item DAG Ready

    state RUNNING {
        [*] --> SelectingRunnableItem : Phase Transition (QUEUED -> READY)
        SelectingRunnableItem --> DispatchedToRole : Seat Execution Lock Acquired
        
        state DispatchedToRole {
            [*] --> ContextAssembly : Layer 1 Prompt & History Assembly
            ContextAssembly --> AgentExecutionLoop : NativeRuntimeV2 / ExternalBroker
            
            state AgentExecutionLoop {
                [*] --> LLMInference
                LLMInference --> EvaluatingToolCall
                EvaluatingToolCall --> ToolExecution : ApprovalEngine Grants Permission
                ToolExecution --> LLMInference : Result Returned
            }

            AgentExecutionLoop --> OutputSubmitted : Work Item Submitted (Phase -> AWAITING_MANAGER_REVIEW)
        }

        DispatchedToRole --> ManagerReview : Manager Evaluation
        
        state ManagerReview {
            [*] --> EvaluatingOutputContract
            EvaluatingOutputContract --> ApprovedVerdict : Meets Artifact Contract
            EvaluatingOutputContract --> RejectedVerdict : Fails Quality / Lint Checks
        }

        ManagerReview --> ReworkLoop : Rejected (Phase -> READY_FOR_REWORK)
        ReworkLoop --> DispatchedToRole : Re-dispatched to Worker Role
    }

    RUNNING --> PAUSED_GOVERNANCE : ApprovalEngine Requires Escalation / User Input
    PAUSED_GOVERNANCE --> RUNNING : Approval Granted by Owner

    RUNNING --> AWAITING_HUMAN : Assigned to Human Contractor (Shadow Mode)
    AWAITING_HUMAN --> RUNNING : Deliverable Submitted via MessageBus / SQLite

    RUNNING --> COMPLETED : All Work-Item DAG Nodes Approved / Closed
    RUNNING --> FAILED : Unrecoverable Exception / Max Retries Exceeded

    state COMPLETED {
        [*] --> DistillingExperience : Layer 5 Trajectory Learning
        DistillingExperience --> SynthesizingPlaybooks : Promote Recurring Lessons to SkillLibrary
        SynthesizingPlaybooks --> MemoryEvolved : Evolved Employee Profiles & Playbooks Saved
    }

    COMPLETED --> [*] : Session Closed
    FAILED --> [*] : Error Logged
```

---

## 3. Self-Built, Self-Run, Self-Grown Operational Lifecycle

OpenOPC operates on three self-sustaining feedback loops:

1. **Self-Built (Staffing)**: Translates user goals into role structures and recruits optimal AI employees from talent presets or prior project experience.
2. **Self-Run (Execution)**: Decomposes tasks into dynamic work-item DAGs, assigning ownership and executing through parallel agent sessions with automated review and rework cycles.
3. **Self-Grown (Learning)**: Attributes project results to individual roles, distills raw interaction trajectories into private role experiences, and promotes recurring patterns to company-wide playbooks.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Presentation as UI / CLI / Channels
    participant Recruiter as Recruiter & Org Engine (Self-Built)
    participant CompanyRuntime as Company Runtime (Self-Run)
    participant AgentBroker as Agent Engine & Tools
    participant Evolution as Employee Evolution (Self-Grown)
    participant DB as SQLite / Vector Store

    %% Self-Built Phase
    User->>Presentation: Submit Goal / Task Prompt
    Presentation->>Recruiter: Request Company Staffing
    Recruiter->>DB: Query Talent Presets & Past Employee Experience
    DB-->>Recruiter: Employee Profiles
    Recruiter->>Recruiter: Derive Org Chart & Hire Roles
    Recruiter-->>CompanyRuntime: Org Ready (Roles & Employees Staffed)

    %% Self-Run Phase
    CompanyRuntime->>CompanyRuntime: Decompose Goal into Work-Item DAG
    loop For each runnable Work Item in DAG
        CompanyRuntime->>AgentBroker: Execute Work Item (Role context + Tools)
        AgentBroker->>AgentBroker: Run Tools / LLM Iterations
        AgentBroker-->>CompanyRuntime: Submit Work Item Output
        CompanyRuntime->>CompanyRuntime: Manager Review (Accept / Rework / Escalate)
        alt Needs Rework
            CompanyRuntime->>AgentBroker: Trigger Rework Loop
        else Escalated Blocker
            CompanyRuntime->>Presentation: Request Human Approval / Guidance
            User-->>Presentation: Grant Decision / Input
            Presentation-->>CompanyRuntime: Resume Execution
        end
    end
    CompanyRuntime-->>Presentation: Final Deliverable Complete

    %% Self-Grown Phase
    User->>Presentation: Feedback & Outcome Rating
    Presentation->>Evolution: Trigger Trajectory Distillation
    Evolution->>DB: Fetch Execution Trajectories
    Evolution->>Evolution: Credit/Blame Attribution per Employee Role
    Evolution->>Evolution: Synthesize Private Experience & Shared Playbooks
    Evolution->>DB: Store Evolved Employee Memory & Playbooks
```

---

## 4. Layer 1: Perception & Context Assembly Architecture

Layer 1 (`opc/layer1_perception/`) prepares the complete context required for agent decisions before any LLM inference or external CLI invocation.

```mermaid
flowchart TD
    subgraph ContextInputs ["Raw Context Sources"]
        UserPrompt["User Prompt & Task Goal"]
        FileAttachments["Files & Attachments (context_loader.py)"]
        RolePersona["Role & Employee Profile (org_config.py)"]
        OrgPlaybooks["Company & Shared Playbooks"]
        ExecutionHistory["Work Item Step History"]
        VectorMem["ChromaDB Vector Retrieval (memory_manager.py)"]
    end

    subgraph AssemblyPipeline ["Context Assembler (context_assembler.py)"]
        CompactHistory["History Compactor & Truncation"]
        RAGSearch["Semantic Memory & Skill Retrieval"]
        FormatContract["Output Contract & Response Schema Injection"]
        ToolStrategy["Tool Strategy & Available Function Specs"]
    end

    subgraph OutputPayload ["Assembled Execution Context"]
        SystemPrompt["System Prompt (PromptHarnessBuilder)"]
        UserMessagePayload["Structured User Payload & Attachments"]
        ToolDefinitions["Tool Declarations & JSON Schemas"]
    end

    UserPrompt --> AssemblyPipeline
    FileAttachments --> AssemblyPipeline
    RolePersona --> AssemblyPipeline
    OrgPlaybooks --> AssemblyPipeline
    ExecutionHistory --> CompactHistory
    VectorMem --> RAGSearch

    CompactHistory --> AssemblyPipeline
    RAGSearch --> AssemblyPipeline
    FormatContract --> AssemblyPipeline
    ToolStrategy --> AssemblyPipeline

    AssemblyPipeline --> SystemPrompt
    AssemblyPipeline --> UserMessagePayload
    AssemblyPipeline --> ToolDefinitions
```

---

## 5. Layer 2: Organization Engine & Work-Item State Machine

The Organization Engine (`opc/layer2_organization/`) manages dynamic work-item orchestration. Each work item progresses through a strict state machine with defined ownership transitions.

```mermaid
stateDiagram-v2
    [*] --> QUEUED : Work Item Created (Manager Holding)

    QUEUED --> READY : Released / Dependencies Resolved
    READY --> IN_PROGRESS : Dispatched & Execution Lock Acquired

    state IN_PROGRESS {
        [*] --> ExecutingWorkItem
        ExecutingWorkItem --> BLOCKED : Missing Dependency / Resource
        BLOCKED --> ExecutingWorkItem : Blocker Resolved
        ExecutingWorkItem --> AWAITING_HUMAN_DELIVERABLE : Assigned to Human (Shadow Mode)
        AWAITING_HUMAN_DELIVERABLE --> ExecutingWorkItem : Deliverable Submitted
    }

    IN_PROGRESS --> AWAITING_PEER_REVIEW : Worker Submits for Peer Review
    AWAITING_PEER_REVIEW --> AWAITING_MANAGER_REVIEW : Peer Approval Granted
    IN_PROGRESS --> AWAITING_MANAGER_REVIEW : Worker Submits directly to Manager

    state AWAITING_MANAGER_REVIEW {
        [*] --> ManagerEvaluating
        ManagerEvaluating --> APPROVED : Output Meets Contract
        ManagerEvaluating --> READY_FOR_REWORK : Output Fails Review (Rework)
        ManagerEvaluating --> REJECTED : Terminal Rejection
    }

    READY_FOR_REWORK --> IN_PROGRESS : Re-dispatched to Worker Role
    APPROVED --> CLOSED : Deliverable Merged & Closed
    SUPERSEDED --> [*] : Replaced by Reorg
    CANCELLED --> [*] : Cancelled by User
    FAILED --> [*] : Timeout / Unrecoverable Error
    CLOSED --> [*] : Complete
```

### Collaboration Modes

The Manager Role delegates and coordinates work across five distinct operational modes:
- **`execute`**: Direct execution of single-step tasks by an individual agent.
- **`delegate`**: Breakdown and assignment of work items to subordinate role agents.
- **`review`**: Evaluation of submitted work items against defined acceptance criteria.
- **`integrate`**: Combination of sub-task outputs into a cohesive final deliverable.
- **`rework`**: Targeted re-assignment with specific correction guidelines following rejection.

---

## 6. Layer 3: Agent Execution & External Broker Adapters

Layer 3 (`opc/layer3_agent/`) decouples task execution from specific LLM vendors or CLI tools. Tasks can run natively via OpenOPC's `NativeAgent` / `NativeRuntimeV2` engine or be delegated to external coding agents via `ExternalAgentBroker`.

```mermaid
classDiagram
    class CompanyRuntimeContract {
        +execute_work_item()
        +stream_events()
    }

    class NativeAgent {
        +run_step()
        +invoke_tool()
    }

    class NativeRuntimeV2 {
        +StreamingToolExecutor executor
        +SubagentManager subagents
        +PermissionManager permissions
        +WorktreeManager worktree
        +execute_loop()
    }

    class ExternalAgentBroker {
        +execute_task()
        +select_best_external_resume_session()
    }

    class ExternalAgentAdapter {
        <<abstract>>
        +execute()
        +health_check()
    }

    class CodexAdapter {
        +execute_codex_cli()
    }

    class ClaudeCodeAdapter {
        +execute_claude_cli()
    }

    class CursorAdapter {
        +execute_cursor_cli()
    }

    class OpenCodeAdapter {
        +execute_opencode_cli()
    }

    class PromptHarnessBuilder {
        +build_system_prompt()
        +attach_role_context()
        +attach_playbooks()
    }

    CompanyRuntimeContract <|-- NativeAgent
    CompanyRuntimeContract <|-- ExternalAgentBroker
    NativeAgent --> NativeRuntimeV2
    NativeAgent --> PromptHarnessBuilder
    ExternalAgentBroker --> ExternalAgentAdapter
    ExternalAgentAdapter <|-- CodexAdapter
    ExternalAgentAdapter <|-- ClaudeCodeAdapter
    ExternalAgentAdapter <|-- CursorAdapter
    ExternalAgentAdapter <|-- OpenCodeAdapter
```

---

## 7. Layer 4: Tool Execution Engine & Governance Pipeline

Layer 4 (`opc/layer4_tools/`) provides executable capabilities. Every tool invocation passes through a multi-tier safety pipeline before reaching execution handlers.

```mermaid
flowchart LR
    subgraph ToolCallEmitter ["Agent Execution Layer"]
        AgentCall["Agent Emits Tool Call Request"]
    end

    subgraph GovernanceEngine ["Governance & Approval Engine"]
        ApprovalEngine["Approval Engine (opc.layer0_interaction.approval_engine)"]
        PermMgr["Permission Manager (opc.layer3_agent.runtime_v2.permissions)"]
        Allowlist["Approval Allowlist (opc.layer5_memory.approval_allowlist)"]
        ShellSafety["Shell Safety Guard (opc.layer2_organization.shell_safety)"]
    end

    subgraph ToolHandlers ["Layer 4 Execution Handlers"]
        Browser["Playwright Browser (browser.py)"]
        ShellExec["Shell / Python Exec (shell.py, python_exec.py)"]
        FileOps["File & Git Ops (file_ops.py, git_ops.py)"]
        CollabRPC["Collaboration RPC (collaboration_rpc.py)"]
        SearchTodo["Web Search & Todo (web_search.py, todo.py)"]
    end

    subgraph Sanitization ["Output Budget & Truncation"]
        OutputBudget["Output Budget (output_budget.py)"]
    end

    AgentCall --> ApprovalEngine
    ApprovalEngine --> PermMgr
    ApprovalEngine --> Allowlist
    ApprovalEngine --> ShellSafety
    
    Allowlist -->|Match Active Session Grant| ToolHandlers
    ShellSafety -->|Safe Shell Command| ToolHandlers
    ShellSafety -->|Unsafe / Destructive| Escalation["Escalate to Owner / Reject"]
    ApprovalEngine -->|Approved| ToolHandlers

    ToolHandlers --> Browser
    ToolHandlers --> ShellExec
    ToolHandlers --> FileOps
    ToolHandlers --> CollabRPC
    ToolHandlers --> SearchTodo

    Browser --> OutputBudget
    ShellExec --> OutputBudget
    FileOps --> OutputBudget
    CollabRPC --> OutputBudget
    SearchTodo --> OutputBudget

    OutputBudget --> Result["Return Truncated & Sanitized Tool Result"]
```

---

## 8. Layer 5: Memory Evolution & Playbook Promotion Mechanism

Layer 5 (`opc/layer5_memory/`) drives the **Self-Grown** loop, transforming raw execution traces into role-specific knowledge and company-wide playbooks.

```mermaid
flowchart TB
    subgraph TrajectoryCapture ["Execution Trajectory Logging"]
        RawTrace["Raw Execution Trajectories (store.py)"]
        UserFeedback["User Outcome Evaluation & Rating"]
    end

    subgraph EvolutionEngine ["Employee Evolution Engine (employee_evolution.py)"]
        Compactor["History Compactor (history_compactor.py)"]
        Attributor["Credit & Blame Attribution Analyzer"]
        ExperienceDistiller["Private Role Lesson Synthesizer"]
        PlaybookPromoter["Company Playbook Promotion Engine"]
    end

    subgraph MemoryStorage ["Persistent Memory Stores"]
        PrivateMem["Employee Private Experience Profile (.json)"]
        SharedPlaybooks["Company Shared Playbooks Library (skill_library.py)"]
        VectorDB["ChromaDB Vector Embeddings (memory_manager.py)"]
    end

    RawTrace --> Compactor
    UserFeedback --> Attributor
    Compactor --> Attributor
    Attributor --> ExperienceDistiller
    ExperienceDistiller --> PrivateMem

    ExperienceDistiller --> PlaybookPromoter
    PlaybookPromoter -->|Recurring High-Signal Pattern| SharedPlaybooks
    
    PrivateMem --> VectorDB
    SharedPlaybooks --> VectorDB
```

---

## 9. Real-Time Office UI & Multi-Channel Synchronization Topology

OpenOPC provides a real-time web workspace (Office UI) powered by FastAPI and WebSockets, alongside multi-channel messaging integrations.

```mermaid
flowchart LR
    subgraph Clients ["User Interfaces & Clients"]
        WebBrowser["Browser Office UI (React + Phaser 3)"]
        TerminalCLI["Interactive CLI (opc chat / opc exec)"]
        TUIBoard["Terminal Board (opc.plugins.cli_board)"]
    end

    subgraph MultiChannel ["Messaging Gateways"]
        TG["Telegram"]
        DC["Discord"]
        FS["Feishu"]
        SL["Slack"]
        DD["DingTalk"]
        QQ["QQ Bot"]
        MX["Matrix"]
        MC["Mochat"]
        WA["WhatsApp"]
        EM["Email"]
    end

    subgraph ServerBackend ["OpenOPC Plugin & Gateway Server"]
        ChannelManager["Channel Manager (opc.channels.manager)"]
        OfficeUIServer["Office UI Server (FastAPI + WebSockets)"]
        WSHandler["WebSocket Event Handler (ws_handler.py)"]
        SnapshotBuilder["Org Snapshot Builder (snapshot_builder.py)"]
    end

    subgraph EngineCore ["OpenOPC Core Engine"]
        MessageBus["Layer 0 Message Bus"]
        CompanyMode["Layer 2 Company Mode"]
    end

    WebBrowser <-->|WebSocket / REST| OfficeUIServer
    OfficeUIServer --> WSHandler
    WSHandler --> SnapshotBuilder
    SnapshotBuilder --> CompanyMode

    TerminalCLI --> CompanyMode
    TUIBoard --> CompanyMode

    MultiChannel <--> ChannelManager
    ChannelManager <--> MessageBus
    MessageBus <--> CompanyMode
```

---

## 10. Data Model & Persistence Architecture

OpenOPC utilizes asynchronous SQLite (`aiosqlite`) for structured entity storage alongside ChromaDB vector storage for role memory.

```mermaid
erDiagram
    delegation_work_items ||--o{ tasks : projects_to_runtime
    delegation_work_items ||--o{ artifact_records : produces
    seat_states ||--o{ delegation_work_items : executes
    employees ||--o{ seat_states : fills_seat
    tasks ||--o{ cost_events : incurs_cost
    sessions ||--o{ runtime_permission_grants : scopes_permission
    delegation_work_items ||--o{ org_changelogs : records_event

    delegation_work_items {
        string work_item_id PK
        string run_id
        string project_id
        string title
        string phase
        string owner_role_id
        string parent_work_item_id FK
        json dependencies
    }

    tasks {
        string id PK
        string session_id
        string project_id
        string title
        string status
        string assigned_to
        string assigned_external_agent
        json metadata
        json result
    }

    seat_states {
        string seat_state_id PK
        string team_instance_id
        string role_id
        string seat_id
        string employee_id FK
        string status
        string current_work_item_id FK
    }

    employees {
        string employee_id PK
        string name
        string role_id
        string username
        string is_human
        string access_level
    }

    artifact_records {
        string artifact_id PK
        string task_id FK
        string work_item_id FK
        string path
        string category
    }

    runtime_permission_grants {
        string grant_id PK
        string session_id FK
        string tool_name
        string scope
        string status
    }

    org_changelogs {
        string log_id PK
        string org_id
        string event_type
        string actor_id
        string description
        string timestamp
    }

    vector_documents {
        string doc_id PK
        string text
        string timestamp
        string year_month
        string epoch_week
        float epoch_time
        json metadata
    }
```

---

## 11. Module & Directory Map Reference

| Directory / Package | Layer / Scope | Primary Responsibility | Key Files |
| :--- | :--- | :--- | :--- |
| [`opc/core/`](../opc/core) | Core | Data models, configuration schemas, active task trackers, and runtime envelopes. | [`config.py`](../opc/core/config.py), [`models.py`](../opc/core/models.py), [`employee_registry.py`](../opc/core/employee_registry.py) |
| [`opc/layer0_interaction/`](../opc/layer0_interaction) | Layer 0 | System event message bus and interaction decoupling. | [`message_bus.py`](../opc/layer0_interaction/message_bus.py) |
| [`opc/layer1_perception/`](../opc/layer1_perception) | Layer 1 | Context assembly, document loading, and task intent routing. | [`context_assembler.py`](../opc/layer1_perception/context_assembler.py), [`context_loader.py`](../opc/layer1_perception/context_loader.py) |
| [`opc/layer2_organization/`](../opc/layer2_organization) | Layer 2 | Company Mode orchestration, org engine, recruiter, work-item DAG state machine, and governance. | [`company_mode.py`](../opc/layer2_organization/company_mode.py), [`company_runtime.py`](../opc/layer2_organization/company_runtime.py), [`recruiter.py`](../opc/layer2_organization/recruiter.py), [`approval.py`](../opc/layer2_organization/approval.py) |
| [`opc/layer3_agent/`](../opc/layer3_agent) | Layer 3 | Native execution engine, Runtime V2, external agent broker, adapters (Codex, Claude, Cursor, OpenCode), and prompt builder. | [`native_agent.py`](../opc/layer3_agent/native_agent.py), [`external_broker.py`](../opc/layer3_agent/external_broker.py), [`adapters/`](../opc/layer3_agent/adapters) |
| [`opc/layer4_tools/`](../opc/layer4_tools) | Layer 4 | Executable capabilities including Playwright browser, shell execution, file ops, web search, todo, and collaboration RPC. | [`browser.py`](../opc/layer4_tools/browser.py), [`shell.py`](../opc/layer4_tools/shell.py), [`collaboration.py`](../opc/layer4_tools/collaboration.py) |
| [`opc/layer5_memory/`](../opc/layer5_memory) | Layer 5 | Vector/Markdown memory, trajectory distillation, credit attribution, private role experience, and shared playbooks. | [`memory_manager.py`](../opc/layer5_memory/memory_manager.py), [`employee_evolution.py`](../opc/layer5_memory/employee_evolution.py), [`skill_library.py`](../opc/layer5_memory/skill_library.py) |
| [`opc/layer6_observability/`](../opc/layer6_observability) | Layer 6 | Token cost monitoring and structured logging engine. | [`cost_tracker.py`](../opc/layer6_observability/cost_tracker.py), [`opc_logger.py`](../opc/layer6_observability/opc_logger.py) |
| [`opc/cli/`](../opc/cli) | Interface | Typer-based CLI interface suite (`opc init`, `opc chat`, `opc ui`, `opc exec`). | [`app.py`](../opc/cli/app.py) |
| [`opc/channels/`](../opc/channels) | Gateway | Multi-channel communication adapters (Telegram, Discord, Feishu, Slack, DingTalk, Matrix, etc.). | [`manager.py`](../opc/channels/manager.py), [`base.py`](../opc/channels/base.py) |
| [`opc/database/`](../opc/database) | Persistence | Asynchronous SQLite relational persistence store. | [`store.py`](../opc/database/store.py) |
| [`opc/llm/`](../opc/llm) | LLM | Unified LiteLLM integration layer with multi-provider retry and fallback. | [`provider.py`](../opc/llm/provider.py), [`retry.py`](../opc/llm/retry.py) |
| [`opc/market/`](../opc/market) | Marketplace | Talent presets, company architecture presets, package import/export, and sandbox validation. | [`architecture_registry.py`](../opc/market/architecture_registry.py), [`talent_presets.py`](../opc/market/talent_presets.py) |
| [`opc/plugins/office_ui/`](../opc/plugins/office_ui) | Interface | FastAPI web backend server, WebSocket event engine, Phaser 3 Office visualization, and React Workspace. | [`server.py`](../opc/plugins/office_ui/server.py), [`ws_handler.py`](../opc/plugins/office_ui/ws_handler.py), [`snapshot_builder.py`](../opc/plugins/office_ui/snapshot_builder.py) |

---

## 12. Contributor Implementation Guidelines

When contributing new capabilities to OpenOPC:
1. **Adding a New Tool**: Register the tool definition in [`opc/layer4_tools/registry.py`](../opc/layer4_tools/registry.py) and implement the handler in [`opc/layer4_tools/`](../opc/layer4_tools).
2. **Adding an External Agent Adapter**: Subclass `BaseAgentAdapter` in [`opc/layer3_agent/adapters/base.py`](../opc/layer3_agent/adapters/base.py) and register it in [`opc/layer3_agent/adapters/registry.py`](../opc/layer3_agent/adapters/registry.py).
3. **Adding a Channel Provider**: Subclass `BaseChannelProvider` in [`opc/channels/provider_base.py`](../opc/channels/provider_base.py) and register in [`opc/channels/provider_registry.py`](../opc/channels/provider_registry.py).
4. **Modifying Governance / Approvals**: Update policy logic in [`opc/layer2_organization/approval.py`](../opc/layer2_organization/approval.py).
