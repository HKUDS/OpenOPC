# Company Mode Metadata Ownership

Company Mode keeps two different records with different responsibilities:

- `DelegationWorkItem` owns business, collaboration, board, review, report, and user-visible progress facts.
- Runtime `Task` owns execution envelopes, sessions, external-agent state, locks, runtime audit, stdout/result records, and replay.

The executable owner matrix lives in `opc/layer2_organization/metadata_ownership.py`. Treat this document as the human-readable summary; the code is the contract used by tests and runtime diagnostics.

## Owner Groups

| Owner | Examples | Rule |
|---|---|---|
| `work_item` | `work_kind`, `current_turn_mode`, dependency/waiting ids, handoff/context preview, prompt contracts, `progress_log`, role and employee context, review/report metadata, verification fields, delivery package, self-evolution fields | Scheduling, board projection, collaboration, review/report, and user-visible progress reads should use `DelegationWorkItem.metadata`. |
| `runtime_task` | `runtime_v2`, `runtime_verification*`, `member_session_state`, `external_resume_*`, `working_memory`, `interrupted_recovery`, `last_stop_reason`, `peer_wait`, comms reactivation audit, `runtime_control_state`, runtime session team/seat ids | Execution infrastructure should store these only on runtime `Task.metadata`. They must not become WorkItem business facts. |
| `execution_copy` | `mode`, `execution_mode`, `runtime_model`, `company_profile`, organization/runtime topology, delegation ids, seat/role routing, execution-agent selection, workspace/comms roots, parent session id | Runtime `Task` may carry these as immutable routing and UI envelope copies. They are not the authoritative business facts. |

WorkItem-to-runtime-Task linkage is not metadata. It is owned by the `work_item_runtime_links` table. UI payloads may expose this relation as `runtime_task_id` or `execution_turn_id`.

## Migration And Diagnostics

- New Company Mode writes WorkItem-owned fields to `DelegationWorkItem.metadata`.
- Runtime Task metadata is stripped of WorkItem-owned fields that are not allowed execution copies.
- `migrate_work_item_owned_metadata_from_linked_tasks` can dry-run or backfill missing WorkItem-owned values from linked legacy Tasks.
- Conflicts are not silently repaired. WorkItem wins, and diagnostics report `metadata_ownership_conflict`.
- Runtime invariants are validated by `opc/layer2_organization/work_item_runtime_invariants.py` and related tests.
