"""범용 업무 처리 Deep Agent (cowork 스타일).

- 실제 파일시스템 조작(ls/read_file/write_file/edit_file/glob/grep)과
  셸 명령 실행(execute), 계획 수립(write_todos)을 기본 제공한다.
- 다양한 커넥터(웹 검색, MCP 서버 등)를 `build_connector_tools()`에서 조립해 붙인다.

이 파일을 직접 실행하면 LangGraph Studio(deep agent UI)가 뜬다.
langgraph.json 이 아래 `agent` 그래프를 참조한다.

실행 환경에 따라 자동으로 동작이 달라진다:
- 로컬: `langgraph dev` 로 서버를 띄우고 브라우저로 Studio 를 연다.
- 원격(Codespaces/devcontainer/Gitpod): 로컬 브라우저가 없고 기본 포트포워딩이
  Studio 에서 안 붙으므로, `--tunnel`(Cloudflare) 로 공개 URL 을 만들어 Studio 가
  그 URL 로 직접 연결하게 한다. LANGGRAPH_TUNNEL=0/1 로 강제 off/on 가능.
"""

import json
import os
from pathlib import Path

import dotenv
import httpx
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import LocalShellBackend

# get_model_* 는 deepagents 가 프로필 매칭에 쓰는 내부 헬퍼다. 프로필 등록 키를
# 프레임워크와 동일하게 계산하기 위해 그대로 가져다 쓴다.
from deepagents._models import get_model_identifier, get_model_provider
from langchain.chat_models import init_chat_model

# 로컬 모듈: 외부 서비스 커넥터(Slack / Telegram / Email)
from connectors import build_messaging_tools

# ---------------------------------------------------------------------------
# 환경변수 & 모델
# ---------------------------------------------------------------------------
dotenv.load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
base_url = "https://openrouter.ai/api/v1"

if not api_key:
    raise ValueError("OPENAI_API_KEY 환경변수가 필요합니다. .env를 확인하세요.")

# 사내/기관 TLS 검사 프록시가 자체 서명 CA로 HTTPS를 가로채는 환경에서는
# SSL 인증서 검증이 실패한다. 로컬 테스트용으로 검증을 비활성화한 http_client를 사용한다.
# 주의: verify=False는 중간자 공격에 취약하므로 운영 환경에서는 사용하지 말 것.
# langgraph dev 는 async 로 돌기 때문에 토큰 스트리밍(astream)은 async 클라이언트가 필요하다.
# sync/async 둘 다 verify=False 로 넘겨야 --allow-blocking 없이도 스트리밍 경로가 살아난다.
insecure_http_client = httpx.Client(verify=False)
insecure_http_async_client = httpx.AsyncClient(verify=False)

# OpenRouter는 OpenAI 호환 API를 제공하므로 model_provider를 openai로 설정합니다.
model = init_chat_model(
    model="openai/gpt-5-mini",
    model_provider="openai",
    api_key=api_key,
    base_url=base_url,
    streaming=True,
    # gpt-5 계열은 추론 시간이 길어 사소한 질문도 수십 초 걸린다. low 로 두면 호출당
    # 지연이 크게 줄어 메신저 응답이 빨라진다. 복잡한 추론이 필요하면 medium/high 로.
    reasoning_effort="low",
    http_client=insecure_http_client,
    http_async_client=insecure_http_async_client,
)

