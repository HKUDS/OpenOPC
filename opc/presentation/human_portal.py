"""Streamlit Human Contractor Portal for OpenOPC Shadow Mode.

Provides secure JWT authentication, task queue inspection for human contractors,
deliverable upload, and Layer 0/5/6 event trigger pipeline.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import streamlit as st

from opc.core.auth import create_jwt_token, hash_password, verify_jwt_token, verify_password
from opc.database.store import OPCStore
from opc.layer0_interaction.message_bus import MessageBus
from opc.layer6_observability.opc_logger import OPCLogger


def init_store() -> OPCStore:
    db_path = os.getenv("OPC_DB_PATH", ".opc/tasks.db")
    store = OPCStore(db_path)
    asyncio.run(store.ensure_ready())
    return store


def render_login_page(store: OPCStore) -> None:
    st.title("OpenOPC Contractor Portal")
    st.subheader("Shadow Mode Authentication")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log In")

        if submitted:
            if not username or not password:
                st.error("Please provide both username and password.")
                return

            employee = asyncio.run(store.get_employee_by_username(username))
            if not employee or not verify_password(password, employee.get("hashed_password", "")):
                st.error("Invalid credentials or user not registered as contractor.")
                return

            # Store verified JWT token in st.session_state AND st.query_params for browser refresh persistence
            payload = {
                "sub": employee["employee_id"],
                "username": employee["username"],
                "name": employee["name"],
                "role_id": employee["role_id"],
                "access_level": employee.get("access_level", "worker"),
            }
            token = create_jwt_token(payload)
            st.session_state["jwt_token"] = token
            st.session_state["user"] = payload
            st.query_params["token"] = token
            st.success(f"Welcome back, {employee['name']}!")
            st.rerun()


def render_contractor_dashboard(store: OPCStore, user: dict[str, Any]) -> None:
    st.sidebar.title(f"User: {user.get('name', 'Contractor')}")
    st.sidebar.caption(f"Role: `{user.get('role_id', 'worker')}`")
    st.sidebar.caption(f"Access: `{user.get('access_level', 'worker')}`")
    
    if st.sidebar.button("Log Out"):
        st.session_state.clear()
        st.query_params.clear()
        st.rerun()

    tab1, tab2 = st.tabs(["Task Queue", "Organizational Velocity & Changelog"])

    with tab1:
        st.header("Assigned Work Items & Deliverables")
        st.info("Showing tasks requiring your manual deliverable in Shadow Mode.")

        user_role = user.get("role_id", "")
        all_tasks = asyncio.run(store.list_tasks(status=None))
        
        assigned_tasks = [
            t for t in all_tasks
            if str(t.assigned_to or t.metadata.get("owner_role") or "").strip() == user_role
            and (t.status in ("running", "awaiting_human", "pending") or t.metadata.get("sub_state") == "AWAITING_HUMAN_DELIVERABLE")
        ]

        if not assigned_tasks:
            st.warning(f"No pending work items requiring deliverables for role '{user_role}'.")
        else:
            selected_task_title = st.selectbox(
                "Select Task to Submit Deliverable",
                options=[f"{t.id} - {t.title}" for t in assigned_tasks]
            )

            if selected_task_title:
                task_id = selected_task_title.split(" - ")[0]
                task = next((t for t in assigned_tasks if t.id == task_id), None)
                
                if task:
                    st.markdown(f"### **Task**: {task.title}")
                    st.markdown(f"**Description**: {task.description}")
                    st.markdown(f"**Status**: `{task.status.value if hasattr(task.status, 'value') else task.status}`")
                    
                    st.divider()
                    st.subheader("Submit Deliverable")
                    
                    summary_notes = st.text_area("Deliverable Notes / Summary", placeholder="Describe the work completed...")
                    uploaded_file = st.file_uploader("Upload Deliverable Artifact (Code, Report, Changelog, Markdown)", type=["py", "md", "txt", "json", "docx", "xlsx"])

                    if st.button("Submit Deliverable & Resume Pipeline"):
                        if not uploaded_file and not summary_notes.strip():
                            st.error("Please provide notes or upload a file.")
                        else:
                            artifacts_list = []
                            file_content = summary_notes.strip()

                            if uploaded_file is not None:
                                dest_dir = Path(".opc/workspace/deliverables") / task_id
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                dest_path = dest_dir / uploaded_file.name
                                with open(dest_path, "wb") as f:
                                    f.write(uploaded_file.getbuffer())

                                try:
                                    extracted_text = dest_path.read_text(encoding="utf-8", errors="ignore")
                                    file_content += f"\n\n--- Attached File Content ({uploaded_file.name}) ---\n{extracted_text}"
                                except Exception:
                                    file_content += f"\n\nUploaded file: {dest_path} (Raw size: {len(uploaded_file.getbuffer())} bytes)"

                                artifacts_list.append({
                                    "name": uploaded_file.name,
                                    "path": str(dest_path),
                                    "size": len(uploaded_file.getbuffer())
                                })

                            logger = OPCLogger(store=store)
                            asyncio.run(logger.log_trajectory_step(
                                task_id=task.id,
                                role_id=user_role,
                                employee_id=user.get("sub", "human_contractor"),
                                action="HUMAN_DELIVERABLE_SUBMISSION",
                                content=file_content,
                                artifacts=artifacts_list,
                            ))

                            mbus = MessageBus()
                            mbus.publish_event(
                                f"deliverable_completed:{task.id}",
                                {
                                    "task_id": task.id,
                                    "summary": file_content,
                                    "content": file_content,
                                    "artifacts": artifacts_list,
                                    "username": user.get("username", "human_contractor"),
                                }
                            )

                            st.success("Deliverable submitted! The execution pipeline is resuming...")
                            st.rerun()

    with tab2:
        st.header("Time-Machine Analytics & Organizational Velocity")
        st.subheader("Historical Performance Aggregation (Global, Team, Individual)")

        interval = st.radio("Aggregation Interval", options=["weekly", "daily"], horizontal=True)
        perf_data = asyncio.run(store.get_temporal_performance(interval=interval))

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### Global Organization Velocity")
            global_records = perf_data.get("global", [])
            if global_records:
                import pandas as pd
                df_global = pd.DataFrame(global_records)
                st.line_chart(df_global.set_index("time_bucket")[["completed_tasks", "total_active_tasks"]])
            else:
                st.info("No global performance data available yet.")

        with col2:
            st.markdown("### Team Slices")
            team_dict = perf_data.get("team", {})
            if team_dict:
                team_options = list(team_dict.keys())
                selected_team = st.selectbox("Select Team / Role", options=team_options)
                if selected_team:
                    import pandas as pd
                    df_team = pd.DataFrame(team_dict[selected_team])
                    st.bar_chart(df_team.set_index("time_bucket")[["completed_tasks"]])
            else:
                st.info("No team slice metrics recorded yet.")

        st.divider()
        col3, col4 = st.columns(2)

        with col3:
            st.markdown("### Individual Performance Slices (Human & AI Employees)")
            indiv_dict = perf_data.get("individual", {})
            if indiv_dict:
                selected_actor = st.selectbox("Select Employee / Contractor", options=list(indiv_dict.keys()))
                if selected_actor:
                    import pandas as pd
                    df_indiv = pd.DataFrame(indiv_dict[selected_actor])
                    st.line_chart(df_indiv.set_index("time_bucket")[["event_count", "impact_score"]])
            else:
                st.info("No individual performance entries recorded yet.")

        with col4:
            st.markdown("### Organizational Changelog Feed")
            search_query = st.text_input("Search Changelog Feed (by event, actor, or description)", placeholder="Filter events...")

            changelogs = asyncio.run(store.get_org_changelogs(limit=50, search=search_query))
            if changelogs:
                import pandas as pd
                df_logs = pd.DataFrame(changelogs)[["timestamp", "event_type", "actor_id", "description", "impact_score"]]
                st.dataframe(df_logs, use_container_width=True)
            else:
                st.info("No organizational changelog events match your search query.")


def main() -> None:
    st.set_page_config(page_title="OpenOPC Contractor Portal", layout="wide")
    store = init_store()

    token = st.query_params.get("token") or st.session_state.get("jwt_token")
    user_claims = verify_jwt_token(token) if token else None

    if not user_claims:
        render_login_page(store)
    else:
        st.session_state["jwt_token"] = token
        st.session_state["user"] = user_claims
        render_contractor_dashboard(store, user_claims)


if __name__ == "__main__":
    main()
