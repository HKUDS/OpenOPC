"""Shared exact-attempt contract for deterministic final-delivery validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from opc.core.models import DelegationWorkItem, Task


FINAL_DELIVERY_PUBLICATION_PROVENANCE_KEY = (
    "company_final_delivery_publication"
)
FINAL_DELIVERY_PUBLICATION_KIND = (
    "company_controller_authoritative_final_delivery"
)
FINAL_DELIVERY_PUBLICATION_SCHEMA_VERSION = 1


def delivery_package_sha256(package: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(package or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validation_record_matches_current_delivery(
    task: Task,
    work_item: DelegationWorkItem,
    package: Mapping[str, Any],
) -> bool:
    task_record = dict(
        (task.metadata or {}).get("pre_delivery_validation", {}) or {}
    )
    work_item_record = dict(
        (work_item.metadata or {}).get("pre_delivery_validation", {}) or {}
    )
    if task_record != work_item_record:
        return False
    if str(task_record.get("status", "") or "").strip() != "passed":
        return False
    if task_record.get("valid") is not True:
        return False
    try:
        record_attempt = int(
            task_record.get("work_item_attempt_seq", 0) or 0
        )
        task_attempt = int(
            (task.metadata or {}).get(
                "claimed_work_item_attempt_seq", 0
            )
            or 0
        )
        work_item_attempt = int(
            (work_item.metadata or {}).get("attempt_seq", 0) or 0
        )
    except (TypeError, ValueError):
        return False
    if (
        record_attempt <= 0
        or record_attempt != task_attempt
        or record_attempt != work_item_attempt
    ):
        return False
    expected_hash = str(
        task_record.get("delivery_package_sha256", "") or ""
    ).strip()
    if not expected_hash:
        return False
    try:
        return delivery_package_sha256(package) == expected_hash
    except (TypeError, ValueError):
        return False


def final_delivery_publication_provenance(
    *,
    run_id: str,
    task_id: str,
    work_item_id: str,
    attempt_seq: int,
    package_hash: str,
) -> dict[str, Any]:
    """Build the Store-stamped proof of one atomic final publication."""

    return {
        "schema_version": FINAL_DELIVERY_PUBLICATION_SCHEMA_VERSION,
        "commit_kind": FINAL_DELIVERY_PUBLICATION_KIND,
        "run_id": str(run_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "work_item_id": str(work_item_id or "").strip(),
        "work_item_attempt_seq": int(attempt_seq or 0),
        "delivery_package_sha256": str(package_hash or "").strip(),
    }


def final_delivery_checkpoint_payload_matches(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    task_id: str,
    work_item_id: str,
    attempt_seq: int,
    package_hash: str,
) -> bool:
    """Accept only a card stamped by the authoritative final Store command.

    The top-level attempt/hash fields are intentionally checked as well as the
    nested provenance.  They are the public checkpoint contract consumed by
    feedback handlers, while the nested exact-shape record proves that the row
    was inserted in the same transaction as the Task/WorkItem/run handoff.
    """

    if not isinstance(payload, Mapping):
        return False
    try:
        expected = final_delivery_publication_provenance(
            run_id=run_id,
            task_id=task_id,
            work_item_id=work_item_id,
            attempt_seq=attempt_seq,
            package_hash=package_hash,
        )
        actual = payload.get(FINAL_DELIVERY_PUBLICATION_PROVENANCE_KEY)
        return bool(
            expected["run_id"]
            and expected["task_id"]
            and expected["work_item_id"]
            and int(expected["work_item_attempt_seq"]) > 0
            and expected["delivery_package_sha256"]
            and isinstance(actual, Mapping)
            and dict(actual) == expected
            and str(payload.get("waiting_task_id", "") or "").strip()
            == expected["task_id"]
            and str(payload.get("waiting_work_item_id", "") or "").strip()
            == expected["work_item_id"]
            and int(payload.get("work_item_attempt_seq", 0) or 0)
            == int(expected["work_item_attempt_seq"])
            and str(
                payload.get("delivery_package_sha256", "") or ""
            ).strip()
            == expected["delivery_package_sha256"]
        )
    except (TypeError, ValueError):
        return False