# ---------------------------------------------------------------------------
# 작업 공간(파일시스템 백엔드)
# ---------------------------------------------------------------------------
# 에이전트의 파일/셸 작업은 이 디렉터리 안으로 제한된다.
# WORKSPACE_DIR 환경변수로 바꿀 수 있다.
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "workspace")).expanduser().resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# 스킬 디렉터리(workspace/skills). 각 스킬은 SKILL.md(+ 스크립트)를 가진 하위 폴더다.
# 프로젝트의 example_skills/ 를 workspace/skills 로 매 실행 시 동기화한다.
# → 예시 스킬을 업데이트하면 자동 반영된다. 커스텀 스킬은 example_skills 밖의
#   다른 이름으로 workspace/skills 에 만들면 이 동기화의 영향을 받지 않는다.
#
# 주의: shutil.copytree 는 내용이 같아도 매번 파일을 다시 써 mtime 을 바꾼다.
# langgraph dev 는 workspace/ 를 감시(watchfiles)하므로, 그럴 경우 import→동기화→
# 파일 변경 감지→리로드→다시 동기화 의 '무한 리로드 루프'에 빠진다. 그래서 내용이
# 실제로 달라진 파일만 쓰는 idempotent 동기화를 사용한다(동일하면 건드리지 않음).
def _sync_tree(src: Path, dst: Path) -> None:
    """src 트리를 dst 로 복사하되, 내용이 바뀐 파일만 실제로 쓴다."""
    for _p in src.rglob("*"):
        _rel = _p.relative_to(src)
        _target = dst / _rel
        if _p.is_dir():
            _target.mkdir(parents=True, exist_ok=True)
            continue
        _data = _p.read_bytes()
        # 내용이 같으면 건너뛴다(파일을 건드리지 않아 리로드가 유발되지 않음).
        if _target.exists() and _target.read_bytes() == _data:
            continue
        _target.parent.mkdir(parents=True, exist_ok=True)
        _target.write_bytes(_data)


_skills_dir = WORKSPACE / "skills"
_skills_dir.mkdir(parents=True, exist_ok=True)
_example_skills = Path("example_skills")
if _example_skills.is_dir():
    for _src in _example_skills.iterdir():
        if _src.is_dir():
            _sync_tree(_src, _skills_dir / _src.name)

# 이메일 트리거 규칙 파일(workspace/email_triggers.json). 없으면 빈 배열로 만들어
# 두어(스킬 set-email-triggers 로 CRUD) 위치를 발견하기 쉽게 한다.
_triggers = WORKSPACE / "email_triggers.json"
if not _triggers.exists():
    _triggers.write_text("[]\n", encoding="utf-8")

# 메모리 파일(workspace/AGENTS.md). 에이전트가 사용자의 선호·피드백·역할 등을
# edit_file 로 이 파일에 스스로 기록하고, 다음 세션에 자동으로 불러온다.
# edit_file 은 기존 파일을 대상으로 하므로, 없으면 최소 템플릿으로 미리 만들어 둔다.
_agents_md = WORKSPACE / "AGENTS.md"
if not _agents_md.exists():
    _agents_md.write_text(
        "# Agent Memory\n\n"
        "이 파일은 에이전트가 사용자에 대해 학습한 내용을 기록하는 장기 메모리다.\n"
        "(선호, 반복되는 피드백, 역할 정의, 도구 사용에 필요한 정보 등)\n\n"
        "## User\n\n## Preferences\n\n## Notes\n",
        encoding="utf-8",
    )

# LocalShellBackend = 실제 파일시스템 조작 + 셸 실행(execute).
# - virtual_mode=True: 파일 도구의 경로를 workspace 기준으로 제한(.. 탈출 방지 가드레일).
#   전체 머신 접근이 필요하면 WORKSPACE_DIR 를 넓게 잡거나 virtual_mode=False 로 바꾼다.
# - inherit_env=True: git/python 등 로컬 도구를 그대로 쓸 수 있게 환경변수 상속.
backend = LocalShellBackend(
    root_dir=str(WORKSPACE),
    virtual_mode=True,
    inherit_env=True,
)


