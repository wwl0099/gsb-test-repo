# GSB Pair-wise Automation Script
# Drives codex exec for A/B comparison per PDF spec.
# Output aligns with submission fields: initial SHA, sessionID,
# trace files, product snapshots, harness info.

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    label: str
    thread_id: str
    session_id: str
    trace_src: Path
    trace_dst: Path
    commit_sha: str
    workspace: Path
    diff_summary: str
    parent_verified: bool
    success: bool
    error: str = ""


@dataclass
class PairResult:
    prompt_id: str
    prompt: str
    task_type: str
    difficulty: str
    language: str
    env_reproducibility: str
    initial_sha: str
    harness: str
    harness_version: str
    os_name: str
    run_a: Optional[RunResult] = None
    run_b: Optional[RunResult] = None


@dataclass
class Config:
    codex_home: Path
    base_repo: Path
    output_dir: Path
    initial_sha: str
    github_remote: str = ""
    timeout_seconds: int = 1800
    codex_executable: str = ""
    verbose: bool = False


# ---------------------------------------------------------------------------
# Pre-run validation
# ---------------------------------------------------------------------------

def _find_codex() -> str:
    """Auto-detect codex executable path."""
    import shutil as _shutil
    # Check common locations
    candidates = [
        Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "codex",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    # Fall back to PATH
    found = _shutil.which("codex")
    if found:
        return found
    return "codex"


def detect_harness_version(config: Config) -> str:
    try:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(config.codex_home)
        r = subprocess.run(
            [config.codex_executable, "--version"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        for line in (r.stdout + r.stderr).splitlines():
            if "codex-cli" in line.lower():
                return line.split()[-1]
        return "unknown"
    except Exception:
        return "unknown"


def validate_base_repo(base_repo: Path) -> str:
    """Verify base-repo is a clean git repo. Returns 40-char SHA.
    FAILS if there are uncommitted changes — never auto-commits."""
    r = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(base_repo), capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"Not a git repository: {base_repo}")

    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(base_repo), capture_output=True, text=True,
    )
    if r.stdout.strip():
        raise SystemExit(
            f"Base repo has uncommitted changes. Please commit or stash first.\n{r.stdout}"
        )

    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(base_repo), capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def verify_parent_commit(workspace: Path, expected_parent: str) -> bool:
    """Verify the parent of HEAD is the expected initial SHA."""
    r = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=str(workspace), capture_output=True, text=True,
    )
    actual = r.stdout.strip()
    ok = (actual == expected_parent)
    if not ok:
        print(f"  [FAIL] Parent commit mismatch! Expected {expected_parent[:8]}, got {actual[:8]}",
              file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# Core: run codex exec and parse JSONL
# ---------------------------------------------------------------------------

def run_codex_exec(prompt, workspace, config):
    env = os.environ.copy()
    env["CODEX_HOME"] = str(config.codex_home)

    cmd = [
        config.codex_executable, "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", str(workspace),
        prompt,
    ]

    if config.verbose:
        print(f"  [CMD] codex exec --json ...")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8", cwd=str(workspace), shell=True,
    )

    thread_id = ""
    jsonl_lines = []
    completed = False
    deadline = time.time() + config.timeout_seconds

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            jsonl_lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not thread_id and event.get("type") == "thread.started":
                thread_id = event.get("thread_id", "")

            if event.get("type") == "turn.completed":
                completed = True

            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, config.timeout_seconds)

        proc.wait(timeout=10)

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise

    stderr_output = proc.stderr.read()
    if stderr_output and config.verbose:
        print(f"  [stderr] {stderr_output[:500]}", file=sys.stderr)

    if not thread_id:
        raise RuntimeError("No thread.started event in JSONL output")
    if not completed:
        print("  [WARN] No turn.completed event - run may be incomplete", file=sys.stderr)

    return thread_id, "\n".join(jsonl_lines)


# ---------------------------------------------------------------------------
# Trace file lookup
# ---------------------------------------------------------------------------

