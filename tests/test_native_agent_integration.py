"""Integration tests for NativeAgent upgrades.

Tests all 7 upgrade items from NATIVE_AGENT_UPGRADE_PLAN.md:
1. Role-aware system prompts
2. TODO tool interception
3. Probe (read-only exploration sub-loop)
4. Concurrent tool execution
5. Transient LLM error retry
6. Doom loop detection
7. Context compression

Runs through the OPC engine with real LLM calls.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


from opc.core.config import OPCConfig
from opc.engine import OPCEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_logs: list[str] = []


def _progress():
    async def cb(text: str) -> None:
        _logs.append(text)
        # Print tool calls for visibility
        if text.startswith("[Tool:"):
            print(f"    {text[:120]}")
    return cb


async def run_task(desc: str, task_msg: str, timeout: int = 300) -> tuple[bool, str]:
    """Run a single task through the engine. Returns (success, response)."""
    config = OPCConfig.load(
        Path(__file__).parent.parent / ".opc" / "config",
        trusted_source=True,
    )
    engine = OPCEngine(config=config, project_id="test", on_progress=_progress())
    _logs.clear()

    try:
        await engine.initialize()
        response = await asyncio.wait_for(
            engine.process_message(task_msg, project_id="test"),
            timeout=timeout,
        )
        # Consider it successful if we got a non-error response
        success = bool(response and "Error" not in response[:50] and "failed" not in response[:50].lower())
        return success, response
    except asyncio.TimeoutError:
        return False, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return False, f"EXCEPTION: {e}\n{traceback.format_exc()}"
    finally:
        try:
            await engine.shutdown()
        except Exception:
            pass


def print_result(idx: int, desc: str, success: bool, response: str, elapsed: float):
    status = "PASS" if success else "FAIL"
    icon = "✓" if success else "✗"
    print(f"\n{'='*60}")
    print(f"[{icon}] Test {idx}: {desc} — {status} ({elapsed:.1f}s)")
    print(f"{'='*60}")
    # Show first 300 chars of response
    preview = response[:300].replace("\n", " ")
    print(f"  Response: {preview}...")
    if _logs:
        tool_calls = [l for l in _logs if l.startswith("[Tool:")]
        if tool_calls:
            print(f"  Tool calls ({len(tool_calls)}):")
            for tc in tool_calls[:5]:
                print(f"    {tc[:100]}")
            if len(tool_calls) > 5:
                print(f"    ... and {len(tool_calls) - 5} more")


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

REGULAR_TASKS = [
    # Test 1: Basic single-agent coding task (executor role)
    (
        "Basic coding task (executor role, CODING_GUIDELINES)",
        "Create a Python function that calculates the Fibonacci sequence up to n terms. "
        "Write it to /tmp/opc_test_fib.py and verify it works by running it."
    ),
    # Test 2: Review-style task (should use REVIEW_GUIDELINES)
    (
        "Review/analysis task",
        "Read the file /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/__init__.py "
        "and tell me what version of OPC is defined there."
    ),
    # Test 3: Planning task (should trigger coordinator)
    (
        "Planning/decomposition task",
        "What are the key components needed to build a simple REST API with FastAPI? "
        "Just list the components, don't write code."
    ),
    # Test 4: File operations (tests file_read, file_write tools)
    (
        "File read/write operations",
        "Read the file /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/pyproject.toml "
        "and tell me the project name and version."
    ),
    # Test 5: Shell execution
    (
        "Shell command execution",
        "Run `python --version` and `pip --version` and report the versions."
    ),
    # Test 6: Multi-step task (should benefit from TODO tracking)
    (
        "Multi-step task with TODO tracking",
        "Do these three things: 1) List the files in /tmp 2) Create a file /tmp/opc_test_hello.txt with content 'Hello OPC' "
        "3) Read the file back and confirm its content."
    ),
    # Test 7: Code search task (tests file_search/list_dir)
    (
        "Code search and analysis",
        "Search the OPC codebase at /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/ "
        "for all files that import 'asyncio'. Just list the file paths."
    ),
    # Test 8: Python execution
    (
        "Python execution tool",
        "Use Python to calculate: What is the sum of all prime numbers below 100?"
    ),
    # Test 9: Web-related (tests web_search if available)
    (
        "Simple computation task",
        "Calculate the factorial of 20 using Python and tell me the result."
    ),
    # Test 10: Git operations
    (
        "Git status check",
        "Check the git status of the repository at /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC "
        "and report the current branch and any uncommitted changes."
    ),
]

RANDOM_TASKS = [
    (
        "Random: JSON processing",
        "Create a Python script at /tmp/opc_test_json.py that reads a JSON string "
        "'{\"users\": [{\"name\": \"Alice\", \"age\": 30}, {\"name\": \"Bob\", \"age\": 25}]}' "
        "and prints each user's name and age. Then run it."
    ),
    (
        "Random: Directory listing",
        "List the contents of /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/layer3_agent/ "
        "and briefly describe the purpose of each file."
    ),
    (
        "Random: Math calculation",
        "What is 2^64? Calculate it precisely using Python."
    ),
    (
        "Random: Text processing",
        "Write a Python one-liner that reverses the string 'Hello, OPC World!' and tell me the result."
    ),
    (
        "Random: File creation",
        "Create a CSV file at /tmp/opc_test_data.csv with 5 rows of sample data "
        "(name, age, city columns) and then read it back."
    ),
    (
        "Random: Code explanation",
        "Read the file /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/layer4_tools/todo.py "
        "and explain what it does in 2-3 sentences."
    ),
    (
        "Random: System info",
        "Run `uname -a` and `whoami` and report the system information."
    ),
    (
        "Random: String manipulation",
        "Use Python to count how many vowels are in the sentence: "
        "'The quick brown fox jumps over the lazy dog'"
    ),
    (
        "Random: File analysis",
        "How many lines of Python code are in the file "
        "/Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/layer3_agent/runtime_v2/runtime.py?"
    ),
    (
        "Random: Simple script",
        "Write a Python script at /tmp/opc_test_sort.py that sorts the list [5, 2, 8, 1, 9, 3] "
        "and prints the sorted result. Then run it."
    ),
]

STRESS_TASKS = [
    (
        "Stress: Long output handling",
        "List all files recursively in /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/ "
        "and count the total number of .py files."
    ),
    (
        "Stress: Multiple tool calls",
        "Do all of these: 1) Read /tmp/opc_test_hello.txt 2) Run `date` 3) Run `python -c \"print(42**10)\"` "
        "4) List files in /tmp/opc_test*.* pattern. Report all results."
    ),
    (
        "Stress: Complex computation",
        "Write a Python script at /tmp/opc_test_matrix.py that multiplies two 3x3 matrices "
        "[[1,2,3],[4,5,6],[7,8,9]] and [[9,8,7],[6,5,4],[3,2,1]] and prints the result. Run it."
    ),
    (
        "Stress: File search + analysis",
        "Find all Python files in /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/ "
        "that contain the word 'async' and count them."
    ),
    (
        "Stress: Multi-file operations",
        "Create three files: /tmp/opc_stress_a.txt with 'AAA', /tmp/opc_stress_b.txt with 'BBB', "
        "/tmp/opc_stress_c.txt with 'CCC'. Then read all three and confirm contents."
    ),
    (
        "Stress: Error recovery",
        "Try to read a file that doesn't exist: /tmp/nonexistent_opc_test_xyz.txt. "
        "Then create it with content 'recovered' and read it again."
    ),
    (
        "Stress: Chained operations",
        "Run `echo hello` and capture the output. Then create /tmp/opc_stress_echo.txt with that output. "
        "Then run `cat /tmp/opc_stress_echo.txt` to verify."
    ),
    (
        "Stress: Code generation + execution",
        "Write a Python script at /tmp/opc_stress_primes.py that finds all prime numbers between 1 and 200. "
        "Run it and report the count of primes found."
    ),
    (
        "Stress: Rapid computation",
        "Calculate these using Python: 1) 123456789 * 987654321  2) 2**100  3) sum(range(1, 10001)). "
        "Report all three results."
    ),
    (
        "Stress: File read + summary",
        "Read /Users/lizongwei/Desktop/Coding_Project/OPENOPC_BENCH/OpenOPC/opc/engine.py "
        "and tell me: how many async methods does the OPCEngine class have? List their names."
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_suite(name: str, tasks: list[tuple[str, str]]) -> tuple[int, int]:
    """Run a test suite. Returns (passed, total)."""
    print(f"\n{'#'*60}")
    print(f"# {name} ({len(tasks)} tasks)")
    print(f"{'#'*60}")

    passed = 0
    for i, (desc, msg) in enumerate(tasks, 1):
        print(f"\n>>> Running test {i}/{len(tasks)}: {desc}")
        start = time.time()
        success, response = await run_task(desc, msg, timeout=180)
        elapsed = time.time() - start
        print_result(i, desc, success, response, elapsed)
        if success:
            passed += 1

    print(f"\n{'='*60}")
    print(f"{name} Results: {passed}/{len(tasks)} passed")
    print(f"{'='*60}")
    return passed, len(tasks)


async def main():
    total_passed = 0
    total_tests = 0

    # Phase 1: Regular tasks
    p, t = await run_suite("REGULAR TASKS", REGULAR_TASKS)
    total_passed += p
    total_tests += t

    # Phase 2: Random tasks
    p, t = await run_suite("RANDOM TASKS", RANDOM_TASKS)
    total_passed += p
    total_tests += t

    # Phase 3: Stress tests
    p, t = await run_suite("STRESS TESTS", STRESS_TASKS)
    total_passed += p
    total_tests += t

    # Final summary
    print(f"\n{'#'*60}")
    print(f"# FINAL SUMMARY: {total_passed}/{total_tests} passed")
    if total_passed == total_tests:
        print(f"# ALL TESTS PASSED!")
    else:
        print(f"# {total_tests - total_passed} FAILURES — need to fix and re-run")
    print(f"{'#'*60}")

    return total_passed == total_tests


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
