# OPC Chat Slash Commands

`opc chat` includes an interactive slash console for managing context, sessions, runtime visibility, company work items, organization assets, and diagnostics without opening the Office UI.

The source of truth for this table is `_SLASH_COMMANDS` in `opc/cli/app.py`.

## Chat And Context

- `/help` shows the command table.
- `/quit` exits interactive chat.
- `/queue list|drop|clear` inspects or edits queued prompts while a turn is running.
- `/status` shows project, session, mode, agent, domains, model, cost, and external agent availability.
- `/mode [task|company] [corporate|custom]` changes how future natural-language messages run.
- `/agent [native|codex|jiuwen|jiuwenswarm|claude_code|cursor|opencode|none]` sets or clears the preferred execution agent.
- `/domains [domain ...|clear]` sets or clears domain hints.
- Aliases: `/p` is `/project`, `/s` is `/session`, `/t` is `/task`, and `/checkpoint` is `/checkpoints`.

## Project And Session

- `/project` shows current project info and project command usage.
- `/project list|create|switch|rename|delete` manages project ids through the shared Office service.
- `/stop [task_id|session_id]` stops the current or selected runtime and preserves history.
- `/continue [task_id|session_id] [message]` continues a stopped runtime without re-planning.
- `/session` shows current session info and session command usage.
- `/session list|new|create|resume|show|config|send|rename|delete|stop|continue|complete` manages task-backed and plain chat sessions.
- `/session create [title] [--mode task|company|org] [--agent ...]` creates a task-backed session. `org` is a compatibility selector for Company Mode with a saved org architecture.

## Tasks And Runtime

- `/tasks [status] [--limit N] [--full]` lists project tasks.
- `/task show|move|done|rename|delete` inspects and updates persisted tasks.
- `/runtime [--limit N] [--full]` shows live runtime, active tasks, external sessions, and checkpoints.
- `/logs <task_id|session_id> [--limit N] [--full]` shows execution logs, runtime events, tools, and transcript.
- `/comms <task_id> [--limit N] [--full]` shows company-mode messages, handoffs, review notes, and handoff context.
- `/attachments [--limit N] [--full]` lists current-session attachment references.
- `/checkpoints [--limit N] [--full]` lists pending execution checkpoints.

## Company Work And Board

- `/staffing [context]` opens the pending company staffing editor or role context preview.
- `/kanban [once|stop|all]` shows current-session work-item status inline and can watch live updates.
- `/board kanban|pipeline|work-item|role|logs` opens the CLI board inspector in read-only mode.
- `/work-items list|show|logs|role-status` inspects company work items and role progress.

## Organization, Talent, And Market

- `/org` shows or edits organization config.
- `/org role add|update|delete|bulk-add` manages roles.
- `/org policy update --payload ...` updates runtime policy.
- `/org strategy update --final-decider <role>` updates organization strategy.
- `/org saved list|save|load|delete` manages saved organization architectures.
- `/agent list|detail|create|delete|move|import-employee` manages visual office agents.
- `/talent list|employees|scan|import|import-repo|hire|employee|import-agent` manages talent templates and hired employees.
- `/market browse|preview|list|presets|apply-preset|install|uninstall|export` manages architecture presets and `.opcpkg` packages.
- `/reorg list|show|approve|deny|apply` manages reorganization proposals.

## Diagnostics And Display

- `/cost` shows token and cost counters.
- `--limit N` caps list output between 1 and 100 rows.
- `--full` expands long text that is otherwise shortened for compact terminal display.
- Destructive commands require `--yes`.
