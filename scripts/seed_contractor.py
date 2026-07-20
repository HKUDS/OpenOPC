"""Administrative Seeding Utility for OpenOPC Shadow Mode.

Seeds or updates human contractor profiles into the consolidated EMPLOYEES table
with PBKDF2 hashed passwords via opc.core.auth.

Usage:
    python scripts/seed_contractor.py --username john_dev --password mysecretpass --name "John Contractor" --role developer --access worker
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from opc.core.auth import hash_password
from opc.core.config import EmployeeConfig
from opc.database.store import OPCStore


async def seed_contractor(
    db_path: str,
    username: str,
    password: str,
    name: str,
    role_id: str,
    access_level: str = "worker",
) -> None:
    store = OPCStore(db_path)
    await store.ensure_ready()

    employee_id = f"human_{username}"
    hashed_pwd = hash_password(password)

    employee = EmployeeConfig(
        employee_id=employee_id,
        name=name,
        role_id=role_id,
        username=username,
        hashed_password=hashed_pwd,
        is_human=True,
        access_level=access_level,
        description=f"Human contractor assigned to {role_id}",
        seniority="senior",
        status="active",
        metadata={"shadow_mode": True, "registered_via": "seed_contractor.py"},
    )

    await store.save_employee(employee)
    print(f"[SUCCESS] Successfully seeded human contractor into '{db_path}':")
    print(f"   - Employee ID: {employee_id}")
    print(f"   - Name: {name}")
    print(f"   - Username: {username}")
    print(f"   - Assigned Role: {role_id}")
    print(f"   - Access Level: {access_level}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed Human Contractor into OpenOPC EMPLOYEES table.")
    parser.add_argument("--db-path", default=os.getenv("OPC_DB_PATH", ".opc/tasks.db"), help="Path to SQLite database")
    parser.add_argument("--username", required=True, help="Contractor login username")
    parser.add_argument("--password", required=True, help="Contractor login password")
    parser.add_argument("--name", required=True, help="Contractor full name")
    parser.add_argument("--role", required=True, help="Assigned role ID in org topology")
    parser.add_argument("--access", default="worker", choices=["worker", "admin"], help="Access level")

    args = parser.parse_args()

    asyncio.run(
        seed_contractor(
            db_path=args.db_path,
            username=args.username,
            password=args.password,
            name=args.name,
            role_id=args.role,
            access_level=args.access,
        )
    )


if __name__ == "__main__":
    main()
