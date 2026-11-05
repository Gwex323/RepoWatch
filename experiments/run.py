import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SUITES = {
    "bootstrap": ["tests/test_bootstrap.py"],
    "protocols": ["tests/test_protocols.py"],
    "e7": ["tests/test_storage_prototype.py"],
    "runtime": ["tests/test_runtime_prototype.py"],
    "e1-contracts": ["tests/test_provider_probe.py"],
    "git-handoff-contracts": ["tests/test_git_handoff.py"],
    "e8-contracts": ["tests/test_remote_recovery.py"],
    "e13-contracts": ["tests/test_scheduling_prototype.py"],
    "embedding-contracts": ["tests/test_embedding_probe.py"],
    "e14-budget": ["tests/test_budgets.py", "tests/test_budget_migration.py"],
    "p0-local": ["tests"],
    "p1-kernel": ["tests"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="执行本地可复现实验切片")
    parser.add_argument(
        "suite", choices=[*SUITES, "e1", "git-handoff", "e8", "e13", "embedding"]
    )
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--provider", help="仅诊断指定名称的供应商")
    parser.add_argument("--api", choices=["chat_completions", "responses"])
    parser.add_argument("--role", choices=["processor", "reviewer"])
    parser.add_argument("--stage", choices=["connection", "tools", "handoff"])
    parser.add_argument("--model", action="append")
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--fastembed-path", type=Path)
    arguments = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("请使用 backend/.venv 中已锁定的 Python 3.12")
    root = Path(__file__).resolve().parents[1]
    if arguments.suite == "e1":
        from repowatch.runtime.provider_probe import ProbeSelection, probe_from_file

        report = probe_from_file(
            arguments.provider_config or root / "experiments/providers.local.json",
            selection=ProbeSelection(
                provider=arguments.provider,
                api=arguments.api,
                role=arguments.role,
                stage=arguments.stage or "handoff",
            ),
        )
        result_directory = root / "experiments/results/local"
        result_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = result_directory / f"e1-{stamp}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "model_calls": report["model_calls"],
                    "diagnostic_only": report.get("diagnostic_only", False),
                    "report": output.relative_to(root).as_posix(),
                }
            )
        )
        passed = report["gate_passed"] or (
            report.get("diagnostic_only", False) and report["status"] == "passed"
        )
        return 0 if passed else 2
    if arguments.suite == "git-handoff":
        from repowatch.runtime.git_handoff import run_experiment

        report = run_experiment()
        result_directory = root / "experiments/results/local"
        result_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = result_directory / f"e11-{stamp}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "error": report["error"],
                    "delivery_ready": report["delivery"]["ready"],
                    "blockers": report["delivery"]["blockers"],
                    "report": output.relative_to(root).as_posix(),
                }
            )
        )
        return 0 if report["status"] == "passed" else 2
    if arguments.suite == "e8":
        from repowatch.runtime.remote_recovery import run_experiment

        report = run_experiment()
        result_directory = root / "experiments/results/local"
        result_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = result_directory / f"e8-{stamp}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "pr_count": report["remote"]["pr_count"],
                    "create_calls": report["remote"]["create_calls"],
                    "report": output.relative_to(root).as_posix(),
                }
            )
        )
        return 0 if report["status"] == "passed" else 2
    if arguments.suite == "e13":
        from repowatch.runtime.scheduling_prototype import run_experiment

        report = run_experiment()
        result_directory = root / "experiments/results/local"
        result_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = result_directory / f"e13-{stamp}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "checks": report["checks"],
                    "report": output.relative_to(root).as_posix(),
                }
            )
        )
        return 0 if report["status"] == "passed" else 2
    if arguments.suite == "embedding":
        from repowatch.runtime.embedding_probe import CANDIDATES, run_experiment

        cache = arguments.embedding_cache or root / ".cache/fastembed"
        fastembed = arguments.fastembed_path
        if fastembed is None:
            candidate = root / ".cache/embedding-eval"
            fastembed = candidate if candidate.is_dir() else None
        report = run_experiment(tuple(arguments.model or CANDIDATES), cache, fastembed)
        result_directory = root / "experiments/results/local"
        result_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        output = result_directory / f"embedding-{stamp}.json"
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "selection": report["selection"],
                    "models": [
                        {
                            "model": item["model"],
                            "error": item.get("error"),
                            "metrics": item.get("metrics"),
                        }
                        for item in report["models"]
                    ],
                    "report": output.relative_to(root).as_posix(),
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["status"] == "passed" else 2
    if any(
        (
            arguments.provider_config,
            arguments.provider,
            arguments.api,
            arguments.role,
            arguments.stage,
        )
    ):
        parser.error("供应商配置和诊断参数仅用于 e1 套件")
    command = [
        sys.executable,
        "-m",
        "pytest",
        *SUITES[arguments.suite],
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if arguments.suite == "p1-kernel":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        command.extend(["--basetemp", str(root / ".cache" / f"p1-kernel-{stamp}")])
    result = subprocess.run(
        command, cwd=root / "backend", capture_output=True, text=True, check=False
    )
    output = root / "experiments" / "results" / "local" / f"{arguments.suite}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "suite": arguments.suite,
                "scope": (
                    "P1 全量后端回归，包含 E7／E14／E18／E20 内核切片及 P0 回归；"
                    "不替代真实供应商、浏览器、容器或最终产品验收"
                    if arguments.suite == "p1-kernel"
                    else "P1 2.4 E14 预算切片；不代表完整进展检测、真实供应商或 P1 阶段通过"
                    if arguments.suite == "e14-budget"
                    else "本地测试切片；不代表阶段验收或真实供应商通过"
                ),
                "python": sys.version.split()[0],
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    print(f"本地结果：{output.relative_to(root).as_posix()}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