# ---------------------------------------------------------------------------
# 커넥터(도구) — 필요에 따라 자동으로 붙는다
# ---------------------------------------------------------------------------
def _run_async(coro):
    """이벤트 루프 유무와 무관하게 코루틴을 동기 실행한다."""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 이미 실행 중인 루프가 있으면 별도 스레드에서 돌린다.
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _web_search_tools() -> list:
    """웹 검색 커넥터(Tavily). TAVILY_API_KEY 가 있으면 자동 활성화."""
    if not os.getenv("TAVILY_API_KEY"):
        return []
    try:
        from langchain_tavily import TavilySearch
        from langchain_core.tools import StructuredTool
    except ImportError:
        print("[connector] langchain-tavily 미설치 — 웹 검색 건너뜀")
        return []

    base = TavilySearch(max_results=5)

    def _sanitize(kwargs: dict) -> dict:
        # Tavily 는 finance 토픽 + fast/ultra-fast search_depth 조합을 400 으로 거부한다.
        # LLM 이 이 조합을 고르면 search_depth 를 advanced 로 낮춰 유효한 호출로 보정한다.
        if kwargs.get("topic") == "finance" and kwargs.get("search_depth") in ("fast", "ultra-fast"):
            kwargs = {**kwargs, "search_depth": "advanced"}
        return kwargs

    def _search(**kwargs):
        return base.invoke(_sanitize(kwargs))

    async def _asearch(**kwargs):
        return await base.ainvoke(_sanitize(kwargs))

    print("[connector] Tavily 웹 검색 활성화")
    return [
        StructuredTool.from_function(
            func=_search,
            coroutine=_asearch,
            name=base.name,
            description=base.description,
            args_schema=base.args_schema,
        )
    ]


def _mcp_tools() -> list:
    """MCP 커넥터. 프로젝트 루트의 mcp_servers.json 이 있으면 로드.

    형식은 mcp_servers.example.json 참고. 빈 `{}` 면 아무것도 붙지 않는다.
    """
    config_path = Path("mcp_servers.json")
    if not config_path.exists():
        return []
    try:
        servers = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[connector] mcp_servers.json 파싱 실패(무시): {e}")
        return []
    if not servers:  # 빈 템플릿({}) 이면 조용히 건너뛴다
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        print("[connector] langchain-mcp-adapters 미설치 — MCP 커넥터 건너뜀")
        return []
    try:
        client = MultiServerMCPClient(servers)
        tools = _run_async(client.get_tools())
        print(f"[connector] MCP 서버 {len(servers)}개에서 도구 {len(tools)}개 로드")
        return tools
    except Exception as e:  # 커넥터 하나가 실패해도 에이전트는 떠야 한다
        print(f"[connector] MCP 로드 실패(무시하고 진행): {e}")
        return []


def build_connector_tools() -> list:
    """붙일 커넥터 도구를 모두 조립한다. 여기에 새 커넥터를 추가하면 된다."""
    tools: list = []
    tools += _web_search_tools()
    tools += _mcp_tools()
    tools += build_messaging_tools()  # Slack / Telegram / Email (connectors.py)
    return tools


connector_tools = build_connector_tools()


# ---------------------------------------------------------------------------
# 스킬(Skill) — Anthropic Agent Skills 패턴(점진적 공개)
# ---------------------------------------------------------------------------
# 스킬 소스 디렉터리(backend=workspace 기준 경로). 여러 개면 뒤로 갈수록 우선순위가 높다.
# 각 스킬은 workspace/skills/<이름>/SKILL.md 형태로 둔다.
# 에이전트는 처음엔 스킬의 이름·설명만 보고, 필요할 때 read_file 로 전체 지침을 읽는다.
SKILL_SOURCES = ["/skills/"]


# ---------------------------------------------------------------------------
# 메모리(Memory) — AGENTS.md 장기 기억
# ---------------------------------------------------------------------------
# 아래 파일들의 내용이 매 세션 시스템 프롬프트에 주입되고, 에이전트는 edit_file 로
# 스스로 갱신한다(선호·피드백·역할 등). backend=workspace 기준 경로.
MEMORY_SOURCES = ["/AGENTS.md"]


