---
name: meta-harness
description: 자기 자신(이 하네스)을 격리된 복사본으로 헤드리스 실행해 실행기록·산출물을 확인하고, 시스템 프롬프트·도구코드·스킬을 격리 환경에서 수정해 다시 실행한 뒤, 두 결과를 비교해 더 나은 쪽만 본체에 반영(promote)하는 자기개선 절차. "meta-harness", "자기개선", "self-improve", "이 질의로 나를 개선", "프롬프트/도구/스킬 A/B 로 실험" 같은 요청에 사용.
---

# meta-harness — 자기 자신을 격리 실행·개선하기

이 스킬은 **하네스가 자기 자신을 개선**하게 한다. 흐름은 이렇다:

1. 본체(레포 소스)를 **격리 복사**한 `baseline` 을 만든다.
2. 질의 **A** 에 대해 baseline 을 **헤드리스로 실행**하고, 실행기록·최종답변·산출물·지표를 캡처한다.
3. baseline 의 약점을 진단하고, 개선을 적용한 **variant(`v1`)** 를 격리 상태에서 만든다.
4. 같은 질의 A 로 v1 을 실행한다.
5. 두 실행을 **비교**한다.
6. v1 이 더 나으면 사용자 확인 후 본체에 **promote**, 아니면 baseline 유지(필요 시 `v2` 로 반복).

**모든 격리 작업은 레포 밖 임시 홈에서 일어난다.** 본체와 라이브 `langgraph dev` 는
`promote` 전까지 절대 바뀌지 않는다. meta-run 은 기본적으로 메시징 커넥터(Slack/
Telegram/Email)가 꺼진 상태로 돌아 실수로 실제 메시지를 보내지 않는다.

## 엔진

모든 조작은 이 스킬의 CLI 로 한다. 셸(`execute`)에서 호출한다:

```
python skills/meta-harness/metaharness.py <subcommand> [옵션]
```

> cwd 가 workspace 가 아니어도 `--home` 없이 레포를 자동 탐지한다. 스크립트가 레포
> 루트를 위로 올라가며 찾는다. 홈 경로는 `doctor` 출력에서 확인할 수 있다.

서브커맨드: `doctor · init · run · fork · edit · set-prompt · diff · compare · show · list · promote · clean`

## 절차

### 0) 사전 점검 (필수)

```
python skills/meta-harness/metaharness.py doctor
```

- `harness_entry: true`, `deepagents` 정상, `OPENAI_API_KEY: true` 인지 확인.
- `git_clean` 이 false 면 사용자에게 **먼저 커밋/스태시** 를 권한다(promote 롤백 안전망).
- 질의 A 의 **성공기준**을 명확히 한다. 사용자가 안 줬으면 짧게 물어라: *무엇이
  "더 나은" 결과인가?* (정확성 / 산출물 품질 / 단계 수·속도 / 특정 도구 사용 등).
  기준 없이 비교하면 정량지표에 과적합된다.

### 1) baseline 실행

질의 A 를 파일로 저장한 뒤(멀티라인·특수문자 안전) 실행한다:

```
cat > /tmp/queryA.txt <<'EOF'
<질의 A 전문>
EOF
python skills/meta-harness/metaharness.py run --variant baseline --query-file /tmp/queryA.txt
```

- `run` 은 baseline 이 없으면 자동 생성한다.
- 끝나면 요약(JSON)과 산출물 경로가 출력된다. 실행기록을 읽어라:

```
python skills/meta-harness/metaharness.py show --variant baseline --what transcript
python skills/meta-harness/metaharness.py show --variant baseline --what answer
```

산출물 파일 목록/내용은 요약의 `artifacts` 와 `runs/baseline/artifacts/` 에 있다.

### 2) 진단 → 개선 지점 매핑

실행기록·최종답변·산출물을 성공기준에 비추어 **구체적 약점**을 찾는다. 각 약점을
아래 "노브(knob)" 중 하나에 연결한다:

| 약점 | 수정할 곳(variant 안) |
|------|----------------------|
| 행동원칙/톤/판단 문제 | `langchain-deepagents.py` 의 `SYSTEM_PROMPT` |
| 특정 도구의 동작/스키마/에러 | `connectors.py`, 또는 `langchain-deepagents.py` 의 `_web_search_tools`/`_mcp_tools` |
| 반복 절차의 품질 | `workspace_seed/skills/<name>/SKILL.md` (+ 스크립트) |

### 3) variant 만들고 **최소 변경**으로 수정

```
python skills/meta-harness/metaharness.py fork --from baseline --name v1
```

**핵심 원칙 — 원본은 그대로 두고, 바꿀 부분만 국소 수정한다.** variant 는 원본과
글자 하나까지 동일한 복사본이다. 개선은 "원본을 통째로 다시 쓰는 것"이 아니라
"원본에서 문제가 되는 부분만 정확히 고치는 것"이다. 그래야 (1) 무엇이 효과였는지
분리되고 (2) 원본의 좋은 지침을 실수로 날리지 않는다. 한 번에 한두 곳만 바꿔라.

수정은 `edit` 서브커맨드로 한다(원본 텍스트 조각을 정확히 find→replace, 유일 매칭 강제):

