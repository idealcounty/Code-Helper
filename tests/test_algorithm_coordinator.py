from __future__ import annotations

import sys
import time
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.config import AppConfig
from coding_agent.web.app import create_app


def _python_command(path: Path) -> str:
    return f'"{sys.executable}" "{path}"'


def test_direct_algorithm_run_is_deterministic_and_persists_report(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    oracle = tmp_path / "oracle.py"
    candidate.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    value = int(line.strip() or 0)\n"
        "    print(value * 2)\n",
        encoding="utf-8",
    )
    oracle.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    value = int(line.strip() or 0)\n"
        "    print(value + value)\n",
        encoding="utf-8",
    )
    app = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        response = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
            json={
                "candidate_command": _python_command(candidate),
                "candidate_path": "candidate.py",
                "oracle_command": _python_command(oracle),
                "profile": "quick",
                "cases": [
                    {"label": "zero", "input": "0\n"},
                    {"label": "seven", "input": "7\n"},
                ],
            },
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(500):
            status = client.get(
                f"/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/status"
            )
            assert status.status_code == 200
            if not status.json()["running"]:
                break
            time.sleep(0.01)
        result = status.json()["run"]
        assert result["status"] == "completed"
        assert result["model_requests"] == 0
        assert result["report"]["summary"]["passed"] == 2
        assert result["report"]["run"]["profile"] == "quick"
        assert result["report"]["run"]["model_requests"] == 0
        assert all(item["oracle_source"] == "user_command" for item in result["report"]["cases"])
        events = client.get(
            f"/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/events"
        )
        assert events.status_code == 200
        assert events.json()["total"] >= 2

        second = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
            json={
                "candidate_command": _python_command(candidate),
                "candidate_path": "candidate.py",
                "oracle_command": _python_command(oracle),
                "profile": "quick",
                "cases": [
                    {"label": "zero", "input": "0\n"},
                    {"label": "seven", "input": "7\n"},
                ],
            },
        )
        assert second.status_code == 202
        second_id = second.json()["run_id"]
        # Windows process startup can exceed one second under coverage or
        # concurrent test load; keep polling long enough to test the result
        # rather than making the assertion depend on scheduler timing.
        for _ in range(500):
            status = client.get(
                f"/api/sessions/{session_id}/algorithm-lab/runs/{second_id}/status"
            )
            if not status.json()["running"]:
                break
            time.sleep(0.01)
        assert status.json()["run"]["report"]["run"]["cache"]["hits"] >= 1


def test_direct_algorithm_run_fails_fast_and_can_be_cancelled(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("print('wrong')\n", encoding="utf-8")
    app = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"workspace": str(tmp_path)}).json()["session_id"]
        response = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
                json={
                    "candidate_command": _python_command(candidate),
                    "profile": "standard",
                "cases": [{"label": "bad", "input": "1\n", "expected": "1\n"}],
            },
        )
        run_id = response.json()["run_id"]
        for _ in range(500):
            status = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/status")
            if not status.json()["running"]:
                break
            time.sleep(0.01)
        result = status.json()["run"]
        assert result["status"] == "failed"
        assert result["report"]["summary"]["total"] == 1

        repeat = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
            json={
                "candidate_command": _python_command(candidate),
                "profile": "standard",
                "cases": [{"label": "bad", "input": "1\n", "expected": "1\n"}],
            },
        )
        repeat_id = repeat.json()["run_id"]
        for _ in range(100):
            repeat_status = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/{repeat_id}/status")
            if not repeat_status.json()["running"]:
                break
            time.sleep(0.01)
        assert repeat_status.json()["run"]["report"]["run"]["shrink_cache_hit"] is True

        slow = tmp_path / "slow.py"
        slow.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
        response = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
            json={
                "candidate_command": _python_command(slow),
                "profile": "full",
                "cases": [{"label": "slow", "input": "1\n", "expected": "1\n"}],
            },
        )
        slow_id = response.json()["run_id"]
        cancel = client.post(f"/api/sessions/{session_id}/algorithm-lab/runs/{slow_id}/cancel")
        assert cancel.status_code == 200
        for _ in range(500):
            status = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/{slow_id}/status")
            if not status.json()["running"]:
                break
            time.sleep(0.01)
        assert status.json()["run"]["status"] == "cancelled"


def test_compiled_candidate_reuses_source_cache(tmp_path: Path) -> None:
    if shutil.which("g++") is None:
        return
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text(
        "#include <iostream>\n"
        "int main(){ long long value; if (std::cin >> value) std::cout << value * 2 << '\\n'; }\n",
        encoding="utf-8",
    )
    app = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"workspace": str(tmp_path)}).json()["session_id"]

        def run_once() -> dict:
            response = client.post(
                f"/api/sessions/{session_id}/algorithm-lab/runs",
                json={
                    "candidate_path": "candidate.cpp",
                    "profile": "quick",
                    "cases": [{"input": "3\n", "expected": "6\n"}],
                },
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            for _ in range(400):
                status = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/status")
                if not status.json()["running"]:
                    return status.json()["run"]
                time.sleep(0.01)
            raise AssertionError("compiled algorithm run did not finish")

        first = run_once()
        second = run_once()
        assert first["status"] == second["status"] == "completed"
        assert first["report"]["run"]["compile_cache"]["hit"] is False
        assert second["report"]["run"]["compile_cache"]["hit"] is True
