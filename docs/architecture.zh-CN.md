<h1 align="center">OpenOPC 系统架构规范</h1>

<p align="center">
  <a href="architecture.md">English</a> | <b>简体中文</b>
</p>

欢迎阅读 **OpenOPC** 系统架构文档。本文档为开发者和开源贡献者提供完整、结构化且易于维护的架构参考，详细介绍了 OpenOPC 的 7 层认知架构、全局系统状态流、感知管道、工作项状态机、Agent 代理适配器、工具治理管道、记忆进化机制、持久化模型以及多渠道交互接口。

---

## 1. 系统架构总览 (System Architecture Overview)

OpenOPC 是一个专为构建、运行与演进**一人公司 (One-Person Company, OPC)** 打造的自主 AI Agent 协作框架。平台核心基于 **7 层认知架构 (7-Layer Cognitive Architecture)**，并辅以持久化存储、LLM 抽象层、人才市场及多端用户界面等基础设施模块。

```mermaid
flowchart TB
    subgraph Presentation ["展示与渠道层 (Presentation & Channels)"]
        CLI["CLI 命令行工具 (opc.cli.app)"]
        OfficeUI["Office UI Web 应用 (opc.plugins.office_ui)"]
        CLIBoard["TUI 终端看板 (opc.plugins.cli_board)"]
        Channels["多渠道网关 (opc.channels)"]
    end

    subgraph Layer0 ["Layer 0: 交互层 (Interaction)"]
        MsgBus["消息总线 (opc.layer0_interaction.message_bus)"]
    end

    subgraph Layer1 ["Layer 1: 感知层 (Perception)"]
        CtxAssembler["上下文组装器 (opc.layer1_perception.context_assembler)"]
        CtxLoader["上下文加载器 (opc.layer1_perception.context_loader)"]
        TaskRouter["任务路由 (opc.layer1_perception.task_router)"]
    end

    subgraph Layer2 ["Layer 2: 组织引擎 (Organization)"]
        CompanyMode["公司模式编排器 (opc.layer2_organization.company_mode)"]
        CompanyRuntime["公司运行时 (opc.layer2_organization.company_runtime)"]
        OrgEngine["组织引擎 (opc.layer2_organization.org_engine)"]
        Recruiter["招聘与人员配置 (opc.layer2_organization.recruiter)"]
        Approval["治理与审批 (opc.layer2_organization.approval)"]
        Comms["组织通信 (opc.layer2_organization.comms)"]
        TurnMode["轮次模式管理 (opc.layer2_organization.turn_mode)"]
    end

    subgraph Layer3 ["Layer 3: Agent 与执行层 (Agent & Execution)"]
        NativeAgent["原生 Agent 引擎 (opc.layer3_agent.native_agent)"]
        RuntimeV2["Agent 运行时 V2 (opc.layer3_agent.runtime_v2)"]
        ExternalBroker["外部 Agent 代理 (opc.layer3_agent.external_broker)"]
        Adapters["CLI/SDK 适配器 (Codex, Claude, Cursor, OpenCode)"]
        PromptHarness["Prompt 构建器 (opc.layer3_agent.prompt_harness)"]
    end

    subgraph Layer4 ["Layer 4: 工具与能力层 (Tools & Capabilities)"]
        BrowserTools["浏览器工具 (Playwright)"]
        ShellTools["Shell & Python 执行"]
        FileTools["文件与 Git 操作"]
        CollabTools["协作 RPC"]
        SearchTools["网络搜索与待办事项"]
    end

    subgraph Layer5 ["Layer 5: 记忆与进化层 (Self-Grown)"]
        MemManager["记忆管理器 (opc.layer5_memory.memory_manager)"]
        EmpEvolution["员工进化与剧本 (opc.layer5_memory.employee_evolution)"]
        SkillLib["技能库与导入器 (opc.layer5_memory.skill_library)"]
    end

    subgraph Layer6 ["Layer 6: 可观测性层 (Observability)"]
        CostTracker["成本追踪 (opc.layer6_observability.cost_tracker)"]
        Logger["结构化日志 (opc.layer6_observability.opc_logger)"]
    end

    subgraph CoreInfra ["核心与存储基础设施 (Core Infrastructure)"]
        Config["核心配置与模型 (opc.core)"]
        DBStore["异步 SQLite 存储 (opc.database.store)"]
        LLMProvider["LLM 提供商 / LiteLLM (opc.llm.provider)"]
        MarketRegistry["人才市场与预设 (opc.market)"]
    end

    %% 流向连接
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

## 2. 全局系统状态流与项目生命周期 (Global System State Flow)

全局系统生命周期管理着 OpenOPC 项目的宏观状态转换 — 从初始 Prompt 提交到公司人员招聘、工作项 DAG 动态执行、治理中断审批、交付物审阅以及最终轨迹蒸馏。

```mermaid
stateDiagram-v2
    [*] --> UNINITIALIZED : 系统启动 / 加载工作区

    UNINITIALIZED --> CONFIGURING : opc init / 选择预设
    CONFIGURING --> STAFFING : 提交目标 (自建阶段)

    state STAFFING {
        [*] --> DerivingOrgChart : 推导组织架构图
        DerivingOrgChart --> QueryingTalentMarket : 评估预设与历史雇员
        QueryingTalentMarket --> HiringEmployees : 角色与员工匹配招聘
        HiringEmployees --> OrgStaffed : 团队花名册组建完成
    }

    STAFFING --> ORCHESTRATING : 组织花名册就位

    state ORCHESTRATING {
        [*] --> DecomposingGoal : 组织引擎生成工作项 DAG
        DecomposingGoal --> SchedulingDAG : 解析工作项前置依赖
        SchedulingDAG --> DispatchingWorkItem : 抽取可运行工作项
    }

    ORCHESTRATING --> EXECUTING_ITEM : 工作项分发至执行角色

    state EXECUTING_ITEM {
        [*] --> AssemblingContext : Layer 1 上下文组装
        AssemblingContext --> AgentLoop : NativeAgent / ExternalBroker 迭代
        
        state AgentLoop {
            [*] --> LLMInference : LLM 推理
            LLMInference --> EvaluatingToolCall : 校验工具调用
            EvaluatingToolCall --> ExecutingTool : 治理审批通过并执行工具
            ExecutingTool --> LLMInference : 附加工具执行结果
        }

        AgentLoop --> AWAITING_HUMAN_APPROVAL : 高风险操作 / 运行阻碍
        AWAITING_HUMAN_APPROVAL --> AgentLoop : 人类授权决策 / 输入指导
        AgentLoop --> SubmittingOutput : 生成工作项产出
    }

    EXECUTING_ITEM --> IN_MANAGER_REVIEW : 执行角色提交工作项

    state IN_MANAGER_REVIEW {
        [*] --> ReviewingDeliverable : 审阅交付物
        ReviewingDeliverable --> Accepted : 满足输出契约要求
        ReviewingDeliverable --> Rejected : 未达验收标准
    }

    IN_MANAGER_REVIEW --> REWORKING : 已拒绝并附带修改建议 (Reworking)
    REWORKING --> EXECUTING_ITEM : 重新分发至执行角色

    IN_MANAGER_REVIEW --> ORCHESTRATING : 已接受 (DAG 中仍有待执行工作项)
    IN_MANAGER_REVIEW --> PROJECT_COMPLETE : 已接受 (DAG 中所有工作项均已完成)

    state PROJECT_COMPLETE {
        [*] --> GeneratingFinalReport : 生成最终报告
        GeneratingFinalReport --> DistillingTrajectories : 自成长阶段
        
        state DistillingTrajectories {
            [*] --> RatingOutcomes : 解析用户评价反馈
            RatingOutcomes --> AttributingCredit : 角色归因评级
            AttributingCredit --> UpdatingPrivateExperience : 更新角色私有经验
            UpdatingPrivateExperience --> SynthesizingPlaybooks : 提升高频经验模式
        }

        DistillingTrajectories --> MemoryEvolved : 共享公司剧本库已更新
    }

    PROJECT_COMPLETE --> [*] : 项目归档 / 空闲
