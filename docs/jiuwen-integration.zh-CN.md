# Jiuwen / JiuwenSwarm 接入

OpenOPC 提供两个独立的外部执行 Agent：

- `jiuwen`：普通单 Agent，行为与 Codex、Claude Code 相同；一个 OpenOPC Task/WorkItem 对应一个外部执行单元。
- `jiuwenswarm`：不透明 Team；Jiuwen 内部的成员、任务与消息不会映射成 OpenOPC 角色、子任务、Session 或 Kanban 卡片。

二者均支持 Task Mode 与 Company Mode。推荐使用 Gateway 传输，因为它支持流式事件、会话恢复、用户问答/审批以及中断；`transport: cli` 是无交互 JSONL 回退方式。

## API 配置

Jiuwen 使用 `~/.jiuwenswarm/config/.env`，不会自动读取 OpenOPC 的 `.opc/config/llm_config.yaml`。可以复用同一个 API key 和 endpoint，但需要使用 Jiuwen/提供方支持的 Agent 模型名。例如火山方舟 Agent Plan 的配置为：

```dotenv
API_BASE="https://ark.cn-beijing.volces.com/api/plan/v3"
API_KEY="<取自 .opc/config/llm_config.yaml 的 api_key>"
MODEL_NAME="ark-code-latest"
MODEL_PROVIDER="OpenAI"
```

`openai/minimax-m3` 等普通补全模型名即使能被 OpenOPC 直接调用，也可能不支持 Agent Plan 请求；在上述 endpoint 下应使用 Plan 路由名 `ark-code-latest`。配置后运行：

```bash
chmod 600 ~/.jiuwenswarm/config/.env
jiuwenswarm-start --restart default
```

## 安装与检查

按照 JiuwenSwarm 官方说明完成模型与运行时配置，然后启动其 Gateway：

```bash
jiuwenswarm-start
```

为 OpenOPC 安装 Gateway 客户端依赖并检查连接：

```bash
pip install -e '.[jiuwen]'
opc agents list
opc agents preflight jiuwen
opc agents preflight jiuwenswarm
```

默认 Gateway 地址为 `ws://127.0.0.1:19001/tui`。可以在 `.opc/config/agent_config.yaml` 的 `gateway_url` 中修改，或设置 `JIUWENSWARM_GATEWAY_URL`。

## Task Mode

```bash
opc exec --mode task --agent jiuwen "分析并修复这个问题"
opc exec --mode task --agent jiuwenswarm "由一个 Team 完成这个交付"
```

Office UI 的 Agent 选择器和交互 CLI 的 `/agent` 同样提供这两个选项。

## Company Mode：普通 Agent

`jiuwen` 可以像其他外部 Agent 一样作为公司角色的执行器。全局选择或角色配置只改变执行器，不改变组织的 WorkItem 拓扑：

```bash
opc exec --mode company --agent jiuwen "完成公司任务"
```

在 Office UI 的 **Company → Org → 角色 → Runtime** 中选择“仅当前角色”，再把执行 Agent 设为 `Jiuwen`，表示只接管该角色收到的 WorkItem；其他角色仍按原来的招聘与执行策略运行。`JiuwenSwarm Team` 不出现在“仅当前角色”选项里，以免把普通角色执行器与 Team 子树边界混淆。

## Company Mode：Team 子树绑定

把一个组织边界及其下级角色折叠为一个 JiuwenSwarm Team：

```bash
opc org team-bind \
  --org corporate \
  --role cto \
  --agent jiuwenswarm \
  --scope subtree \
  --provider-mode team \
  --session-scope company_run \
  --max-inflight 1 \
  --failure-policy fail_closed \
  --artifact-isolation validated_workspace
```

Office UI 也可以直接完成绑定：在 **Company → Org** 中选中 CTO、CMO 等边界角色，展开 **Runtime**，选择“当前角色及所有下级作为一个 JiuwenSwarm Team”。

