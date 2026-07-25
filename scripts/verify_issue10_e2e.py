"""Real-scenario verification for issue #10 (CommsReactivationSweeper 无限死循环).

Reproduces the reporter's timeline against the REAL engine, REAL sqlite store,
REAL startup reconcile, REAL dispatcher and REAL resume paths in a sandboxed
OPC_HOME (config copied from the project's .opc, external agents all disabled
exactly like the reporter's environment):

Phase A  seed the killed-process state (intake work item phase=running,
         runtime task pinned to codex, durable claim left behind, NO
         checkpoint — i.e. the process died without graceful shutdown).
Phase B  fresh engine boot → real startup reconcile must convert the state
         into a pending company_runtime_interrupted checkpoint + holds
         (no auto-resume).
Phase C  real resume ("重跑" plain message, then Continue-button force
         resume) → the availability gate must fail the codex-pinned item
         CLOSED (FAILED + blocked_reason), the run must converge, and no
         dispatch attempt may be burned (attempt_seq stays 0).
Phase D  another fresh boot (restart) → no resume storm, benign follow-up
         message doesn't wake anything, and 20s of idle engine time uses
         (near-)zero CPU — the reporter saw 67-95% pinned.
Phase E  the generic loop brake: repeated kill-mid-flight cycles (boot →
         force resume → wait for durable claim → cancel → drop engine) on a
         native-pinned item whose LLM endpoint blackholes. Pre-fix this
         replays forever; post-fix the attempt ledger must terminalize the
         card within the streak limits.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SCRATCH = Path(__file__).resolve().parent
REPO = Path("/data2/bjdwhzzh/project-hku/OpenOPC")
SANDBOX_ROOT = SCRATCH / "issue10_sandbox"

sys.path.insert(0, str(REPO))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def make_home(name: str, *, blackhole_llm: bool = False) -> Path:
    home = SANDBOX_ROOT / name
    if home.exists():
        shutil.rmtree(home)
    (home / "config").mkdir(parents=True)
    src = REPO / ".opc" / "config"
    for item in src.iterdir():
        if item.is_dir():
            shutil.copytree(item, home / "config" / item.name)
        else:
            shutil.copy2(item, home / "config" / item.name)
    # Reporter's environment: ALL external agents disabled in config.
    import yaml

    agent_cfg_path = home / "config" / "agent_config.yaml"
    agent_cfg = yaml.safe_load(agent_cfg_path.read_text()) or {}
    external_agents = agent_cfg.get("external_agents") or {}
    for key, value in list(external_agents.items()):
        if isinstance(value, dict):
            value["enabled"] = False
    agent_cfg_path.write_text(yaml.safe_dump(agent_cfg, allow_unicode=True))
    if blackhole_llm:
        llm_cfg_path = home / "config" / "llm_config.yaml"
        llm_cfg = yaml.safe_load(llm_cfg_path.read_text()) or {}
        llm = llm_cfg.setdefault("llm", {})
        # Non-routable address: real connect attempt hangs → wide cancel
        # window, zero API cost. Trim retry/timeout knobs when present.
        llm["base_url"] = "http://10.255.255.1:9/v1"
        for knob in ("timeout", "request_timeout", "connect_timeout"):
            if knob in llm:
                llm[knob] = 8
        llm_cfg_path.write_text(yaml.safe_dump(llm, allow_unicode=True))
    return home


async def seed_killed_state(home: Path, *, pinned_agent: str) -> None:
    """Write the exact post-kill DB state from the issue via real store APIs."""
    from opc.core.models import (
        DelegationRoleSession,
        DelegationWorkItem,
        Phase,
        Task,
        TaskStatus,
    )
    from opc.database.store import OPCStore
    from opc.layer2_organization.company_mode import (
        serialize_company_work_item_runtime_plan,
    )
    from opc.layer2_organization.org_work_item_planner import (
        CompanyWorkItemRuntimePlan,
        WorkItemProjectionSpec,
    )
    from opc.layer2_organization.work_item_links import set_linked_work_item_id

    proj_dir = home / "projects" / "default"
    proj_dir.mkdir(parents=True, exist_ok=True)
    store = OPCStore(proj_dir / "tasks.db")
    await store.initialize()
    plan = CompanyWorkItemRuntimePlan(
        profile="corporate",
        projections=[
            WorkItemProjectionSpec(
                projection_id="market-analyst-intake",
                turn_type="intake",
                title="市场分析师 Intake",
                summary="Receive the user request and dispatch.",
                role_id="market_analyst",
            )
        ],
        metadata={
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "final_decider_role_id": "market_analyst",
            "top_level_role_ids": ["market_analyst"],
        },
    )
    await store.save_task(
        Task(
            id="ui-anchor-sess-parent",
            title="Company chat",
            session_id="sess-parent",
            project_id="default",
            status=TaskStatus.RUNNING,  # anchor left RUNNING by the kill
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
    )
    await store.save_delegation_role_session(
        DelegationRoleSession(
            role_session_id="role-runtime-1",
            run_id="run-1",
            project_id="default",
            role_id="market_analyst",
            seat_id="seat-1",
            status="running",  # killed mid-flight
        )
    )
    await store.save_delegation_work_item(
        DelegationWorkItem(
            work_item_id="wi-intake-1",
            run_id="run-1",
            role_id="market_analyst",
            seat_id="seat-1",
            title="市场分析师 Intake",
            summary="Receive the user request and dispatch.",
            kind="intake",
            projection_id="market-analyst-intake",
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id="role-runtime-1",
            claimed_by_seat_id="seat-1",
            metadata={
                "work_item_projection_id": "market-analyst-intake",
                "claimed_by_role_session_id": "role-runtime-1",
                "claimed_task_id": "task-intake-1",
            },
        )
    )
    task = Task(
        id="task-intake-1",
        title="市场分析师 Intake",
        session_id="sess-child",
        parent_session_id="sess-parent",
        status=TaskStatus.RUNNING,
        project_id="default",
        assigned_to="market_analyst",
        assigned_external_agent=(None if pinned_agent == "native" else pinned_agent),
        execution_lock=True,
        metadata={
            "company_profile": "corporate",
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "work_item_runtime": True,
            "work_item_projection_id": "market-analyst-intake",
            "delegation_run_id": "run-1",
            "delegation_role_session_id": "role-runtime-1",
            "delegation_seat_id": "seat-1",
            "selected_execution_agent": pinned_agent,
            "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
            "progress_log": ["started", "working"],
            # Minimal runtime topology so runtime.bootstrap materializes the
            # market_analyst role session after restart (a real run persists
            # this on the root runtime task / delegation run).
            "runtime_topology": {
                "seats": [
                    {
                        "seat_id": "seat-1",
                        "role_id": "market_analyst",
                        "team_id": "team::market_analyst",
                        "team_instance_id": "team::market_analyst::1",
                        "employee_id": "market_analyst-emp-1",
                    }
                ]
            },
        },
    )
    set_linked_work_item_id(task, "wi-intake-1")
    await store.save_task(task)
    await store.link_work_item_runtime_task("wi-intake-1", "task-intake-1")
    await store.close()


async def boot_engine(home: Path):
    from opc.core.config import OPCConfig
    from opc.engine import OPCEngine

    os.environ["OPC_HOME"] = str(home)
    config = OPCConfig.load(home / "config")
    engine = OPCEngine(config=config, project_id="default")
    await engine.initialize()
    return engine


async def drop_engine(engine) -> None:
    """Simulate a process kill: stop only what a dead process cannot keep
    running (background tasks / fds). No graceful checkpointing."""
    try:
        if engine.comms_reactivation_sweeper is not None:
            await engine.comms_reactivation_sweeper.stop()
    except Exception:
        pass
    try:
        if engine.store is not None:
            await engine.store.close()
    except Exception:
        pass


async def get_item(home: Path, work_item_id: str):
    from opc.database.store import OPCStore

    store = OPCStore(home / "projects" / "default" / "tasks.db")
    await store.initialize()
    try:
        return await store.get_delegation_work_item(work_item_id)
    finally:
        await store.close()


async def phase_abcd() -> None:
    print("\n== Phase A: seed killed-process state (codex-pinned intake) ==")
    home = make_home("home_codex")
    await seed_killed_state(home, pinned_agent="codex")
    print("  seeded.")

    print("\n== Phase B: fresh boot → real startup reconcile ==")
    engine = await boot_engine(home)
    available = engine._available_external_agents()
    check("all external agents disabled (reporter env)", available == [], f"available={available}")
    checkpoints = await engine.store.get_pending_checkpoints(project_id="default")
    interrupted = [
        c for c in checkpoints if str(c.checkpoint_type) == "company_runtime_interrupted"
    ]
    check(
        "startup reconcile created interrupted checkpoint (no auto-resume)",
        len(interrupted) == 1,
        f"pending={[(c.checkpoint_type, c.status) for c in checkpoints]}",
    )
    item = await engine.store.get_delegation_work_item("wi-intake-1")
    check(
        "work item held, claim cleared, still phase=running",
        item is not None
        and str(item.metadata.get("dispatch_hold", "")) == "company_runtime_suspended"
        and not str(item.claimed_by_role_runtime_session_id or "").strip(),
        f"phase={item.phase.value if item else None} hold={item.metadata.get('dispatch_hold') if item else None}",
    )

    print("\n== Phase C: real resume — plain '重跑' then Continue force-resume ==")
    t0 = time.monotonic()
    try:
        response = await asyncio.wait_for(
            engine.process_message(
                "重跑",
                project_id="default",
                session_id="sess-parent",
                mode="company",
                company_profile="corporate",
            ),
            timeout=240,
        )
    except Exception as exc:  # noqa: BLE001
        response = f"<exception {type(exc).__name__}: {exc}>"
    elapsed1 = time.monotonic() - t0
    print(f"  '重跑' → {elapsed1:.1f}s\n  response: {str(response)[:300]}")

    item = await engine.store.get_delegation_work_item("wi-intake-1")
    if item is not None and item.phase.value != "failed":
        # Also exercise the Continue-button path if the decider path didn't
        # reach the gate (e.g. followup target routing declined).
        t0 = time.monotonic()
        try:
            response2 = await asyncio.wait_for(
                engine.process_message(
                    "continue",
                    project_id="default",
                    session_id="sess-parent",
                    mode="company",
                    company_profile="corporate",
                    message_metadata={"ui_force_resume": True},
                ),
                timeout=240,
            )
        except Exception as exc:  # noqa: BLE001
            response2 = f"<exception {type(exc).__name__}: {exc}>"
        print(f"  force resume → {time.monotonic() - t0:.1f}s\n  response: {str(response2)[:300]}")
        item = await engine.store.get_delegation_work_item("wi-intake-1")

    check(
        "codex-pinned item failed CLOSED with diagnostic",
        item is not None
        and item.phase.value == "failed"
        and "codex" in str(item.blocked_reason or ""),
        f"phase={item.phase.value if item else None} blocked_reason={str(item.blocked_reason or '')[:120]}",
    )
    check(
        "no dispatch attempt burned (attempt_seq==0: failed before any claim)",
        item is not None and int(dict(item.metadata or {}).get("attempt_seq", 0) or 0) == 0,
        f"attempt_seq={dict(item.metadata or {}).get('attempt_seq') if item else None}",
    )
    task_row = await engine.store.get_task("task-intake-1")
    check(
        "runtime task projected FAILED",
        task_row is not None and task_row.status.value == "failed",
        f"status={task_row.status.value if task_row else None}",
    )
    await drop_engine(engine)

    print("\n== Phase D: restart again → converged state stays converged ==")
    engine2 = await boot_engine(home)
    pending_after = await engine2.store.get_pending_checkpoints(project_id="default")
    company_pending = [
        c
        for c in pending_after
        if "company_runtime" in str(c.checkpoint_type)
    ]
    check(
        "no new company-runtime checkpoint for the terminal run",
        len(company_pending) == 0,
        f"pending={[(c.checkpoint_type, c.status) for c in pending_after]}",
    )
    try:
        response3 = await asyncio.wait_for(
            engine2.process_message(
                "现在怎么样了?",
                project_id="default",
                session_id="sess-parent",
                mode="company",
                company_profile="corporate",
            ),
            timeout=240,
        )
        print(f"  follow-up response: {str(response3)[:200]}")
        followup_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  follow-up raised: {type(exc).__name__}: {exc}")
        followup_ok = False
    item = await engine2.store.get_delegation_work_item("wi-intake-1")
    check(
        "follow-up message does not revive the failed item",
        followup_ok and item is not None and item.phase.value == "failed",
        f"phase={item.phase.value if item else None}",
    )

    # Idle CPU: reporter saw 67-95% pinned after restart. Sample this process
    # for 20s with the engine + sweeper alive and nothing to do.
    cpu0 = os.times()
    wall0 = time.monotonic()
    await asyncio.sleep(20)
    cpu1 = os.times()
    wall = time.monotonic() - wall0
    cpu_pct = ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) / wall * 100
    check("idle engine CPU < 15% of one core", cpu_pct < 15.0, f"cpu={cpu_pct:.1f}% over {wall:.0f}s")
    await drop_engine(engine2)


async def phase_e() -> None:
    print("\n== Phase E: ledger brake under repeated kill-mid-flight (native pin, blackhole LLM) ==")
    from opc.layer2_organization.phase import (
        ATTEMPT_CRASH_STREAK_LIMIT,
        ATTEMPT_INTERRUPTED_STREAK_LIMIT,
    )

    home = make_home("home_native", blackhole_llm=True)
    await seed_killed_state(home, pinned_agent="native")
    max_cycles = ATTEMPT_CRASH_STREAK_LIMIT + ATTEMPT_INTERRUPTED_STREAK_LIMIT + 2
    terminal_cycle = None
    for cycle in range(1, max_cycles + 1):
        engine = await boot_engine(home)
        resume_task = asyncio.create_task(
            engine.process_message(
                "continue",
                project_id="default",
                session_id="sess-parent",
                mode="company",
                company_profile="corporate",
                message_metadata={"ui_force_resume": True},
            )
        )
        # Wait for a durable claim (attempt opened) or a terminal verdict.
        claimed_seq = None
        outcome = "no-claim"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            item = await get_item(home, "wi-intake-1")
            if item is None:
                continue
            metadata = dict(item.metadata or {})
            if item.phase.value == "failed":
                outcome = "terminal"
                break
            seq = int(metadata.get("attempt_seq", 0) or 0)
            if seq and not bool(metadata.get("attempt_settled", True)):
                claimed_seq = seq
                outcome = f"claimed(attempt {seq})"
                break
            if resume_task.done():
                outcome = "resume-returned"
                break
        # Kill mid-flight (the reporter's repeated restart).
        resume_task.cancel()
        try:
            await asyncio.wait_for(resume_task, timeout=30)
        except (asyncio.CancelledError, Exception):
            pass
        await drop_engine(engine)
        item = await get_item(home, "wi-intake-1")
        metadata = dict(item.metadata or {}) if item else {}
        print(
            f"  cycle {cycle}: {outcome} → phase={item.phase.value if item else '?'} "
            f"crash_streak={metadata.get('attempt_crash_streak', 0)} "
            f"interrupted_streak={metadata.get('attempt_interrupted_streak', 0)} "
            f"attempt_seq={metadata.get('attempt_seq', 0)}"
        )
        if item is not None and item.phase.value == "failed":
            terminal_cycle = cycle
            break
    item = await get_item(home, "wi-intake-1")
    metadata = dict(item.metadata or {}) if item else {}
    check(
        f"kill-loop converges to FAILED within {max_cycles} cycles",
        terminal_cycle is not None,
        f"terminal_cycle={terminal_cycle} blocked_reason={str(item.blocked_reason or '')[:120] if item else ''}",
    )
    check(
        "ledger accounting visible (streaks/attempts recorded)",
        int(metadata.get("attempt_seq", 0) or 0) >= 1
        and (
            int(metadata.get("attempt_crash_streak", 0) or 0) >= 1
            or int(metadata.get("attempt_interrupted_streak", 0) or 0) >= 1
            or "attempt ledger" in str(item.blocked_reason or "")
            or str(metadata.get("last_transition_reason", "")) == "claimed_work_item_exception"
        ),
        f"metadata attempt_*: seq={metadata.get('attempt_seq')} crash={metadata.get('attempt_crash_streak')} intr={metadata.get('attempt_interrupted_streak')} reason={metadata.get('last_transition_reason')}",
    )


async def main() -> None:
    await phase_abcd()
    await phase_e()
    print("\n==== SUMMARY ====")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