```

---

## 3. 自建、自运转、自成长运行生命周期 (Operational Lifecycle)

OpenOPC 围绕三大自我驱动的反哺闭环运行：

1. **自建 (Self-Built - 组织配置)**: 将用户目标转化为组织架构与角色定义，并从人才预设或历史项目积累中招聘最匹配的 AI 员工。
2. **自运转 (Self-Run - 任务执行)**: 将复杂任务拆解为动态工作项 DAG (有向无环图)，分配负责人并在并行 Agent 会话中自动执行审阅与重作循环。
3. **自成长 (Self-Grown - 经验学习)**: 将项目交付结果归因至具体角色，提取原始交互轨迹为角色私有经验，并将复用模式提升为全公司共享剧本 (Playbook)。

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户
    participant Presentation as 界面 / CLI / 渠道
    participant Recruiter as 招聘器与组织引擎 (自建)
    participant CompanyRuntime as 公司运行时 (自运转)
    participant AgentBroker as Agent 引擎与工具
    participant Evolution as 员工进化 (自成长)
    participant DB as SQLite / 向量数据库

    %% 自建阶段
    User->>Presentation: 提交目标 / 任务 Prompt
    Presentation->>Recruiter: 请求组织人员配置
    Recruiter->>DB: 查询人才预设与历史员工经验
    DB-->>Recruiter: 返回员工档案
    Recruiter->>Recruiter: 生成组织架构图并招聘角色
    Recruiter-->>CompanyRuntime: 组织组建完成 (角色与员工就位)

    %% 自运转阶段
    CompanyRuntime->>CompanyRuntime: 分解目标为工作项 DAG
    loop 循环执行 DAG 中处于可运行状态的工作项
        CompanyRuntime->>AgentBroker: 执行工作项 (携带角色上下文与工具)
        AgentBroker->>AgentBroker: 运行工具 / LLM 迭代
        AgentBroker-->>CompanyRuntime: 提交工作项产出
        CompanyRuntime->>CompanyRuntime: 管理者审阅 (接受 / 重作 / 升级)
        alt 需要重作
            CompanyRuntime->>AgentBroker: 触发重作循环
        else 遇到阻碍升级
            CompanyRuntime->>Presentation: 请求人类审批 / 决策
            User-->>Presentation: 授权决策 / 输入指导
            Presentation-->>CompanyRuntime: 恢复执行
        end
    end
    CompanyRuntime-->>Presentation: 最终交付物完成

    %% 自成长阶段
    User->>Presentation: 提交反馈与评价
    Presentation->>Evolution: 触发轨迹提炼与蒸馏
    Evolution->>DB: 获取执行轨迹
    Evolution->>Evolution: 按员工角色进行归因评级
    Evolution->>Evolution: 合成私有经验与共享剧本
    Evolution->>DB: 存储进化后的员工记忆与剧本
```