# ---------------------------------------------------------------------------
# 시스템 프롬프트
# ---------------------------------------------------------------------------
# deepagents 내장 BASE_AGENT_PROMPT 를 그대로 가져온 것이다. 자유롭게 편집하면 된다.
# (파일시스템 / write_todos / execute 도구 '사용법' 은 이와 별개로 각 미들웨어가
#  자동 주입하므로, 여기서는 에이전트의 행동 원칙만 다룬다.)
SYSTEM_PROMPT = """You are a deep agent, an AI assistant that helps users accomplish tasks using tools. You respond with text and tool calls. The user can see your responses and tool outputs in real time.

## Core Behavior

- Be concise and direct. Don't over-explain unless asked.
- NEVER add unnecessary preamble ("Sure!", "Great question!", "I'll now...").
- Don't say "I'll now do X" — just do it.
- If the request is underspecified, ask only the minimum followup needed to take the next useful action.
- If asked how to approach something, explain first, then act.

## Professional Objectivity

- Prioritize accuracy over validating the user's beliefs
- Disagree respectfully when the user is incorrect
- Avoid unnecessary superlatives, praise, or emotional validation

## Doing Tasks

When the user asks you to do something:

1. **Understand first** — read relevant files, check existing patterns. Quick but thorough — gather enough evidence to start, then iterate.
2. **Act** — implement the solution. Work quickly but accurately.
3. **Verify** — check your work against what was asked, not against your own output. Your first attempt is rarely correct — iterate.

Keep working until the task is fully complete. Don't stop partway and explain what you would do — just do it. Only yield back to the user when the task is done or you're genuinely blocked.

**When things go wrong:**

- If something fails repeatedly, stop and analyze *why* — don't keep retrying the same approach.
- If you're blocked, tell the user what's wrong and ask for guidance.

## Clarifying Requests

- Do not ask for details the user already supplied.
- Use reasonable defaults when the request clearly implies them.
- Prioritize missing semantics like content, delivery, detail level, or alert criteria.
- Avoid opening with a long explanation of tool, scheduling, or integration limitations when a concise blocking followup question would move the task forward.
- Ask domain-defining questions before implementation questions.
- For monitoring or alerting requests, ask what signals, thresholds, or conditions should trigger an alert.

## Progress Updates

For longer tasks, provide brief progress updates at reasonable intervals — a concise sentence recapping what you've done and what's next."""


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------
# create_deep_agent 은 넘긴 system_prompt 를 '내장 BASE_AGENT_PROMPT 앞에 덧붙인다'.
# 위 SYSTEM_PROMPT 는 그 내장 프롬프트를 그대로 복사한 것이라, system_prompt 로 넘기면
# 내용이 '중복'된다. 그래서 대신 HarnessProfile.base_system_prompt 로 등록해 내장
# 프롬프트를 '교체'한다(중복 없음). 프로필은 모델에 매칭되며, 키는 "<provider>:<identifier>"
# 형식이라 모델을 바꿔도 아래 계산식이 그대로 맞는 키를 만든다.
_profile_key = f"{get_model_provider(model)}:{get_model_identifier(model)}"
register_harness_profile(_profile_key, HarnessProfile(base_system_prompt=SYSTEM_PROMPT))

# deep agent 생성 (langgraph.json 이 이 `agent` 그래프를 참조).
# system_prompt 를 넘기지 않으므로 위에서 등록한 SYSTEM_PROMPT 가 그대로 사용된다.
def build_agent(checkpointer=None):
    """deep agent 그래프를 만든다.

    gateway.py 등 외부에서 checkpointer(InMemorySaver 등)를 넣어 대화별 문맥을
    유지하며 재사용할 수 있다. checkpointer=None 이면 langgraph dev 가 자체 지속성을
    제공한다.
    """
    return create_deep_agent(
        model=model,
        tools=connector_tools,
        backend=backend,
        skills=SKILL_SOURCES,
        memory=MEMORY_SOURCES,
        checkpointer=checkpointer,
    )


agent = build_agent()


