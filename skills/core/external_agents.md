---
name: external_agents
description: "External agent capability profiles and delegation guidance"
domain:
  - general
  - coding
  - frontend
  - backend
  - devops
  - writing
  - documentation
  - automation
always_on: true
trigger: "When deciding whether to use the native agent or an external CLI agent"
---

# External Agent Selection Skill

## Goal
Choose between the OPC native agent and available external agents based on the actual task,
the configured agent profile, and expected execution style. Do not rely on fixed domain
rules alone.

## Native Agent
- Strong default choice for lightweight reasoning, conversation, clarification, and tasks
  that benefit from tight integration with OPC memory, organization, and tool orchestration.
- Prefer native when direct continuity inside OPC matters more than delegating to an
  external CLI agent.

## External CLI Agents
- External agents such as Cursor, Claude Code, Codex, Jiuwen, and JiuwenSwarm are usually strongest for complex,
  tool-heavy, multi-step execution where a dedicated CLI agent can work in an isolated
  workspace.
- Treat `jiuwen` as a normal single external execution unit. Treat `jiuwenswarm` as one
  opaque Team: its internal teammates never become OpenOPC Tasks, roles, sessions, or cards.
- Their strengths are not limited to writing code. They can also handle bash-driven tasks
  such as generating or transforming Markdown, documents, PDFs, slide content, reports,
  scripts, and repository-wide edits.
- When choosing among external agents, consider the configured model, whether the run should
  start a new session or continue an existing one, and any extra CLI arguments already set.

## Decision Heuristics
- Prefer an external agent when the task requires sustained autonomous execution over files,
  shell commands, or project artifacts.
- Prefer an external agent when the request is complex enough that a specialized coding or
  CLI workflow is likely to outperform the native agent.
- Prefer native when the task is simple, mostly conversational, or better served by keeping
  reasoning and tool use inside OPC itself.
- If multiple external agents are available, pick the one whose configured profile best
  matches the task instead of following a fixed ranking.

## Approval And Autonomy
- Treat external agents as part of the same bounded-autonomy system as native tools.
- Routine, low-risk actions can be auto-approved when they match learned user preferences.
- Ambiguous, sensitive, or destructive operations should trigger escalation to the user.
- Learn from explicit user approvals or rejections so future runs behave more like the
  user's trusted second self rather than a static automation pipeline.