---

## 4. Layer 1: 感知与上下文组装架构 (Perception & Context Assembly)

Layer 1 (`opc/layer1_perception/`) 在任何 LLM 推理或外部 CLI 调用前，负责准备 Agent 决策所需的完整上下文。

```mermaid
flowchart TD
    subgraph ContextInputs ["原始上下文来源"]
        UserPrompt["用户 Prompt 与任务目标"]
        FileAttachments["文件与附件 (context_loader.py)"]
        RolePersona["角色与员工档案 (org_config.py)"]
        OrgPlaybooks["公司与共享剧本 (Playbooks)"]
        ExecutionHistory["工作项步骤历史"]
        VectorMem["ChromaDB 向量检索 (memory_manager.py)"]
    end

    subgraph AssemblyPipeline ["上下文组装器 (context_assembler.py)"]
        CompactHistory["历史压缩与截断"]
        RAGSearch["语义记忆与技能检索"]
        FormatContract["输出契约与响应 Schema 注入"]
        ToolStrategy["工具策略与可用函数 Spec"]
    end

    subgraph OutputPayload ["组装后的执行 Payload"]
        SystemPrompt["系统 Prompt (PromptHarnessBuilder)"]
        UserMessagePayload["结构化用户 Payload 与附件"]
        ToolDefinitions["工具声明与 JSON Schema"]
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

## 5. Layer 2: 组织引擎与工作项状态机 (Work-Item State Machine)

组织引擎 (`opc/layer2_organization/`) 负责管理动态工作项编排。每个工作项遵循严格的状态机进行流转与所有权交接。

```mermaid
stateDiagram-v2
    [*] --> PENDING : 工作项已创建 (Pending)

    PENDING --> READY : 前置依赖已解决 (Ready)
    READY --> RUNNING : 已分配角色并分发 (Running)

    state RUNNING {
        [*] --> Executing : 执行中
        Executing --> AWAITING_HUMAN_DELIVERABLE : 人类外包角色交接 (Shadow Mode)
        AWAITING_HUMAN_DELIVERABLE --> Executing : 通过 Layer 0 事件完成交付
        Executing --> WaitingApproval : 触发破坏性/高风险操作
        WaitingApproval --> Executing : 审批通过
        Executing --> Blocked : 遇到运行阻碍/缺少信息
        Blocked --> Executing : 阻碍已解除
    }

    RUNNING --> IN_REVIEW : 执行角色提交任务 (In Review)
    RUNNING --> ESCALATED : 严重失败 / 权限超限 (Escalated)

    state IN_REVIEW {
        [*] --> Reviewing : 审阅中
        Reviewing --> Accepted : 管理者批准交付物
        Reviewing --> Rejected : 未达输出契约要求
    }

    IN_REVIEW --> COMPLETED : 已接受 (Completed)
    IN_REVIEW --> RUNNING : 重新分发至执行角色
    ESCALATED --> RUNNING : 由人类所有者/管理者解决
    COMPLETED --> [*]

