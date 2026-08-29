from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.repo_map import RepoMapBuilder
from coding_agent.session import AgentState
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import (
    ToolRegistry,
    Workspace,
    register_git_tools,
    register_repo_map_tool,
)


def test_repo_map_ranks_query_matches_and_python_symbols(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "billing.py").write_text(
        "import decimal\n\nclass Invoice:\n    pass\n\ndef total():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")

    workspace = Workspace(tmp_path)
    data = RepoMapBuilder(workspace).build(query="billing invoice", max_files=5)

    first = data["files"][0]
    assert first["path"] == "src/billing.py"
    assert "class Invoice" in first["symbols"]
    assert "def total" in first["symbols"]
    assert "decimal" in first["imports"]


def test_repo_map_adds_python_import_edges_and_centrality(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "core.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    (src / "app.py").write_text("from src.core import shared\n\ndef run():\n    return shared()\n", encoding="utf-8")
    (src / "cli.py").write_text("from src.core import shared\n", encoding="utf-8")

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="core")
    by_path = {item["path"]: item for item in data["files"]}

    assert "src/core.py" in by_path["src/app.py"]["dependencies"]
    assert by_path["src/core.py"]["centrality"] == 2
    assert "imported by:2" in by_path["src/core.py"]["reason"]


def test_repo_map_links_unique_python_call_targets(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text(
        "def normalize(value):\n    return value.strip()\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "def run(value):\n    return normalize(value)\n", encoding="utf-8"
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "normalize" in by_path["app.py"]["calls"]
    assert by_path["app.py"]["dependencies"] == ["core.py"]
    assert by_path["core.py"]["dependents"] == ["app.py"]


def test_repo_map_links_static_python_dynamic_imports(tmp_path: Path) -> None:
    (tmp_path / "plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "loader.py").write_text(
        "import importlib\n"
        "plugin = importlib.import_module('plugin')\n"
        "fallback = __import__('plugin')\n",
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="loader", max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "plugin" in by_path["loader.py"]["imports"]
    assert by_path["loader.py"]["dependencies"] == ["plugin.py"]


def test_repo_map_extracts_cpp_and_java_symbols_and_imports(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text(
        '#include <vector>\n\nclass Runner {};\nint execute(int value) { return value; }\n',
        encoding="utf-8",
    )
    (tmp_path / "Service.java").write_text(
        "import java.util.List;\n\npublic class Service {\n  public void run() {}\n}\n",
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="Runner Service", max_files=5)
    by_path = {item["path"]: item for item in data["files"]}

    assert "vector" in by_path["main.cpp"]["imports"]
    assert "class Runner" in by_path["main.cpp"]["symbols"]
    assert "def execute" in by_path["main.cpp"]["symbols"]
    assert "java.util.List" in by_path["Service.java"]["imports"]
    assert "class Service" in by_path["Service.java"]["symbols"]
    assert "def run" in by_path["Service.java"]["symbols"]


def test_repo_map_links_local_cpp_include_edges(tmp_path: Path) -> None:
    include = tmp_path / "include"
    source = tmp_path / "src"
    include.mkdir()
    source.mkdir()
    (include / "math.hpp").write_text("int add(int a, int b);\n", encoding="utf-8")
    (source / "main.cpp").write_text(
        '#include "../include/math.hpp"\nint main() { return add(1, 2); }\n',
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="main", max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "../include/math.hpp" in by_path["src/main.cpp"]["imports"]
    assert by_path["src/main.cpp"]["dependencies"] == ["include/math.hpp"]


def test_repo_map_links_go_module_package_imports(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    package = tmp_path / "internal" / "mathx"
    package.mkdir(parents=True)
    (package / "math.go").write_text("package mathx\nfunc Add() int { return 1 }\n", encoding="utf-8")
    (tmp_path / "main.go").write_text(
        'package main\n\nimport (\n    "fmt"\n    "example.com/demo/internal/mathx"\n)\n\nfunc main() { fmt.Println(mathx.Add()) }\n',
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="main", max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "example.com/demo/internal/mathx" in by_path["main.go"]["imports"]
    assert by_path["main.go"]["dependencies"] == ["internal/mathx/math.go"]


def test_repo_map_links_local_javascript_and_typescript_imports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "shared.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    (src / "feature.ts").write_text(
        'import { answer } from "./shared";\nexport function run() { return answer; }\n',
        encoding="utf-8",
    )
    (src / "legacy.js").write_text(
        'const shared = require("./shared");\n', encoding="utf-8"
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="shared", max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "./shared" in by_path["src/feature.ts"]["imports"]
    assert "src/shared.ts" in by_path["src/feature.ts"]["dependencies"]
    assert "src/shared.ts" in by_path["src/legacy.js"]["dependencies"]
    assert by_path["src/shared.ts"]["centrality"] == 2


def test_repo_map_links_javascript_re_exports(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "shared.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    (src / "barrel.ts").write_text(
        'export { answer } from "./shared";\n', encoding="utf-8"
    )
    (src / "app.ts").write_text(
        'import { answer } from "./barrel";\nconsole.log(answer);\n',
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert by_path["src/barrel.ts"]["dependencies"] == ["src/shared.ts"]
    assert by_path["src/app.ts"]["dependencies"] == ["src/barrel.ts"]


def test_repo_map_links_local_java_class_imports(tmp_path: Path) -> None:
    package = tmp_path / "src" / "main" / "java" / "com" / "example"
    package.mkdir(parents=True)
    (package / "Shared.java").write_text(
        "package com.example;\npublic class Shared {}\n", encoding="utf-8"
    )
    (package / "App.java").write_text(
        "package com.example;\nimport com.example.Shared;\npublic class App {}\n",
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(query="Shared", max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "src/main/java/com/example/Shared.java" in by_path["src/main/java/com/example/App.java"]["dependencies"]
    assert by_path["src/main/java/com/example/Shared.java"]["centrality"] == 1


def test_repo_map_uses_java_package_declaration_when_path_differs(tmp_path: Path) -> None:
    library = tmp_path / "src" / "lib"
    app = tmp_path / "src" / "app"
    library.mkdir(parents=True)
    app.mkdir(parents=True)
    (library / "Shared.java").write_text(
        "package com.example;\npublic class Shared {}\n", encoding="utf-8"
    )
    (app / "App.java").write_text(
        "package app;\nimport com.example.Shared;\npublic class App {}\n",
        encoding="utf-8",
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(max_files=10)
    by_path = {item["path"]: item for item in data["files"]}

    assert "src/lib/Shared.java" in by_path["src/app/App.java"]["dependencies"]


def test_context_injects_budgeted_repo_map_metadata(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    state = AgentState.create()
    state.messages = [{"role": "user", "content": "find app"}]
    context = ContextManager(
        workspace=Workspace(tmp_path), max_repo_map_chars=80
    ).build(state, [])

    assert "Repository map" in context.messages[0]["content"]
    assert context.repo_map["budget"] == 80
    assert context.repo_map["selected_chars"] <= 80


def test_context_can_disable_repo_map_for_control_runs(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    state = AgentState.create()
    state.messages = [{"role": "user", "content": "find app"}]

    context = ContextManager(
        workspace=Workspace(tmp_path), repo_map_enabled=False
    ).build(state, [])

    assert "Repository map" not in context.messages[0]["content"]
    assert context.repo_map["selected"] == []
    assert context.repo_map["disabled_by_config"] is True
    assert context.repo_map["disabled_by_profile"] is False


def test_repo_map_reuses_hash_keyed_summary_and_invalidates_external_edit(tmp_path: Path) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("class First {};\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    builder = RepoMapBuilder(workspace)

    first = builder.build(query="First")
    second = builder.build(query="First")
    assert first["totals"]["summary_cache_misses"] == 1
    assert second["totals"]["summary_cache_hits"] == 1

    source.write_text("class Second {};\n", encoding="utf-8")
    third = builder.build(query="Second")
    assert third["totals"]["summary_cache_misses"] == 1
    assert "class Second" in third["files"][0]["symbols"]


def test_repo_map_reuses_dependency_graph_until_importer_changes(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    importer = tmp_path / "app.py"
    importer.write_text("from core import shared\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    builder = RepoMapBuilder(workspace)

    first = builder.build(query="core")
    second = builder.build(query="core")
    assert first["totals"]["dependency_graph_cache_misses"] == 1
    assert second["totals"]["dependency_graph_cache_hits"] == 1

    importer.write_text("# import removed\n", encoding="utf-8")
    third = builder.build(query="core")
    assert third["totals"]["dependency_graph_incremental_updates"] == 1
    assert third["files"][0]["centrality"] == 0


def test_repo_map_incremental_graph_preserves_unaffected_edges(tmp_path: Path) -> None:
    (tmp_path / "core_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "core_b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    importer_a = tmp_path / "app_a.py"
    importer_a.write_text("from core_a import a\n", encoding="utf-8")
    (tmp_path / "app_b.py").write_text("from core_b import b\n", encoding="utf-8")

    workspace = Workspace(tmp_path)
    builder = RepoMapBuilder(workspace)
    first = builder.build(query="core")
    first_by_path = {item["path"]: item for item in first["files"]}
    assert first_by_path["core_a.py"]["centrality"] == 1
    assert first_by_path["core_b.py"]["centrality"] == 1

    importer_a.write_text("# import removed\n", encoding="utf-8")
    second = builder.build(query="core")
    second_by_path = {item["path"]: item for item in second["files"]}

    assert second["totals"]["dependency_graph_incremental_updates"] == 1
    assert second_by_path["core_a.py"]["centrality"] == 0
    assert second_by_path["core_b.py"]["centrality"] == 1
    assert second_by_path["core_b.py"]["dependents"] == ["app_b.py"]


def test_repo_map_rebuilds_graph_for_module_path_set_changes(tmp_path: Path) -> None:
    (tmp_path / "core.py").write_text("def shared():\n    return 1\n", encoding="utf-8")
    importer = tmp_path / "app.py"
    importer.write_text("from core import shared\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    builder = RepoMapBuilder(workspace)

    builder.build(query="core")
    (tmp_path / "new_module.py").write_text("value = 1\n", encoding="utf-8")
    second = builder.build(query="core")

    assert second["totals"]["dependency_graph_cache_misses"] == 1
    assert second["totals"]["dependency_graph_incremental_updates"] == 0


def test_repo_map_cache_survives_workspace_restart(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("def cached_symbol():\n    return 1\n", encoding="utf-8")

    first_workspace = Workspace(tmp_path)
    first = RepoMapBuilder(first_workspace).build(query="cached")
    assert first["totals"]["summary_cache_misses"] == 1

    restarted_workspace = Workspace(tmp_path)
    second = RepoMapBuilder(restarted_workspace).build(query="cached")
    assert second["totals"]["summary_cache_hits"] == 1
    assert second["totals"]["dependency_graph_cache_hits"] == 1
    assert "def cached_symbol" in second["files"][0]["symbols"]


def test_legacy_repo_map_cache_is_refreshed_for_call_edges(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text("def target():\n    return 1\n", encoding="utf-8")
    app = tmp_path / "app.py"
    app.write_text(
        "from module import target\n\ndef run():\n    return target()\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    cache_file = tmp_path / ".code-helper" / "cache" / "repo-map.json"
    cache_file.parent.mkdir(parents=True)
    # Simulate a cache written before the ``calls`` field was introduced.
    cache_file.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "module.py": {
                        "sha256": digest,
                        "imports": [],
                        "symbols": ["def target"],
                    }
                },
                "graph": {
                    "signature": [["module.py", digest]],
                    "metadata": {},
                },
            }
        ),
        encoding="utf-8",
    )

    workspace = Workspace(tmp_path)
    assert workspace.repo_graph_cache is None
    data = RepoMapBuilder(workspace).build(query="target", max_files=10)

    assert data["totals"]["summary_cache_misses"] == 2
    app_entry = next(item for item in data["files"] if item["path"] == "app.py")
    assert app_entry["dependencies"] == ["module.py"]
    refreshed = json.loads(cache_file.read_text(encoding="utf-8"))
    assert refreshed["files"]["module.py"]["calls"] == []


def test_repo_map_marks_explicit_build_roots(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "src" / "index.js").write_text("export default 1;", encoding="utf-8")
    (tmp_path / "bin" / "cli.js").write_text("console.log('ok');", encoding="utf-8")
    (tmp_path / "pkg" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (tmp_path / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"main": "src/index.js", "bin": {"demo": "bin/cli.js"}}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project.scripts]\ndemo = 'pkg.cli:main'\n", encoding="utf-8"
    )
    (tmp_path / "CMakeLists.txt").write_text(
        "add_executable(native src/main.cpp)\n", encoding="utf-8"
    )

    data = RepoMapBuilder(Workspace(tmp_path)).build(max_files=20)
    by_path = {item["path"]: item for item in data["files"]}

    assert set(data["totals"]["build_roots"]) == {
        "src/index.js",
        "bin/cli.js",
        "pkg/cli.py",
        "src/main.cpp",
    }
    assert all(by_path[path]["build_root"] for path in data["totals"]["build_roots"])
    assert "build root" in by_path["pkg/cli.py"]["reason"]


def test_get_repo_map_is_registered_as_read_tool(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_repo_map_tool(registry, workspace)
    executor = ToolExecutor(registry)

    result = asyncio.run(
        executor.execute("get_repo_map", {"query": "main", "max_files": 10})
    )

    assert result.ok is True
    assert result.data["files"][0]["path"] == "app.py"


def test_context_injects_project_rules(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Always inspect tests before editing.", encoding="utf-8"
    )
    workspace = Workspace(tmp_path)
    context = ContextManager(workspace=workspace).build(AgentState.create(), [])

    system = context.messages[0]["content"]
    assert "Project rules:" in system
    assert "Always inspect tests before editing." in system


def test_context_uses_target_path_rule_chain_and_override(tmp_path: Path) -> None:
    src = tmp_path / "src"
    unrelated = tmp_path / "docs"
    src.mkdir()
    unrelated.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (src / "AGENTS.md").write_text("src default rule", encoding="utf-8")
    (src / "AGENTS.override.md").write_text("src override rule", encoding="utf-8")
    (unrelated / "AGENTS.md").write_text("docs rule must stay out", encoding="utf-8")
    (src / "app.py").write_text("value = 1\n", encoding="utf-8")

    state = AgentState.create()
    state.changed_files.add("src/app.py")
    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])
    system = context.messages[0]["content"]

    assert "root rule" in system
    assert "src override rule" in system
    assert "src default rule" not in system
    assert "docs rule must stay out" not in system
    assert [item["path"] for item in context.rule_sources] == [
        "AGENTS.md",
        "src/AGENTS.override.md",
    ]
    assert context.rule_candidates == 3


def test_context_rule_budget_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * 100, encoding="utf-8")
    state = AgentState.create()
    context = ContextManager(
        workspace=Workspace(tmp_path), max_rule_chars=24
    ).build(state, [])

    assert context.rule_truncated is True
    assert context.rule_chars == 24
    assert context.rule_sources[0]["truncated"] is True


def test_context_targets_recent_tool_path_when_no_file_changed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (src / "AGENTS.md").write_text("src rule", encoding="utf-8")
    state = AgentState.create()
    state.recent_actions.append(
        {
            "signature": json.dumps(
                {"name": "read_file", "arguments": {"path": "src/app.py"}}
            )
        }
    )

    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])

    assert "src rule" in context.messages[0]["content"]


def test_get_diff_tool_returns_workspace_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    sample = tmp_path / "sample.txt"
    sample.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    sample.write_text("new\n", encoding="utf-8")

    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_git_tools(registry, workspace)
    executor = ToolExecutor(registry)

    result = asyncio.run(executor.execute("get_diff", {}))

    assert result.ok is True
    assert "-old" in result.data["diff"]
    assert "+new" in result.data["diff"]
