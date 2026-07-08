#!/usr/bin/env python
"""meta-harness: 자기 자신(하네스)을 격리 환경에서 실행·개선하는 CLI.

이 스크립트는 스킬 `meta-harness` 의 실행 엔진이다. 에이전트는 `execute` 셸로
이 파일의 서브커맨드를 호출해서:

  1. 자기 자신(레포 소스)을 통째로 격리 복사(variant) 하고
  2. 그 복사본을 헤드리스로 주어진 질의에 대해 실행(run)해 전체 실행기록·산출물을 캡처하며
  3. 복사본의 시스템 프롬프트/도구코드/스킬을 수정한 또 다른 variant 를 만들어 다시 실행하고
  4. 두 실행 결과를 비교(compare)한 뒤
  5. 더 나은 쪽을 본체(레포)에 승격(promote) 한다.

격리 원칙
---------
- 모든 작업물(variant 복사본, 격리 워크스페이스, 실행기록)은 레포 '밖' 임시 홈에 둔다.
  → 라이브 `langgraph dev` 의 watchfiles 가 감시하는 레포/워크스페이스를 건드리지 않아
    meta-run 도중 본체가 리로드되지 않는다.
- meta-run 은 기본적으로 메시징 커넥터(Slack/Telegram/Email)를 '끈' 상태로 돈다
  (variant 의 .env 에서 해당 키를 제거). 실수로 실제 메시지를 보내지 않기 위함.
  실제 커넥터까지 포함해 돌리려면 variant 생성 시 --live 를 준다(위험).
- 본체(레포)에 실제로 쓰는 유일한 명령은 `promote` 뿐이다.

deepagents 는 `execute` 셸에 프로세스 격리가 없으므로(virtual_mode 는 파일 도구 경로만
가상화), 이 스크립트는 절대경로로 레포 소스를 자유롭게 복사/실행할 수 있다.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 공통 상수/유틸
# ---------------------------------------------------------------------------

# 하네스 본체를 식별하는 마커 파일(레포 루트 탐지에 사용).
HARNESS_ENTRY = "langchain-deepagents.py"

# variant 복사 시 제외할 무겁거나 런타임/스크래치 성격의 경로.
COPY_IGNORE_DIRS = {
    ".git", ".venv", "venv", "workspace", "__pycache__", ".langgraph_api",
    ".meta", ".mypy_cache", ".ruff_cache", ".pytest_cache", "node_modules",
    ".idea", ".vscode",
}

# meta-run 시 variant .env 에 남길 키(그 외 메시징/시크릿 키는 제거).
# 모델 호출과 웹검색만 살리고, 실수로 실제 메시지가 나가지 않게 한다.
ENV_ALLOWLIST = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "ANTHROPIC_API_KEY",
    "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
    "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_PROJECT",
}

# promote 로 본체에 되돌릴 수 있는 소스 경로(안전 화이트리스트).
# .env / uv.lock / pyproject 등 설정·시크릿은 기본 제외(--include-config 로 포함).
PROMOTABLE_PREFIXES = (
    "langchain-deepagents.py", "connectors.py", "gateway.py",
    "langgraph.json", "workspace_seed/",
)

MAX_ARTIFACT_BYTES = 2_000_000  # 산출물로 복사할 파일 최대 크기


def die(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def find_repo_root() -> Path:
    """cwd 와 이 스크립트 위치에서 위로 올라가며 하네스 본체 레포 루트를 찾는다."""
    candidates = [Path.cwd(), Path(__file__).resolve().parent]
    for start in candidates:
        p = start
        for _ in range(8):
            if (p / HARNESS_ENTRY).is_file() and (p / "langgraph.json").is_file():
                return p.resolve()
            if p.parent == p:
                break
            p = p.parent
    die(f"레포 루트를 찾지 못했습니다({HARNESS_ENTRY} 를 포함한 상위 디렉터리가 없음).")
    raise SystemExit(1)  # for type checkers


def home_dir(repo: Path, override: str | None) -> Path:
    """격리 작업물을 둘 홈 디렉터리(레포 밖)."""
    if override:
        return Path(override).expanduser().resolve()
    env = os.getenv("METAHARNESS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    tag = hashlib.sha1(str(repo).encode()).hexdigest()[:8]
    base = Path(tempfile.gettempdir()) / "agent-meta-harness" / f"{repo.name}-{tag}"
    return base.resolve()


def variants_dir(home: Path) -> Path:
    return home / "variants"


def runs_dir(home: Path) -> Path:
    return home / "runs"


def variant_path(home: Path, name: str) -> Path:
    return variants_dir(home) / name


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _copy_ignore(dirpath: str, names: list[str]) -> set[str]:
    ignored = set()
    for n in names:
        if n in COPY_IGNORE_DIRS or n.endswith(".pyc") or n.endswith(".pyo"):
            ignored.add(n)
    return ignored


def sanitize_env_file(env_path: Path, live: bool) -> None:
    """variant 의 .env 에서 메시징/시크릿 키를 제거한다(live=True 면 그대로 둠).

    WORKSPACE_DIR 도 제거해야 헤드리스가 지정한 격리 워크스페이스가 우선된다.
    """
    if not env_path.exists():
        return
    if live:
        # 실 커넥터까지 살리되, WORKSPACE_DIR 만은 반드시 제거(격리 워크스페이스 우선).
        keep = None
    else:
        keep = ENV_ALLOWLIST
    out_lines: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == "WORKSPACE_DIR":
            continue  # 항상 제거
        if keep is None or key in keep:
            out_lines.append(line)
        # 그 외 키는 드롭
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# variant 관리
# ---------------------------------------------------------------------------

def make_variant(repo: Path, home: Path, name: str, src: Path | None, live: bool) -> Path:
    """레포(또는 다른 variant)를 격리 복사해 새 variant 를 만든다."""
    dst = variant_path(home, name)
    source = src if src is not None else repo
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dst, ignore=_copy_ignore, symlinks=False)
    # variant 자신이 만든 _ws / runs 흔적은 제거(다른 variant 로부터 fork 한 경우).
    for junk in ("_ws", "_runs"):
        jp = dst / junk
        if jp.exists():
            shutil.rmtree(jp, ignore_errors=True)
    sanitize_env_file(dst / ".env", live=live)
    return dst


# ---------------------------------------------------------------------------
# 헤드리스 실행(내부 서브커맨드 __headless 가 실제 에이전트를 돌린다)
# ---------------------------------------------------------------------------

def _serialize_message(msg) -> dict:
    """langchain 메시지를 JSON 직렬화 가능한 dict 로 변환(안전하게)."""
    out: dict = {"type": type(msg).__name__}
    role = getattr(msg, "type", None)
    if role:
        out["role"] = role
    content = getattr(msg, "content", None)
    out["content"] = content
    for attr in ("name", "tool_call_id"):
        v = getattr(msg, attr, None)
        if v:
            out[attr] = v
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        out["tool_calls"] = [
            {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
            if isinstance(tc, dict) else {"name": getattr(tc, "name", None)}
            for tc in tcs
        ]
    usage = getattr(msg, "usage_metadata", None)
    if usage:
        out["usage"] = dict(usage)
    return out


def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(blk.get("text") or blk.get("content") or json.dumps(blk, ensure_ascii=False))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    return str(content)


def _render_transcript(messages: list, meta: dict) -> str:
    lines = [f"# meta-run transcript — variant `{meta.get('variant')}`", ""]
    lines.append(f"- query: {meta.get('query')!r}")
    lines.append(f"- duration: {meta.get('duration_s')}s")
    lines.append(f"- messages: {len(messages)}  tool_calls: {meta.get('num_tool_calls')}")
    if meta.get("error"):
        lines.append(f"- ERROR: {meta['error']}")
    lines.append("")
    lines.append("---")
    for i, m in enumerate(messages):
        role = m.get("role") or m.get("type")
        header = f"\n## [{i}] {role}"
        if m.get("name"):
            header += f" ({m['name']})"
        lines.append(header)
        text = _text_of_content(m.get("content"))
        if text and text.strip():
            lines.append(text.strip())
        for tc in m.get("tool_calls") or []:
            args = json.dumps(tc.get("args"), ensure_ascii=False)
            if len(args) > 1500:
                args = args[:1500] + " …(truncated)"
            lines.append(f"\n**→ tool_call `{tc.get('name')}`**\n```json\n{args}\n```")
    return "\n".join(lines) + "\n"


def _snapshot(ws: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not ws.exists():
        return snap
    for p in ws.rglob("*"):
        if p.is_file():
            try:
                snap[str(p.relative_to(ws))] = _sha1(p.read_bytes())
            except Exception:
                snap[str(p.relative_to(ws))] = "?"
    return snap


def cmd_headless(args: argparse.Namespace) -> int:
    """[내부] variant 하네스를 import 해 질의를 헤드리스로 실행하고 결과를 out-dir 에 덤프."""
    harness_file = Path(args.harness_file).resolve()
    variant_root = harness_file.parent
    ws = Path(args.workspace).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "artifacts").mkdir(exist_ok=True)

    query = Path(args.query_file).read_text(encoding="utf-8") if args.query_file else args.query

    # 격리 워크스페이스를 매 실행 새로 만든다(결정성). 하네스 import 가 seed 를 채운다.
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)
    os.environ["WORKSPACE_DIR"] = str(ws)

    # variant 소스를 우선 import 경로로. cwd 는 이미 variant_root(부모가 설정).
    sys.path.insert(0, str(variant_root))

    result: dict = {
        "variant": args.variant,
        "query": query,
        "error": None,
    }
    messages_dicts: list[dict] = []
    t0 = time.time()
    try:
        spec = importlib.util.spec_from_file_location("harness_under_test", str(harness_file))
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        agent = getattr(mod, "agent", None) or mod.build_agent()

        # seed 직후(=하네스 import 완료) 워크스페이스 스냅샷 → 산출물 diff 기준.
        pre = _snapshot(ws)

        final_state = None
        config = {"recursion_limit": args.recursion_limit}
        payload = {"messages": [{"role": "user", "content": query}]}
        # 자식이 스스로 시간예산을 지킨다: 매 super-step(스트림 청크) 사이에 마감을
        # 확인하고, 넘으면 지금까지의 상태로 '부분 캡처'를 남긴 채 정상 종료한다.
        # → 부모가 SIGKILL 로 죽여 기록이 통째로 유실되는 일을 막는다(meta-harness 는
        #   실패해도 실행기록을 보존해야 한다). 부모 timeout 은 이보다 큰 backstop.
        deadline = t0 + args.deadline_s if args.deadline_s and args.deadline_s > 0 else None
        for chunk in agent.stream(payload, config=config, stream_mode="values"):
            final_state = chunk
            if deadline and time.time() > deadline:
                result["error"] = f"deadline exceeded after {args.deadline_s}s (partial capture)"
                break
        msgs = (final_state or {}).get("messages", []) if isinstance(final_state, dict) else []
        messages_dicts = [_serialize_message(m) for m in msgs]
    except Exception as e:  # noqa: BLE001
        import traceback
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
        pre = pre if "pre" in dir() else {}  # type: ignore
    duration = round(time.time() - t0, 1)

    # 산출물 diff
    post = _snapshot(ws)
    changed = [k for k, v in post.items() if pre.get(k) != v]
    artifacts = []
    for rel in sorted(changed):
        srcf = ws / rel
        try:
            if srcf.stat().st_size > MAX_ARTIFACT_BYTES:
                artifacts.append({"path": rel, "note": "skipped(too large)"})
                continue
            dstf = out / "artifacts" / rel
            dstf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, dstf)
            artifacts.append({"path": rel, "bytes": srcf.stat().st_size})
        except Exception as e:  # noqa: BLE001
            artifacts.append({"path": rel, "note": f"copy failed: {e}"})

    # 메트릭 집계
    tool_calls: dict[str, int] = {}
    tokens_in = tokens_out = 0
    final_answer = ""
    for m in messages_dicts:
        for tc in m.get("tool_calls") or []:
            tool_calls[tc.get("name") or "?"] = tool_calls.get(tc.get("name") or "?", 0) + 1
        u = m.get("usage") or {}
        tokens_in += int(u.get("input_tokens") or 0)
        tokens_out += int(u.get("output_tokens") or 0)
        if (m.get("role") in ("ai", "AIMessage") or m.get("type") == "AIMessage"):
            t = _text_of_content(m.get("content"))
            if t and t.strip():
                final_answer = t.strip()

    summary = {
        "variant": args.variant,
        "query": query,
        "duration_s": duration,
        "num_messages": len(messages_dicts),
        "num_tool_calls": sum(tool_calls.values()),
        "tool_calls_by_name": tool_calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "num_artifacts": len([a for a in artifacts if "bytes" in a]),
        "artifacts": artifacts,
        "final_answer_chars": len(final_answer),
        "error": result["error"],
        "partial": bool(result["error"] and "partial capture" in result["error"]),
    }

    (out / "messages.json").write_text(
        json.dumps(messages_dicts, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "final_answer.md").write_text(final_answer or "(no final answer)", encoding="utf-8")
    (out / "transcript.md").write_text(
        _render_transcript(messages_dicts, {**summary}), encoding="utf-8"
    )
    if result.get("traceback"):
        (out / "error.txt").write_text(result["traceback"], encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not result["error"] else 2


# ---------------------------------------------------------------------------
# 상위 서브커맨드
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    report = {
        "repo_root": str(repo),
        "harness_entry": (repo / HARNESS_ENTRY).is_file(),
        "home": str(home),
        "python": sys.executable,
    }
    try:
        import deepagents  # noqa: F401
        report["deepagents"] = getattr(deepagents, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        report["deepagents"] = f"IMPORT FAILED: {e}"
    env = {}
    envf = repo / ".env"
    present = set()
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                present.add(line.split("=", 1)[0].strip())
    env["OPENAI_API_KEY"] = "OPENAI_API_KEY" in present or bool(os.getenv("OPENAI_API_KEY"))
    env["TAVILY_API_KEY"] = "TAVILY_API_KEY" in present or bool(os.getenv("TAVILY_API_KEY"))
    report["env_keys"] = env
    # git 상태
    try:
        st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
        report["git_clean"] = (st.returncode == 0 and not st.stdout.strip())
    except Exception:
        report["git_clean"] = None
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = report["harness_entry"] and str(report.get("deepagents", "")).count("FAILED") == 0 and env["OPENAI_API_KEY"]
    if not ok:
        print("\n[doctor] 경고: 위 항목 중 실패가 있습니다. 실행 전에 해결하세요.", file=sys.stderr)
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    home.mkdir(parents=True, exist_ok=True)
    variants_dir(home).mkdir(exist_ok=True)
    runs_dir(home).mkdir(exist_ok=True)
    dst = make_variant(repo, home, "baseline", src=None, live=args.live)
    print(json.dumps({
        "home": str(home),
        "baseline_variant": str(dst),
        "live_connectors": bool(args.live),
        "note": "baseline 은 본체의 격리 복사본입니다. run 으로 실행하세요.",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    src = variant_path(home, args.src)
    if not src.exists():
        die(f"원본 variant '{args.src}' 가 없습니다. 먼저 init 하세요.")
    dst = make_variant(repo, home, args.name, src=src, live=args.live)
    print(json.dumps({
        "forked_from": args.src,
        "new_variant": args.name,
        "path": str(dst),
        "editable_knobs": {
            "system_prompt": f"{HARNESS_ENTRY} 의 SYSTEM_PROMPT",
            "tools": f"connectors.py, {HARNESS_ENTRY} 의 _web_search_tools/_mcp_tools",
            "skills": "workspace_seed/skills/<name>/SKILL.md",
        },
        "hint": (
            "variant 는 원본과 동일한 복사본입니다. 개선은 '바꿀 부분만' 최소로 하세요: "
            f"`edit --variant {args.name} --file <경로> --find <원본조각> --replace <새조각>`. "
            f"원본을 통째로 날리지 마세요. 수정 후 `diff --a {args.src} --b {args.name}` 로 "
            "변경이 최소한인지 확인하고 run 하세요."
        ),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_set_prompt(args: argparse.Namespace) -> int:
    """variant 의 SYSTEM_PROMPT 삼중따옴표 블록을 통째로 교체(편의 명령)."""
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    vp = variant_path(home, args.variant)
    entry = vp / HARNESS_ENTRY
    if not entry.exists():
        die(f"variant '{args.variant}' 에 {HARNESS_ENTRY} 가 없습니다.")
    new_prompt = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    text = entry.read_text(encoding="utf-8")
    marker = "SYSTEM_PROMPT = \"\"\""
    start = text.find(marker)
    if start == -1:
        die("SYSTEM_PROMPT 블록을 찾지 못했습니다.")
    body_start = start + len(marker)
    end = text.find("\"\"\"", body_start)
    if end == -1:
        die("SYSTEM_PROMPT 종료 삼중따옴표를 찾지 못했습니다.")
    old_body = text[body_start:end]
    # 새 프롬프트 안의 삼중따옴표는 이스케이프.
    safe = new_prompt.replace("\"\"\"", "\\\"\\\"\\\"")
    new_text = text[:body_start] + safe + text[end:]
    entry.write_text(new_text, encoding="utf-8")
    result = {"variant": args.variant, "updated": "SYSTEM_PROMPT",
              "old_chars": len(old_body), "new_chars": len(new_prompt)}
    # set-prompt 는 프롬프트를 '통째로' 교체한다. 새 프롬프트가 원본보다 크게 짧으면
    # 원본의 좋은 지침을 의도치 않게 날렸을 가능성이 높다 → 경고(강제 아님).
    # 원본을 유지한 채 국소 수정을 원하면 `edit`(find/replace)를 쓰는 게 맞다.
    if old_body and len(new_prompt) < 0.6 * len(old_body):
        result["warning"] = (
            f"새 프롬프트가 원본({len(old_body)}자)보다 크게 짧습니다({len(new_prompt)}자). "
            "원본 지침을 통째로 날린 게 아닌지 확인하세요. 최소 변경만 원하면 `edit` 서브커맨드로 "
            "특정 부분만 find/replace 하세요."
        )
        print(f"[set-prompt] 경고: {result['warning']}", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _resolve_variant_file(vp: Path, rel: str) -> Path:
    """variant 내부 경로만 허용(밖으로 탈출 방지)."""
    target = (vp / rel).resolve()
    if not str(target).startswith(str(vp.resolve())):
        die(f"variant 밖 경로는 수정할 수 없습니다: {rel}")
    if not target.is_file():
        die(f"파일이 없습니다: {rel} (variant '{vp.name}')")
    return target


def cmd_edit(args: argparse.Namespace) -> int:
    """variant 파일의 특정 부분만 정확히 find→replace 한다(최소 변경 실험용).

    variant 는 원본과 동일한 복사본이므로, 이 명령으로 '바꿀 부분만' 국소 수정하면
    원본 나머지는 그대로 보존된다. find 가 유일하게 매칭돼야 안전하다(--count 로 조정).
    find/replace 는 인라인(--find/--replace) 또는 파일(--find-file/--replace-file)로 준다.
    replace 를 비우면(생략 또는 빈 문자열) 해당 부분을 삭제한다.
    """
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    vp = variant_path(home, args.variant)
    if not vp.exists():
        die(f"variant '{args.variant}' 가 없습니다.")
    target = _resolve_variant_file(vp, args.file)

    if args.find_file:
        find = Path(args.find_file).read_text(encoding="utf-8")
    elif args.find is not None:
        find = args.find
    else:
        die("--find 또는 --find-file 이 필요합니다.")
    if args.replace_file:
        replace = Path(args.replace_file).read_text(encoding="utf-8")
    elif args.replace is not None:
        replace = args.replace
    else:
        replace = ""  # 생략 시 삭제

    if not find:
        die("find 문자열이 비어 있습니다.")
    text = target.read_text(encoding="utf-8")
    n = text.count(find)
    if n == 0:
        die("find 문자열을 찾지 못했습니다. 원본 텍스트와 정확히(공백·들여쓰기 포함) 일치해야 합니다.")
    if n != args.count:
        die(f"find 가 {n}회 나타났습니다(기대 {args.count}회). 더 구체적으로 지정하거나 "
            f"--count {n} 로 맞추세요.")
    new_text = text.replace(find, replace)
    target.write_text(new_text, encoding="utf-8")
    print(json.dumps({
        "variant": args.variant,
        "file": args.file,
        "occurrences_replaced": n,
        "delta_chars": len(new_text) - len(text),
        "hint": f"diff --a baseline --b {args.variant} 로 변경이 최소한인지 확인하세요.",
    }, ensure_ascii=False))
    return 0


# 자식의 graceful 마감(--deadline-s)을 부모 SIGKILL 보다 이만큼 먼저 둔다.
# 자식이 스스로 부분 캡처를 flush 할 시간을 확보하기 위한 여유.
_BACKSTOP_MARGIN_S = 60


def _run_headless_subprocess(repo: Path, variant: Path, ws: Path, out: Path,
                             query_file: Path, recursion_limit: int, timeout: int) -> tuple[int, str]:
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "__headless",
        "--variant", variant.name,
        "--harness-file", str(variant / HARNESS_ENTRY),
        "--workspace", str(ws),
        "--query-file", str(query_file),
        "--out-dir", str(out),
        "--recursion-limit", str(recursion_limit),
        # 자식은 timeout 초에서 스스로 멈추고 부분 캡처를 남긴다.
        "--deadline-s", str(timeout),
    ]
    # 부모 env 를 물려주되 WORKSPACE_DIR 는 헤드리스가 직접 설정하므로 제거.
    env = {k: v for k, v in os.environ.items() if k != "WORKSPACE_DIR"}
    try:
        # 부모 timeout 은 자식 마감 + 여유. 자식이 정상적으로 부분 캡처를 남기면
        # 여기까지 오지 않고, 자식이 단일 스텝에 멈춰버린 경우에만 SIGKILL 백스톱.
        proc = subprocess.run(cmd, cwd=str(variant), env=env, capture_output=True,
                              text=True, timeout=timeout + _BACKSTOP_MARGIN_S)
        return proc.returncode, (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired:
        return 124, (f"TIMEOUT: 자식이 마감({timeout}s)+백스톱({_BACKSTOP_MARGIN_S}s) 내에 "
                     "부분 캡처조차 남기지 못했습니다(단일 스텝에서 멈춤 가능). run.log 확인.")


def cmd_run(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    # baseline 이 없으면 자동 init.
    if not variants_dir(home).exists() or not variant_path(home, "baseline").exists():
        home.mkdir(parents=True, exist_ok=True)
        variants_dir(home).mkdir(exist_ok=True)
        runs_dir(home).mkdir(exist_ok=True)
        make_variant(repo, home, "baseline", src=None, live=False)
    vp = variant_path(home, args.variant)
    if not vp.exists():
        die(f"variant '{args.variant}' 가 없습니다. init/fork 로 먼저 만드세요.")

    out = runs_dir(home) / args.variant
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    ws = out / "_ws"

    # 질의 파일 준비
    if args.query_file:
        qf = Path(args.query_file).resolve()
    else:
        if not args.query:
            die("--query 또는 --query-file 이 필요합니다.")
        qf = out / "query.txt"
        qf.write_text(args.query, encoding="utf-8")

    print(f"[run] variant={args.variant}  timeout={args.timeout}s  recursion_limit={args.recursion_limit}")
    print(f"[run] 격리 워크스페이스: {ws}")
    rc, log = _run_headless_subprocess(repo, vp, ws, out, qf, args.recursion_limit, args.timeout)
    (out / "run.log").write_text(log, encoding="utf-8")

    summ_path = out / "summary.json"
    if summ_path.exists():
        summary = json.loads(summ_path.read_text(encoding="utf-8"))
        print("\n=== summary ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"\n산출물/기록 경로: {out}")
        print(f"  - 실행기록:   {out/'transcript.md'}")
        print(f"  - 최종답변:   {out/'final_answer.md'}")
        print(f"  - 산출물:     {out/'artifacts'}/")
        return 0 if not summary.get("error") else 2
    print("\n[run] 실패: summary.json 이 생성되지 않았습니다. run.log 를 확인하세요:")
    print(log[-2000:])
    return rc or 1


def _load_summary(home: Path, variant: str) -> dict | None:
    p = runs_dir(home) / variant / "summary.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _source_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        parts = set(p.relative_to(root).parts)
        if parts & COPY_IGNORE_DIRS or p.name.endswith((".pyc", ".pyo")):
            continue
        if p.name == ".env":  # 시크릿 diff 노출 방지
            continue
        try:
            files[str(p.relative_to(root))] = p.read_bytes()
        except Exception:
            pass
    return files


def _diff_sources(a_root: Path, b_root: Path) -> list[str]:
    a = _source_files(a_root)
    b = _source_files(b_root)
    out: list[str] = []
    for rel in sorted(set(a) | set(b)):
        av = a.get(rel)
        bv = b.get(rel)
        if av == bv:
            continue
        try:
            at = (av or b"").decode("utf-8").splitlines(keepends=True)
            bt = (bv or b"").decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            out.append(f"# binary differs: {rel}")
            continue
        out.extend(difflib.unified_diff(at, bt, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return out


def cmd_diff(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)

    def resolve(name: str) -> Path:
        return repo if name == "REPO" else variant_path(home, name)

    a_root, b_root = resolve(args.a), resolve(args.b)
    if not a_root.exists():
        die(f"'{args.a}' 경로 없음: {a_root}")
    if not b_root.exists():
        die(f"'{args.b}' 경로 없음: {b_root}")
    lines = _diff_sources(a_root, b_root)
    if not lines:
        print(f"(소스 동일: {args.a} == {args.b})")
    else:
        sys.stdout.write("".join(lines))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    sa, sb = _load_summary(home, args.a), _load_summary(home, args.b)
    if not sa:
        die(f"'{args.a}' 실행 결과(summary.json)가 없습니다. 먼저 run 하세요.")
    if not sb:
        die(f"'{args.b}' 실행 결과(summary.json)가 없습니다. 먼저 run 하세요.")
    keys = ["duration_s", "num_messages", "num_tool_calls", "tokens_total",
            "num_artifacts", "final_answer_chars", "error"]
    print(f"{'metric':<22}{args.a:<22}{args.b:<22}")
    print("-" * 66)
    for k in keys:
        print(f"{k:<22}{str(sa.get(k)):<22}{str(sb.get(k)):<22}")
    print(f"\ntool_calls[{args.a}]: {json.dumps(sa.get('tool_calls_by_name', {}), ensure_ascii=False)}")
    print(f"tool_calls[{args.b}]: {json.dumps(sb.get('tool_calls_by_name', {}), ensure_ascii=False)}")
    # 소스 변경 요약
    va, vb = variant_path(home, args.a), variant_path(home, args.b)
    if va.exists() and vb.exists():
        changed = set()
        af, bf = _source_files(va), _source_files(vb)
        for rel in set(af) | set(bf):
            if af.get(rel) != bf.get(rel):
                changed.add(rel)
        print(f"\n두 variant 의 소스 차이 파일: {sorted(changed) or '(없음)'}")
    print("\n실행기록 비교 경로:")
    print(f"  {runs_dir(home)/args.a/'transcript.md'}")
    print(f"  {runs_dir(home)/args.b/'transcript.md'}")
    print("\n※ 정량 지표는 참고용입니다. 최종 우열은 질의 A 의 성공기준에 비추어 판단하세요.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    base = runs_dir(home) / args.variant
    fname = {
        "transcript": "transcript.md", "answer": "final_answer.md",
        "summary": "summary.json", "log": "run.log", "error": "error.txt",
    }.get(args.what, args.what)
    p = base / fname
    if not p.exists():
        die(f"파일 없음: {p}")
    sys.stdout.write(p.read_text(encoding="utf-8"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    out = {"home": str(home), "variants": [], "runs": []}
    if variants_dir(home).exists():
        out["variants"] = sorted(p.name for p in variants_dir(home).iterdir() if p.is_dir())
    if runs_dir(home).exists():
        for p in sorted(runs_dir(home).iterdir()):
            if p.is_dir():
                s = _load_summary(home, p.name)
                out["runs"].append({"variant": p.name,
                                    "summary": {k: s.get(k) for k in ("duration_s", "num_tool_calls", "tokens_total", "num_artifacts", "error")} if s else None})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    vp = variant_path(home, args.variant)
    if not vp.exists():
        die(f"variant '{args.variant}' 가 없습니다.")

    # 승격 대상: variant 와 본체가 다른 promotable 소스 파일.
    vf = _source_files(vp)
    rf = _source_files(repo)
    changed: list[str] = []
    for rel in sorted(set(vf) | set(rf)):
        if not (rel.startswith(PROMOTABLE_PREFIXES) or rel in PROMOTABLE_PREFIXES):
            if not args.include_config:
                continue
        if vf.get(rel) != rf.get(rel):
            changed.append(rel)
    if not changed:
        print("승격할 변경사항이 없습니다(variant 와 본체 소스가 동일).")
        return 0

    # git 안전장치
    try:
        st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10)
        dirty = bool(st.stdout.strip())
    except Exception:
        dirty = None

    print(f"[promote] variant '{args.variant}' → 본체({repo})")
    print(f"[promote] 변경될 파일: {changed}")
    if dirty:
        print("[promote] ⚠️ 워킹트리에 커밋되지 않은 변경이 있습니다. 승격 전 커밋/스태시 권장(롤백 안전망).")
    elif dirty is False:
        print("[promote] git 워킹트리 clean — 승격 후 `git diff` 로 확인하고, 되돌리려면 `git checkout -- <file>`.")

    if not args.yes:
        print("\n[promote] 실제 적용하려면 --yes 를 붙여 다시 실행하세요(본체가 수정됩니다).")
        # 어떤 diff 인지 보여준다.
        for rel in changed:
            try:
                at = (rf.get(rel) or b"").decode("utf-8").splitlines(keepends=True)
                bt = (vf.get(rel) or b"").decode("utf-8").splitlines(keepends=True)
                sys.stdout.write("".join(difflib.unified_diff(at, bt, fromfile=f"본체/{rel}", tofile=f"variant/{rel}")))
            except UnicodeDecodeError:
                print(f"(binary) {rel}")
        return 0

    for rel in changed:
        srcf = vp / rel
        dstf = repo / rel
        dstf.parent.mkdir(parents=True, exist_ok=True)
        if srcf.exists():
            shutil.copy2(srcf, dstf)
        else:
            # variant 에서 삭제된 파일: 본체도 삭제할지는 보수적으로 건너뜀
            print(f"[promote] (건너뜀) variant 에 없는 파일: {rel}")
    print(json.dumps({"promoted": changed, "repo": str(repo)}, ensure_ascii=False, indent=2))
    print("[promote] 완료. langgraph dev 가 실행 중이면 파일 변경으로 자동 리로드됩니다.")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    repo = find_repo_root()
    home = home_dir(repo, args.home)
    if args.variant:
        for d in (variant_path(home, args.variant), runs_dir(home) / args.variant):
            if d.exists():
                shutil.rmtree(d)
        print(f"삭제: variant/run '{args.variant}'")
    elif args.all:
        if home.exists():
            shutil.rmtree(home)
        print(f"삭제: 전체 홈 {home}")
    else:
        die("--all 또는 --variant 를 지정하세요.")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="meta-harness: 자기 자신을 격리 실행·개선한다.")
    p.add_argument("--home", help="격리 작업물 홈(기본: 임시디렉터리/agent-meta-harness/<repo>).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="환경 점검(레포/파이썬/키/deepagents/git).")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("init", help="홈과 baseline(본체 격리복사) 생성.")
    sp.add_argument("--live", action="store_true", help="메시징 커넥터까지 살림(위험).")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("fork", help="기존 variant 를 복사해 새 variant 생성.")
    sp.add_argument("--from", dest="src", default="baseline", help="원본 variant(기본 baseline).")
    sp.add_argument("--name", required=True, help="새 variant 이름.")
    sp.add_argument("--live", action="store_true", help="메시징 커넥터까지 살림(위험).")
    sp.set_defaults(func=cmd_fork)

    sp = sub.add_parser("edit", help="variant 파일의 특정 부분만 find→replace(최소 변경 실험).")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--file", required=True, help="variant 내 상대경로(예: langchain-deepagents.py).")
    sp.add_argument("--find", help="찾을 텍스트(인라인).")
    sp.add_argument("--find-file", help="찾을 텍스트 파일(멀티라인).")
    sp.add_argument("--replace", help="바꿀 텍스트(인라인). 생략/빈값이면 삭제.")
    sp.add_argument("--replace-file", help="바꿀 텍스트 파일(멀티라인).")
    sp.add_argument("--count", type=int, default=1, help="기대 매칭 횟수(기본 1, 유일 매칭 강제).")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("set-prompt",
                        help="variant 의 SYSTEM_PROMPT 블록을 통째로 교체(전면 재작성 시에만).")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--file", help="새 프롬프트 파일(없으면 stdin).")
    sp.set_defaults(func=cmd_set_prompt)

    sp = sub.add_parser("run", help="variant 를 질의에 대해 헤드리스 실행·캡처.")
    sp.add_argument("--variant", default="baseline")
    sp.add_argument("--query", help="질의 텍스트.")
    sp.add_argument("--query-file", help="질의 파일 경로.")
    sp.add_argument("--timeout", type=int, default=900, help="실행 타임아웃 초(기본 900).")
    sp.add_argument("--recursion-limit", type=int, default=60, help="LangGraph recursion_limit.")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("compare", help="두 variant 의 실행 요약을 나란히 비교.")
    sp.add_argument("--a", required=True)
    sp.add_argument("--b", required=True)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("diff", help="두 대상(variant 이름 또는 REPO)의 소스 unified diff.")
    sp.add_argument("--a", required=True)
    sp.add_argument("--b", required=True)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("show", help="실행 산출물 출력(transcript/answer/summary/log).")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--what", default="transcript",
                    help="transcript|answer|summary|log|error 또는 파일명.")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("list", help="variant/run 목록과 요약.")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("promote", help="variant 소스 변경을 본체(레포)에 반영.")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--yes", action="store_true", help="실제 적용(없으면 diff 미리보기만).")
    sp.add_argument("--include-config", action="store_true",
                    help="pyproject/uv.lock/mcp 등 설정 파일도 승격 대상에 포함.")
    sp.set_defaults(func=cmd_promote)

    sp = sub.add_parser("clean", help="격리 작업물 삭제.")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--variant")
    sp.set_defaults(func=cmd_clean)

    # 내부 전용(에이전트가 직접 호출하지 않음): 실제 헤드리스 실행기.
    sp = sub.add_parser("__headless")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--harness-file", required=True)
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--query")
    sp.add_argument("--query-file")
    sp.add_argument("--out-dir", required=True)
    sp.add_argument("--recursion-limit", type=int, default=60)
    sp.add_argument("--deadline-s", type=int, default=0,
                    help="자식 자체 시간예산(초). 넘으면 부분 캡처 후 정상 종료. 0=무제한.")
    sp.set_defaults(func=cmd_headless)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