---

## 5.1. 暗影模式与去中心化人类节点架构 (Shadow Mode & Human Nodes)

OpenOPC 支持 **暗影模式 (Shadow Mode)**，允许将下属角色交由真实人类外包人员完成，作为 CompanyRuntime DAG 中的去中心化计算节点。

```mermaid
sequenceDiagram
    autonumber
    participant Engine as CompanyRuntime (Layer 2)
    participant Adapter as HumanAgentAdapter (Layer 3)
    participant Bus as MessageBus (Layer 0)
    participant Store as OPCStore / SQLite (Database)
    participant Portal as Streamlit Web 门户 (Presentation)
    participant Human as 人类承包商 (Human Node)

    Engine->>Adapter: execute(task, context)
    Adapter->>Store: save_task(status="RUNNING", sub_state="AWAITING_HUMAN_DELIVERABLE")
    Adapter->>Bus: subscribe_once("deliverable_completed:{task_id}")
    Note over Adapter: 适配器原生阻塞 (零轮询)

    Human->>Portal: 身份验证 (JWT / PBKDF2)
    Portal->>Human: 渲染分配的工作项
    Human->>Portal: 上传交付文件 / 总结
    Portal->>Store: save_trajectory() & log_org_changelog()
    Portal->>Bus: publish_event("deliverable_completed:{task_id}", payload)
    Bus-->>Adapter: 事件触发 (解除 subscribe_once 阻塞)
    Adapter-->>Engine: 返回任务完成 Payload
```

---

## 5.2. 时空组织知识与性能追踪器 (Temporal Knowledge & Performance Tracker)

**时空组织知识与性能追踪器** 将 OpenOPC 升级为具有时间感知的组织变更日志与性能分析平台：

1. **时间感知向量元数据 (`opc.layer5_memory.memory_manager`)**:
   - 在向量文档嵌入中严格注入 `timestamp`, `year_month`, `epoch_week`, 与 `epoch_time`。
   - 提供 `query_temporal_memory(query, start_date, end_date)` 用于时间跨度历史对比。

2. **统一组织变更日志表 (`ORG_CHANGELOGS`)**:
   - SQLite Schema: `org_changelogs(id, timestamp, event_type, actor_id, description, impact_score, metadata)`.
   - 由 `OPCLogger` 在 DAG 关键节点静默自动写入。

3. **三级关系性能聚合 (`opc.database.store`)**:
   - 按可配置时间间隔 (`weekly` 或 `daily`) 执行 SQL `GROUP BY` 聚合。
   - 在三个层级切片度量指标：`Global` (全组织), `Team` (团队/角色), `Individual` (个人/承包商/AI员工)。

4. **时光机分析仪表盘 (`opc.presentation.human_portal`)**:
   - Streamlit 分析门户，可视化渲染组织速率折线图、团队切片柱状图、个人绩效图表与可搜索变更日志流。