```
# 예: SYSTEM_PROMPT 에 규칙 한 줄을 삽입하되 원본은 보존 — 앵커 텍스트 뒤에 끼워넣기
python skills/meta-harness/metaharness.py edit --variant v1 \
  --file langchain-deepagents.py \
  --find "## Core Behavior" \
  --replace "## Output Persistence

- 핵심 결과는 workspace/answer.txt 에도 저장하라.

## Core Behavior"

# 예: 특정 규칙 문장 하나만 교체
python skills/meta-harness/metaharness.py edit --variant v1 --file connectors.py \
  --find "max_results=5" --replace "max_results=8"
```

- `--find` 는 원본과 **정확히**(공백·들여쓰기 포함) 일치해야 하고, 파일에서 **유일**해야
  한다(여러 번 나오면 거부 → 더 긴 조각으로 좁혀라). 멀티라인은 `--find-file`/`--replace-file`.
- `--replace` 를 생략/빈값이면 해당 부분 **삭제**. 즉 add/modify/remove 모두 국소로 된다.
- 수정 뒤 반드시 diff 로 **변경이 최소한인지** 확인한다:
  `python skills/meta-harness/metaharness.py diff --a baseline --b v1`
  → 의도한 몇 줄만 바뀌어야 한다. 원본이 통째로 사라졌으면 잘못한 것이다.

> `set-prompt`(프롬프트 블록 전체 교체)는 프롬프트를 **의도적으로 전면 재작성**할 때만
> 써라. 원본보다 크게 짧아지면 경고가 뜬다(원본 지침을 날린 신호). 대부분의 개선은
> `edit` 로 충분하다.

수정 후 실제 무엇이 바뀌었는지 확인:

```
python skills/meta-harness/metaharness.py diff --a baseline --b v1
```

### 4) variant 실행

```
python skills/meta-harness/metaharness.py run --variant v1 --query-file /tmp/queryA.txt
```

### 5) 비교

```
python skills/meta-harness/metaharness.py compare --a baseline --b v1
```

정량지표(단계 수, 토큰, 산출물 수, 소요시간)는 **참고용**이다. 두 `transcript.md` 와
`final_answer.md`, 산출물을 직접 읽고 **성공기준에 비추어 우열을 판단**하라. LLM 은
비결정적이므로 접전이면 `run` 을 한 번 더 돌려(같은 variant 를 다시 run 하면 덮어씀)
결과가 안정적인지 본다.

판정은 셋 중 하나: **v1 우세 / baseline 우세 / 무승부·혼재**.

### 6) 반영 또는 유지

- **v1 우세** → 사용자에게 변경 diff 를 보여주고 승인받은 뒤 반영:
  ```
  python skills/meta-harness/metaharness.py promote --variant v1        # 미리보기(diff)
  python skills/meta-harness/metaharness.py promote --variant v1 --yes  # 실제 적용
  ```
  promote 는 본체(레포)에 쓰는 유일한 명령이다. 라이브 `langgraph dev` 는 파일 변경으로
  자동 리로드된다. 되돌리려면 `git checkout -- <file>`.
- **baseline 우세** → 아무것도 하지 않는다(본체 유지). 개선 여지가 남았으면
  `fork --from baseline --name v2` 로 다른 가설을 시도하고 2~5 를 반복한다.
- **무승부** → 변경을 반영하지 않는 편을 기본으로 하되, 사용자에게 판단을 넘긴다.

마지막에 **무엇을 바꿨고, 지표가 어떻게 달라졌으며, 왜 그 결정을 했는지** 요약 보고한다.

## 반복(선택)

여러 가설을 누적 비교할 수 있다. baseline 을 고정 기준으로 두고 `v1, v2, …` 를 각각
`fork --from baseline` 로 만들거나, 좋았던 variant 를 새 기준으로 삼아
`fork --from v1 --name v1b` 처럼 이어간다. 최종 승자만 promote 한다.

## 주의사항

- **비용/시간**: 매 `run` 은 하네스를 통째로 띄워 실제 모델을 호출한다(수십 초~수 분).
  질의 A 를 개선 신호가 잘 드러나는 대표 과제로 좁게 잡아라. 도구를 많이 쓰는
  variant 는 baseline 보다 오래 걸릴 수 있으니 `--timeout` 을 넉넉히(예: 600) 준다.
- **timeout=부분 캡처**: `--timeout` 초를 넘기면 실행을 멈추되 그때까지의 transcript·
  산출물·지표를 그대로 남긴다(summary 의 `partial: true`, `error` 에 사유). 실패해도
  실행기록이 사라지지 않으므로, 왜 느렸는지/멈췄는지 `show --what transcript` 로 진단하라.
- **격리 보장**: variant 는 레포 밖에 복사되고 격리 워크스페이스에서 돈다. 본체 workspace/
  AGENTS.md/이메일 트리거는 건드리지 않는다.
- **커넥터 off 기본**: 실제 Slack/이메일까지 포함해 재현해야 하면 `init --live` /
  `fork --live` 로 만들되, 실제 메시지가 나갈 수 있음을 사용자에게 먼저 경고하라.
- **정리**: `python skills/meta-harness/metaharness.py clean --all` 로 임시 홈을 지운다.