# ---------------------------------------------------------------------------
# 실행 환경 감지 & langgraph dev 커맨드 조립
# ---------------------------------------------------------------------------
# 원격(Codespaces/devcontainer/Gitpod)에서는 로컬 브라우저가 없어 Studio 연결에
# 공개 URL 이 필요하다. 두 가지 경로를 지원한다:
#
# 1) 터널 모드(기본, LANGGRAPH_TUNNEL 미설정 시 원격이면 자동):
#    `langgraph dev --tunnel` 로 Cloudflare 터널(*.trycloudflare.com)을 열어 공개
#    URL 을 만든다. CORS/공개접근이 자동 처리돼 가장 간편하다.
#    단, 사내망/기업 프록시가 trycloudflare 를 차단하면 접속이 안 된다(SK 사내망 확인됨).
#
# 2) GitHub 포트포워딩 모드(LANGGRAPH_TUNNEL=0):
#    터널 없이 dev 서버만 띄우고, Codespaces 가 제공하는 *.app.github.dev 포워딩
#    URL 로 Studio 에 붙는다. GitHub 도메인은 사내망 화이트리스트인 경우가 많아
#    trycloudflare 가 막힌 환경의 대안이다. 단, 해당 포트를 반드시 'Public' 으로
#    바꿔야 한다(private 이면 Studio 의 fetch 가 GitHub 인증으로 막혀 401).
def _is_remote_env() -> bool:
    """로컬 브라우저가 없고 터널이 필요한 원격 개발 환경인지 판별한다."""
    return (
        os.getenv("CODESPACES", "").lower() == "true"
        or bool(os.getenv("CODESPACE_NAME"))
        or bool(os.getenv("GITPOD_WORKSPACE_ID"))
        or os.getenv("REMOTE_CONTAINERS", "").lower() == "true"
    )


def _env_bool(name: str):
    """불리언 성격의 환경변수를 읽는다. 미설정이면 None(= 자동판단에 위임)."""
    v = os.getenv(name)
    if v is None:
        return None
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _flag_present(args: list, name: str) -> bool:
    """passthrough 인자에 이미 해당 플래그가 있는지(값 포함 형태까지) 확인한다."""
    return any(a == name or a.startswith(name + "=") for a in args)


def _build_dev_command(passthrough: list) -> tuple:
    """`langgraph dev` 실행 커맨드와 터널 사용 여부를 조립한다.

    - LANGGRAPH_TUNNEL 로 강제 on/off 가능(미설정이면 원격 환경 자동 감지).
    - LANGGRAPH_PORT / LANGGRAPH_HOST 로 바인딩 조정 가능.
    - 스크립트에 넘긴 추가 인자(passthrough)는 그대로 전달하며, 중복 플래그는 넣지 않는다.
    """
    tunnel_override = _env_bool("LANGGRAPH_TUNNEL")
    use_tunnel = _is_remote_env() if tunnel_override is None else tunnel_override
    # 사용자가 직접 --tunnel 을 넘겼다면 그것도 존중한다.
    if _flag_present(passthrough, "--tunnel"):
        use_tunnel = True

    cmd = ["langgraph", "dev"]
    if not _flag_present(passthrough, "--allow-blocking"):
        cmd.append("--allow-blocking")
    if use_tunnel and not _flag_present(passthrough, "--tunnel"):
        cmd.append("--tunnel")
    # 터널이거나 원격이면 로컬 브라우저 자동 열기가 무의미하다(헤드리스).
    if (use_tunnel or _is_remote_env()) and not _flag_present(
        passthrough, "--no-browser"
    ):
        cmd.append("--no-browser")

    port = os.getenv("LANGGRAPH_PORT")
    if port and not _flag_present(passthrough, "--port"):
        cmd += ["--port", port]
    host = os.getenv("LANGGRAPH_HOST")
    if host and not _flag_present(passthrough, "--host"):
        cmd += ["--host", host]

    cmd += passthrough
    return cmd, use_tunnel


def _effective_port(passthrough: list) -> str:
    """실제로 바인딩될 포트를 추정한다(passthrough --port > LANGGRAPH_PORT > 기본 2024)."""
    for i, a in enumerate(passthrough):
        if a == "--port" and i + 1 < len(passthrough):
            return passthrough[i + 1]
        if a.startswith("--port="):
            return a.split("=", 1)[1]
    return os.getenv("LANGGRAPH_PORT", "2024")