def find_trace_file(codex_home, thread_id):
    sessions_dir = Path(codex_home) / "sessions"
    if not sessions_dir.is_dir():
        return None
    for root, _dirs, files in os.walk(sessions_dir):
        for fname in files:
            if thread_id in fname and fname.endswith(".jsonl"):
                return Path(root) / fname
    return None


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------

def setup_workspace(base_repo, work_dir):
    work_dir = Path(work_dir)
    if work_dir.exists():
        subprocess.run(["git", "clean", "-fdx"], cwd=str(work_dir), capture_output=True)
        shutil.rmtree(work_dir, ignore_errors=True)
        if work_dir.exists():
            _force_rmtree(work_dir)
    shutil.copytree(base_repo, work_dir, symlinks=False)
    subprocess.run(
        ["git", "checkout", "--detach"],
        cwd=str(work_dir), check=True, capture_output=True,
    )


def _force_rmtree(path: Path):
    for item in path.rglob("*"):
        try:
            item.chmod(0o777)
        except Exception:
            pass
    shutil.rmtree(path, ignore_errors=True)


def git_commit_all(workspace, message):
    subprocess.run(["git", "add", "-A"], cwd=str(workspace),
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message, "--allow-empty"],
                   cwd=str(workspace), check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "HEAD"],
                            cwd=str(workspace), check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def git_diff_summary(workspace, parent_sha):
    r = subprocess.run(
        ["git", "diff", "--stat", parent_sha, "HEAD"],
        cwd=str(workspace), capture_output=True, text=True,
    )
    return r.stdout.strip() or "(no changes)"


def git_push(workspace, branch, remote_url):
    try:
        subprocess.run(
            ["git", "push", remote_url, f"HEAD:{branch}"],
            cwd=str(workspace), check=True,
            capture_output=True, text=True, timeout=60,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [WARN] Push failed: {e.stderr.strip()}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

def run_single(label, prompt, workspace, config, output_dir, parent_sha):
    print(f"\n{'='*60}")
    print(f"  Run {label}: {workspace}")
    print(f"{'='*60}")

    t0 = time.time()
    thread_id, jsonl_output = run_codex_exec(prompt, workspace, config)
    elapsed = time.time() - t0
    print(f"  Elapsed: {elapsed:.0f}s  sessionID: {thread_id}")

    jsonl_dst = Path(output_dir) / f"run_{label}_stdout.jsonl"
    jsonl_dst.write_text(jsonl_output, encoding="utf-8")

    trace_src = find_trace_file(config.codex_home, thread_id)
    trace_dst = Path(output_dir) / f"run_{label}_trace.jsonl"
    if trace_src:
        shutil.copy2(trace_src, trace_dst)
        print(f"  Trace: {trace_src.name} ({trace_src.stat().st_size:,} bytes)")
    else:
        print(f"  [WARN] Trace not found for {thread_id}", file=sys.stderr)

    commit_sha = git_commit_all(workspace, f"GSB Run {label} - automated")
    parent_ok = verify_parent_commit(workspace, parent_sha)
    diff_summary = git_diff_summary(workspace, parent_sha)
    print(f"  Commit: {commit_sha}")
    print(f"  Parent verified: {'OK' if parent_ok else 'MISMATCH!'}")
    print(f"  Changes:\n{diff_summary}")

    return RunResult(
        label=label, thread_id=thread_id, session_id=thread_id,
        trace_src=trace_src or Path(), trace_dst=trace_dst,
        commit_sha=commit_sha, workspace=Path(workspace),
        diff_summary=diff_summary, parent_verified=parent_ok, success=True,
    )


# ---------------------------------------------------------------------------
# A/B pair
# ---------------------------------------------------------------------------

def run_pair(entry, config, tmp_dir):
    prompt_id = entry["id"]
    prompt_text = entry["prompt"]
    task_type = entry.get("task_type", "")
    difficulty = entry.get("difficulty", "")
    language = entry.get("language", "")
    env_repro = entry.get("env_reproducibility", "无外部依赖")

    pair_dir = config.output_dir / prompt_id
    pair_dir.mkdir(parents=True, exist_ok=True)

    harness_version = detect_harness_version(config)
    os_name = f"{platform.system()} {platform.release()}"

    result = PairResult(
        prompt_id=prompt_id, prompt=prompt_text,
        task_type=task_type, difficulty=difficulty, language=language,
        env_reproducibility=env_repro,
        initial_sha=config.initial_sha,
        harness="codex cli", harness_version=harness_version, os_name=os_name,
    )

    ws_a = Path(tmp_dir) / f"{prompt_id}_a"
    ws_b = Path(tmp_dir) / f"{prompt_id}_b"

    # Run A
    print(f"\n[Preparing workspace A]")
    setup_workspace(config.base_repo, ws_a)
    try:
        result.run_a = run_single("A", prompt_text, ws_a, config, pair_dir, config.initial_sha)
    except Exception as e:
        result.run_a = RunResult("A", "", "", Path(), Path(), "", ws_a, "", False, False, str(e))
        print(f"  [ERROR] Run A: {e}", file=sys.stderr)

    # Run B
    print(f"\n[Preparing workspace B]")
    setup_workspace(config.base_repo, ws_b)
    try:
        result.run_b = run_single("B", prompt_text, ws_b, config, pair_dir, config.initial_sha)
    except Exception as e:
        result.run_b = RunResult("B", "", "", Path(), Path(), "", ws_b, "", False, False, str(e))
        print(f"  [ERROR] Run B: {e}", file=sys.stderr)

    # Push
    if config.github_remote:
        if result.run_a and result.run_a.success:
            if git_push(ws_a, f"gsb-{prompt_id}-A", config.github_remote):
                print(f"  Pushed A -> gsb-{prompt_id}-A")
        if result.run_b and result.run_b.success:
            if git_push(ws_b, f"gsb-{prompt_id}-B", config.github_remote):
                print(f"  Pushed B -> gsb-{prompt_id}-B")

    # Save manifest
    manifest = _build_manifest(result)
    manifest_path = pair_dir / "result.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Manifest: {manifest_path}")

    return result