```

### 协作模式 (Collaboration Modes)

管理者角色在以下 5 种不同的模式下协调与分发工作：
- **`execute` (执行)**: 由单个 Agent 直接执行单步任务。
- **`delegate` (委托)**: 拆解并将工作项委派给下属角色 Agent。
- **`review` (审阅)**: 对提交的工作项产出按验收标准进行评估。
- **`integrate` (集成)**: 将各子任务的产出汇总为完整交付物。
- **`rework` (重作)**: 拒绝后附带具体修改建议并重新分发。

---

## 6. Layer 3: Agent 执行与外部代理适配器 (Broker Adapters)

 Layer 3 (`opc/layer3_agent/`) 将任务执行与特定 LLM 厂商或 CLI 工具解耦。任务既可以通过 OpenOPC 的 `NativeAgent` / `NativeRuntimeV2` 原生引擎执行，也可以通过 `ExternalAgentBroker` 委托给外部编程 Agent。

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

## 7. Layer 4: 工具执行引擎与治理管道 (Tool Governance Pipeline)

Layer 4 (`opc/layer4_tools/`) 提供实际的可执行能力。每一次工具调用在进入处理函数之前，必须通过多重安全治理管道。

```mermaid
flowchart LR
    subgraph ToolCallEmitter ["Agent 执行层"]
        AgentCall["Agent 发起工具调用请求"]
    end

    subgraph SafetyGovernance ["安全治理管道"]
        PermMgr["权限管理器 (permissions.py)"]
        Allowlist["审批允许列表 (approval_allowlist.py)"]
        ShellCheck["Shell 安全检查器 (shell_safety.py)"]
        GateHarness["Gate 检查管道 (gate_harness.py)"]
    end

    subgraph ToolHandlers ["Layer 4 工具处理函数"]
        Browser["Playwright 浏览器 (browser.py)"]
        ShellExec["Shell / Python 执行 (shell.py, python_exec.py)"]
        FileOps["文件与 Git 操作 (file_ops.py, git_ops.py)"]
        CollabRPC["协作 RPC (collaboration_rpc.py)"]
        SearchTodo["网络搜索与待办事项 (web_search.py, todo.py)"]
    end

    subgraph Sanitization ["输出预算与结果截断"]
        OutputBudget["输出预算管控 (output_budget.py)"]
    end

    AgentCall --> PermMgr
    PermMgr --> Allowlist
    Allowlist -->|匹配已授权 Grant| ToolHandlers
    Allowlist -->|未匹配/高风险| ShellCheck
    ShellCheck -->|安全命令| GateHarness
    ShellCheck -->|不安全/被拦截| Escalation["升级至人类 / 拒绝执行"]
    GateHarness --> ToolHandlers

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

    OutputBudget --> Result["返回截断与脱敏后的工具结果"]
```

---

## 8. Layer 5: 记忆进化与剧本提升机制 (Memory Evolution & Playbook Promotion)

Layer 5 (`opc/layer5_memory/`) 驱动 **自成长 (Self-Grown)** 闭环，将原始执行轨迹转化为角色私有知识与全公司共享剧本。

```mermaid
flowchart TB
    subgraph TrajectoryCapture ["执行轨迹捕获"]
        RawTrace["原始执行轨迹 (store.py)"]
        UserFeedback["用户评价反馈与打分"]
    end

    subgraph EvolutionEngine ["员工进化引擎 (employee_evolution.py)"]
        Compactor["历史压缩器 (history_compactor.py)"]
        Attributor["归因分析器 (Credit & Blame Attribution)"]
        ExperienceDistiller["角色经验蒸馏器"]
        PlaybookPromoter["公司剧本提升引擎"]
    end

    subgraph MemoryStorage ["持久化记忆存储"]
        PrivateMem["员工私有经验档案 (.json)"]
        SharedPlaybooks["公司共享剧本库 (skill_library.py)"]
        VectorDB["ChromaDB 向量嵌入 (memory_manager.py)"]
    end

    RawTrace --> Compactor
    UserFeedback --> Attributor
    Compactor --> Attributor
    Attributor --> ExperienceDistiller
    ExperienceDistiller --> PrivateMem

    ExperienceDistiller --> PlaybookPromoter
    PlaybookPromoter -->|高频高信号模式| SharedPlaybooks
    
    PrivateMem --> VectorDB
    SharedPlaybooks --> VectorDB