也可以在 Company 会话开始时的 Staffing 或 Recruitment Review 卡片中，直接把某个角色的 Execution Agent 选为 `JiuwenSwarm Team`。此时该角色就是 Team 边界，其所有下级会立即显示为由此 Team 覆盖并禁用员工/Agent 选择。全局创建 Company 会话时选择 `JiuwenSwarm Team`，会预选所有直接向 owner 汇报的顶级角色为独立边界，不会为每个下级嵌套创建 Team；默认 corporate 架构中这会选中 CEO 并覆盖 CEO 子树。

绑定后：

- 组织图仍保留 CTO 及其下级角色，用于职责、路由和可视化；执行图把该子树解析为唯一的 canonical Team seat。
- 上级仍把 WorkItem 派给组织角色 ID（例如 `cto`），不需要也不允许知道 Jiuwen 内部成员。每个派给该边界的 OpenOPC WorkItem 由同一个不透明 Team 执行。
- 边界和被覆盖角色不会进入 OpenOPC 招聘；Jiuwen 自行安排 leader 与成员。Auto Recruit 和 Approve 都在后端重新编译 Team 覆盖范围并过滤这些角色，因此不会注入招聘 prompt、不会创建员工分配；即使旧页面提交了已覆盖下级的选择也会被忽略。
- 被覆盖的下级角色不能再单独选择执行 Agent，避免一个组织角色同时被两套执行单元接管。
- 未选择 Team 子树绑定时，不会生成 Team 执行单元或能力目录，所有角色继续按普通 Company Mode 机制招聘和运行。

上级看到的 Team 能力并非写死。OpenOPC 每次编译运行图时，会从当前组织中动态汇总被覆盖角色的职责、`capabilities`、`skill_refs`、工具和 artifact contract；绑定 `metadata` 中的 `capabilities`、`deliverables` 与 `out_of_scope` 仅作为显式补充。生成的能力清单包含稳定哈希，并进入上级的 delegation contract。上级仍按 `cto` 等角色路由，可在 `delegate_work.required_capabilities` 中附加机器可读能力标签；匹配结果和清单哈希会保存到 WorkItem。未声明标签会产生可审计警告，但不会因为职责文本未被标签化而误拒绝派单。

```bash
opc org team-bindings --json
opc org team-unbind --org corporate --role cto
```

CMO、COO 或自定义组织角色使用相同方式。重叠子树绑定会被拒绝，以避免同一个角色同时归属两个外部 Team。
删除 Team 边界角色会同步移除对应绑定；会造成 Team 子树重叠的组织层级修改会被拒绝。解绑后相关角色恢复普通招聘和执行选择。

## 运行契约与安全

- Gateway Team 模式以 `chat.processing_status(is_processing=false)` 或错误事件作为外层终止信号；内部 `chat.final` 和成员完成事件仅作为进度。
- Team 必须返回包含 `work_item_id`、`attempt_id`、`status`、`summary`、`deliverables`、`verification`、`risks`、`open_questions`、`handoff` 的 JSON 对象；ID 不匹配或字段不完整时失败关闭。
- Company Mode 会把 `project_dir` 与 `trusted_dirs` 强制收紧到当前工作区，并在执行前后生成文件证明；越界符号链接会被拒绝。
- 默认 `failure_policy` 为 `fail_closed`。若绑定显式设置为 `fallback_native`，相同的折叠边界会在外部执行失败后交给 OpenOPC Native 执行。
- `max_inflight` 以项目、组织和 binding 为作用域限制同时运行的 Team 外部进程；默认值为 1。
- `session_scope: company_run` 在同一公司运行/角色会话内复用 Jiuwen 会话；`work_item` 将恢复范围限制在该 WorkItem。

`validated_workspace` 是“Jiuwen trusted-dir 收紧 + OpenOPC 执行前后文件证明”，不是操作系统级进程沙箱。对于不受信任的模型、工具或代码，仍应把 OpenOPC 工作区放在容器、虚拟机或专用低权限账号中运行。

CLI 回退配置示例：

```yaml
external_agents:
  jiuwen:
    command: jiuwenswarm
    transport: cli
    provider_mode: code.normal
  jiuwenswarm:
    command: jiuwenswarm
    transport: cli
    provider_mode: team
```

CLI 回退没有 Gateway 的交互问答桥接；需要审批或问题回答的工作建议使用 Gateway。