def _build_manifest(r: PairResult) -> dict:
    def rr_dict(rr):
        if not rr:
            return None
        return {
            "session_id": rr.session_id,
            "trace_file": str(rr.trace_dst),
            "trace_upload_url": "<< 请上传轨迹文件到对象存储后填写链接 >>",
            "product_commit_sha": rr.commit_sha,
            "parent_verified": rr.parent_verified,
            "diff_summary": rr.diff_summary,
            "screen_recording": "<< 请录制并上传运行录屏，90秒以内 >>",
            "success": rr.success,
            "error": rr.error,
        }

    return {
        # === 题目信息（两次跑共用，只填一次）===
        "user_prompt": r.prompt,
        "task_type": r.task_type,
        "difficulty": r.difficulty,
        "language": r.language,
        "env_reproducibility": r.env_reproducibility,
        # === 环境信息 ===
        "harness": r.harness,
        "harness_version": r.harness_version,
        "os": r.os_name,
        # === 初始环境快照 ===
        "initial_sha": r.initial_sha,
        # === A跑 ===
        "A": rr_dict(r.run_a),
        # === B跑 ===
        "B": rr_dict(r.run_b),
        # === GSB（以下由人工填写）===
        "GSB_conclusion": "<< 人工填写：A更好 / Same / B更好 >>",
        "GSB_reason": "<< 人工填写：三要素（触发节点+实际行为+业务影响）>>",
        "A_delivery_completeness": "<< 人工填写：1-5 >>",
        "A_delivery_completeness_note": "<< 人工填写 >>",
        "B_delivery_completeness": "<< 人工填写：1-5 >>",
        "B_delivery_completeness_note": "<< 人工填写 >>",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GSB Pair-wise Automation Runner")
    parser.add_argument("--base-repo", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", default="./output")
    parser.add_argument("--tmp", default=None)
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--github-remote", default="")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--initial-sha", default="")
    parser.add_argument("--codex-executable", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    base_repo = Path(args.base_repo).resolve()
    prompts_file = Path(args.prompts).resolve()
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(args.tmp).resolve() if args.tmp else Path(tempfile.mkdtemp(prefix="gsb_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if args.codex_home:
        codex_home = Path(args.codex_home).resolve()
    else:
        cli_home = Path.home() / ".codex-cli"
        codex_home = cli_home if cli_home.is_dir() else (Path.home() / ".codex")
    print(f"CODEX_HOME: {codex_home}")

    initial_sha = args.initial_sha or validate_base_repo(base_repo)
    print(f"Initial SHA: {initial_sha}")

    prompts = json.loads(prompts_file.read_text(encoding="utf-8"))
    print(f"Loaded {len(prompts)} prompt(s)")

    config = Config(
        codex_home=codex_home, base_repo=base_repo, output_dir=output_dir,
        initial_sha=initial_sha, github_remote=args.github_remote,
        codex_executable=getattr(args, "codex_executable", "") or _find_codex(),
        timeout_seconds=args.timeout, verbose=args.verbose,
    )

    all_results = []
    for i, entry in enumerate(prompts):
        pid = entry.get("id", f"prompt_{i+1:03d}")
        print(f"\n{'#'*60}")
        print(f"# [{i+1}/{len(prompts)}] {pid}")
        print(f"# {entry.get('task_type', 'N/A')} | {entry.get('difficulty', 'N/A')}")
        print(f"{'#'*60}")
        try:
            all_results.append(run_pair(entry, config, tmp_dir))
        except Exception as e:
            print(f"  [FATAL] {pid}: {e}", file=sys.stderr)

    # Summary
    print(f"\n{'='*60}")
    print("RUN SUMMARY")
    print(f"{'='*60}")
    for r in all_results:
        a_ok = r.run_a and r.run_a.success
        b_ok = r.run_b and r.run_b.success
        a_p = "OK" if (r.run_a and r.run_a.parent_verified) else "PARENT?"
        b_p = "OK" if (r.run_b and r.run_b.parent_verified) else "PARENT?"
        status = "OK" if (a_ok and b_ok) else ("PARTIAL" if (a_ok or b_ok) else "FAIL")
        a_sha = r.run_a.commit_sha[:8] if (r.run_a and r.run_a.commit_sha) else "N/A"
        b_sha = r.run_b.commit_sha[:8] if (r.run_b and r.run_b.commit_sha) else "N/A"
        print(f"  {r.prompt_id}: A={a_sha} [{a_p}] B={b_sha} [{b_p}] -> {status}")

    # Submission checklist
    print(f"\n{'='*60}")
    print("SUBMISSION CHECKLIST")
    print(f"{'='*60}")
    MANUAL = "【人工】"
    for r in all_results:
        print(f"\n  [{r.prompt_id}] {r.task_type} / {r.difficulty}")
        print(f"    User Prompt: {len(r.prompt)} chars")
        print(f"    Harness: {r.harness} v{r.harness_version}  |  OS: {r.os_name}")
        print(f"    Env Reproducibility: {r.env_reproducibility}")
        print(f"    Initial SHA: {r.initial_sha}")
        if r.run_a:
            print(f"    A SessionID: {r.run_a.session_id}")
            print(f"    A Product SHA: {r.run_a.commit_sha}")
            print(f"    A Parent Verified: {'OK' if r.run_a.parent_verified else 'MISMATCH'}")
        if r.run_b:
            print(f"    B SessionID: {r.run_b.session_id}")
            print(f"    B Product SHA: {r.run_b.commit_sha}")
            print(f"    B Parent Verified: {'OK' if r.run_b.parent_verified else 'MISMATCH'}")
        print(f"    {MANUAL} 运行录屏（A/B 各一段，90秒以内）")
        print(f"    {MANUAL} 轨迹文件上传到对象存储")
        print(f"    {MANUAL} GSB结论 + 理由")
        print(f"    {MANUAL} 交付完整性打分（A/B各1-5分+描述）")

    print(f"\nOutput: {output_dir}")
    print("Done.")


if __name__ == "__main__":
    main()