```

---

## 9. 多渠道与展示层拓扑 (Multi-Channel Topology)

OpenOPC 支持终端交互、实时 Web UI 以及多渠道消息平台接入。

```mermaid
flowchart LR
    subgraph Clients ["用户界面与客户端"]
        WebBrowser["浏览器 Office UI (React + Phaser 3)"]
        TerminalCLI["交互式 CLI (opc chat / opc exec)"]
        TUIBoard["终端看板 (opc.plugins.cli_board)"]
    end

    subgraph MultiChannel ["消息网关渠道"]
        TG["Telegram"]
        DC["Discord"]
        FS["飞书 (Feishu)"]
        SL["Slack"]
        DD["钉钉 (DingTalk)"]
        QQ["QQ 机器人"]
        MX["Matrix"]
        MC["Mochat"]
        WA["WhatsApp"]
        EM["邮件 (Email)"]
    end

    subgraph ServerBackend ["OpenOPC 插件与网关服务"]
        ChannelManager["渠道管理器 (opc.channels.manager)"]
        OfficeUIServer["Office UI 服务 (FastAPI + WebSockets)"]
        WSHandler["WebSocket 事件处理器 (ws_handler.py)"]
        SnapshotBuilder["组织快照构建器 (snapshot_builder.py)"]
    end

    subgraph EngineCore ["OpenOPC 核心引擎"]
        MessageBus["Layer 0 消息总线"]
        CompanyMode["Layer 2 公司模式"]
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

## 10. 数据模型与持久化架构 (Persistence Architecture)

OpenOPC 使用异步 SQLite (`aiosqlite`) 存储关系型数据，并结合 ChromaDB 向量数据库存储角色记忆。

```mermaid
erDiagram
    PROJECTS ||--o{ WORK_ITEMS : contains
    PROJECTS ||--o{ ROLES : defines
    ROLES ||--o{ EMPLOYEES : assigned_to
    WORK_ITEMS ||--o{ ARTIFACTS : produces
    WORK_ITEMS ||--o{ TRAJECTORIES : records
    EMPLOYEES ||--o{ TRAJECTORIES : generates
    SESSIONS ||--o{ GRANTS : scoped_by

    PROJECTS {
        string id PK
        string name
        string goal
        string status
        datetime created_at
    }

    WORK_ITEMS {
        string id PK
        string project_id FK
        string title
        string state
        string owner_role FK
        string parent_id FK
        json dependency_ids
    }

    ROLES {
        string id PK
        string project_id FK
        string title
        string responsibility
        json capabilities
    }

    EMPLOYEES {
        string id PK
        string name
        string role_id FK
        string profile_avatar
        json private_experience
    }

    TRAJECTORIES {
        string id PK
        string work_item_id FK
        string employee_id FK
        json step_history
        float outcome_score
    }

    ARTIFACTS {
        string id PK
        string work_item_id FK
        string file_path
        string mime_type
    }

    SESSIONS {
        string id PK
        string mode
        string project_id FK
    }

    GRANTS {
        string id PK
        string session_id FK
        string tool_name
        string scope
    }
```

---

## 11. 模块与目录对照表 (Module & Directory Map)