def _codespaces_base_url(port: str):
    """Codespaces 포트포워딩 공개 URL을 환경변수로 조립한다(불가하면 None).

    GitHub Codespaces 는 CODESPACE_NAME 과 GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN
    (예: app.github.dev)을 주입한다. 포워딩 URL 형식은 https://<name>-<port>.<domain>.
    주의: 끝에 '/'를 붙이지 않는다 — Studio 가 baseUrl 뒤에 경로를 붙일 때
    '//assistants/search' 같은 더블슬래시가 되면 404(Not Found) 가 난다.
    """
    name = os.getenv("CODESPACE_NAME")
    domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    if not (name and domain):
        return None
    return f"https://{name}-{port}.{domain}"


def _studio_url(base_url: str) -> str:
    """주어진 API baseUrl 로 LangGraph Studio 링크를 만든다(트레일링 슬래시 제거)."""
    return f"https://smith.langchain.com/studio/?baseUrl={base_url.rstrip('/')}"


if __name__ == "__main__":
    import subprocess
    import sys

    # 메신저 커넥터가 '실제로 연결되는' 채널이 있으면 공용 게이트웨이를 백그라운드로
    # 함께 띄운다(env 만 있는 게 아니라 라이브 연결 확인까지 통과한 채널만 실행).
    # 채널이 하나도 없으면 게이트웨이 에이전트는 만들어지지 않는다.
    try:
        import gateway
        from langgraph.checkpoint.memory import InMemorySaver

        channels = gateway.start_in_background(
            lambda: build_agent(checkpointer=InMemorySaver())
        )
        if channels:
            print(f"게이트웨이 실행 중 → {', '.join(channels)} (메신저에서 에이전트 사용 가능)")
    except Exception as e:  # 게이트웨이가 실패해도 Studio UI 는 떠야 한다
        print(f"게이트웨이 시작 실패(무시하고 UI만 실행): {e}")

    # 이 파일을 직접 실행하면 langgraph dev 서버를 띄우고 LangGraph Studio를 연다.
    # langgraph dev 는 langgraph.json 을 읽어 위의 `agent` 그래프를 서빙한다.
    # 스크립트에 넘긴 추가 인자(예: --tunnel, --port 8000)는 그대로 전달된다:
    #   uv run python langchain-deepagents.py --tunnel
    cmd, use_tunnel = _build_dev_command(sys.argv[1:])
    _port = _effective_port(sys.argv[1:])
    _cs_base = _codespaces_base_url(_port)

    print(f"작업 공간: {WORKSPACE}")
    if use_tunnel:
        print("터널 모드 — Cloudflare 터널(--tunnel)로 공개 URL을 생성합니다.")
        print("  콘솔에 출력되는 https://<...>.trycloudflare.com 기반 Studio 링크를 여세요.")
        print("  ※ 사내망/기업 프록시가 trycloudflare 를 차단하면 접속되지 않습니다.")
        print("    그 경우 LANGGRAPH_TUNNEL=0 으로 GitHub 포트포워딩 모드를 쓰세요.")
        if _cs_base:
            print(f"  (대안) GitHub 포워딩 Studio URL: {_studio_url(_cs_base)}")
    elif _cs_base:
        print("GitHub 포트포워딩 모드 (사내망 친화적).")
        print(f"  API baseUrl : {_cs_base}")
        print(f"  Studio UI   : {_studio_url(_cs_base)}")
        print(f"  ⚠️ 포트 {_port} 를 반드시 'Public' 으로 바꾸세요(안 그러면 인증벽에 막혀 401):")
        print(f"     VS Code 하단 PORTS 패널 → 포트 {_port} 우클릭 → Port Visibility → Public")
        print(f"     또는: gh codespace ports visibility {_port}:public -c $CODESPACE_NAME")
        print("  ※ baseUrl 끝에 '/' 를 붙이지 마세요(더블슬래시 → 404 Not Found).")
    else:
        print("Deep Agent UI(LangGraph Studio)를 시작합니다... 잠시 후 브라우저가 열립니다.")
    print(f"$ {' '.join(cmd)}")

    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(
            "langgraph 명령을 찾을 수 없습니다. 먼저 'uv sync' 를 실행한 뒤 "
            "'uv run python langchain-deepagents.py' 로 다시 실행하세요."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