| 目录 / 包名 | 图层 / 作用域 | 主要职责 | 关键文件 |
| :--- | :--- | :--- | :--- |
| [`opc/core/`](../opc/core) | 核心层 | 数据模型、配置 Schema、活跃任务追踪与运行时封装。 | [`config.py`](../opc/core/config.py), [`models.py`](../opc/core/models.py), [`employee_registry.py`](../opc/core/employee_registry.py) |
| [`opc/layer0_interaction/`](../opc/layer0_interaction) | Layer 0 | 系统事件消息总线与解耦交互。 | [`message_bus.py`](../opc/layer0_interaction/message_bus.py) |
| [`opc/layer1_perception/`](../opc/layer1_perception) | Layer 1 | 上下文组装、文档加载与任务意图路由。 | [`context_assembler.py`](../opc/layer1_perception/context_assembler.py), [`context_loader.py`](../opc/layer1_perception/context_loader.py) |
| [`opc/layer2_organization/`](../opc/layer2_organization) | Layer 2 | 公司模式编排、组织引擎、招聘、工作项 DAG 状态机及治理策略。 | [`company_mode.py`](../opc/layer2_organization/company_mode.py), [`company_runtime.py`](../opc/layer2_organization/company_runtime.py), [`recruiter.py`](../opc/layer2_organization/recruiter.py), [`approval.py`](../opc/layer2_organization/approval.py) |
| [`opc/layer3_agent/`](../opc/layer3_agent) | Layer 3 | 原生执行引擎、Runtime V2、外部 Agent 代理、适配器 (Codex, Claude, Cursor, OpenCode) 及 Prompt 构建器。 | [`native_agent.py`](../opc/layer3_agent/native_agent.py), [`external_broker.py`](../opc/layer3_agent/external_broker.py), [`adapters/`](../opc/layer3_agent/adapters) |
| [`opc/layer4_tools/`](../opc/layer4_tools) | Layer 4 | 可执行能力集（包括 Playwright 浏览器、Shell 执行、文件操作、Web 搜索、待办事项与协作 RPC）。 | [`browser.py`](../opc/layer4_tools/browser.py), [`shell.py`](../opc/layer4_tools/shell.py), [`collaboration.py`](../opc/layer4_tools/collaboration.py) |
| [`opc/layer5_memory/`](../opc/layer5_memory) | Layer 5 | 向量/Markdown 记忆、轨迹蒸馏、结果归因、角色私有经验与共享剧本。 | [`memory_manager.py`](../opc/layer5_memory/memory_manager.py), [`employee_evolution.py`](../opc/layer5_memory/employee_evolution.py), [`skill_library.py`](../opc/layer5_memory/skill_library.py) |
| [`opc/layer6_observability/`](../opc/layer6_observability) | Layer 6 | Token 成本监控与结构化日志引擎。 | [`cost_tracker.py`](../opc/layer6_observability/cost_tracker.py), [`opc_logger.py`](../opc/layer6_observability/opc_logger.py) |
| [`opc/cli/`](../opc/cli) | 接口层 | Typer CLI 命令行套件 (`opc init`, `opc chat`, `opc ui`, `opc exec`)。 | [`app.py`](../opc/cli/app.py) |
| [`opc/channels/`](../opc/channels) | 网关层 | 多渠道通信适配器（Telegram, Discord, 飞书, Slack, 钉钉, Matrix 等）。 | [`manager.py`](../opc/channels/manager.py), [`base.py`](../opc/channels/base.py) |
| [`opc/database/`](../opc/database) | 持久化 | 异步 SQLite 关系型存储层。 | [`store.py`](../opc/database/store.py) |
| [`opc/llm/`](../opc/llm) | LLM 层 | 统一 LiteLLM 集成层，支持多 Provider 重试与降级机制。 | [`provider.py`](../opc/llm/provider.py), [`retry.py`](../opc/llm/retry.py) |
| [`opc/market/`](../opc/market) | 市场层 | 人才预设、公司架构预设、包导入/导出及沙箱校验。 | [`architecture_registry.py`](../opc/market/architecture_registry.py), [`talent_presets.py`](../opc/market/talent_presets.py) |
| [`opc/plugins/office_ui/`](../opc/plugins/office_ui) | 接口层 | FastAPI 后端服务、WebSocket 事件引擎、Phaser 3 Office 界面与 React 工作区。 | [`server.py`](../opc/plugins/office_ui/server.py), [`ws_handler.py`](../opc/plugins/office_ui/ws_handler.py), [`snapshot_builder.py`](../opc/plugins/office_ui/snapshot_builder.py) |

---

## 12. 开源贡献指南 (Summary for Contributors)

向 OpenOPC 贡献新功能时的指导步骤：
1. **添加新工具**: 在 [`opc/layer4_tools/registry.py`](../opc/layer4_tools/registry.py) 中注册工具定义，并在 [`opc/layer4_tools/`](../opc/layer4_tools) 中实现处理函数。
2. **添加外部 Agent 适配器**: 在 [`opc/layer3_agent/adapters/base.py`](../opc/layer3_agent/adapters/base.py) 中继承 `BaseAgentAdapter`，并在 [`opc/layer3_agent/adapters/registry.py`](../opc/layer3_agent/adapters/registry.py) 中注册。
3. **添加渠道 Provider**: 在 [`opc/channels/provider_base.py`](../opc/channels/provider_base.py) 中继承 `BaseChannelProvider`，并在 [`opc/channels/provider_registry.py`](../opc/channels/provider_registry.py) 中注册。
4. **修改治理 / 审批逻辑**: 在 [`opc/layer2_organization/approval.py`](../opc/layer2_organization/approval.py) 中更新策略逻辑。
