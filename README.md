# create_circuit

프롬프트를 받아 **KiCad 10 회로도(.kicad_sch)**를 그리는 오프라인 에이전트입니다.
부품 선정은 사용자가 미리 끝내고 옵니다. 이 도구가 하는 일은 **설계**입니다 —
저항을 어디에 얼마로 넣을지, 크리스탈이 필요한지, 디커플링을 몇 개 붙일지.

## 이 도구가 지키는 것

**정확도보다 정직함이 먼저입니다.** 요청한 부품이 보드에 없으면 없다고 말하고,
부품이 놓여 있어도 전류가 흐를 수 없으면 그렇다고 말합니다. 그래도 도면은 그려서
넘겨줍니다 — 틀린 곳을 눈으로 볼 수 있어야 하기 때문입니다.
**"막는 문제 0건"이 아니면 그 회로도로 PCB를 발주하면 안 됩니다.**

## 실행

Windows에서 추론 서버와 KiCad CLI를 띄운 뒤, WSL2에서 에이전트를 실행합니다.
에이전트는 llama-server를 기동하지 않습니다 (WSL에서 실행하면 exit 53).

**Windows `llama-server.exe`** (슬롯 1, ctx 8192). 온도는 클라이언트가 0으로 고정합니다.

```text
llama-server.exe -m Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf --host 127.0.0.1 --port 8080 --ctx-size 8192 -ngl 99 --parallel 1
```

빌드 위치 예: `C:\Users\hajun\llama.cpp\build\bin\Release\llama-server.exe`.
WSL2는 `.wslconfig`의 `networkingMode=mirrored`로 `http://127.0.0.1:8080`에 붙습니다.

**Windows KiCad 10** `kicad-cli.exe`는 `C:\Program Files\KiCad\10.0\bin\`입니다. 에이전트가 `wslpath -w`로 경로를 변환합니다.

부품/지식 인덱스:

```bash
PYTHONPATH=src .venv/bin/python scripts/build_part_index.py
PYTHONPATH=src .venv/bin/python scripts/build_knowledge_index.py
```

```bash
.venv/bin/pip install -e ".[web]"     # 한 번만. 이걸 빼면 ModuleNotFoundError: circuitgen
.venv/bin/python -m uvicorn circuitgen.webapp:app --port 8000
```

브라우저에서 `http://localhost:8000`. 한 번에 한 작업만 돌립니다(llama-server 슬롯이 하나).

설치하지 않고 한 번만 띄워보려면 `PYTHONPATH=src`를 앞에 붙이면 됩니다.
editable 설치라 `src/`를 그대로 가리키므로 `data/`(부품·지식 인덱스, 패턴,
`device_limits.json`) 경로는 설치 후에도 저장소 안을 그대로 씁니다.

API로 쓸 경우:

```bash
curl -s localhost:8000/api/health
curl -s -X POST localhost:8000/api/jobs -H 'content-type: application/json' \
     -d '{"prompt":"...", "name":"led"}'          # -> {"id": "..."}
curl -s localhost:8000/api/jobs/<id>                # 상태와 리포트
curl -s localhost:8000/api/jobs/<id>/file/schematic  # .kicad_sch
```

CLI만 쓰려면 `scripts/run_agent.py`, 측정은 `tests/benchmarks/bench_general.py`입니다.

## 서비스와 테스트 자산의 경계

- `src/circuitgen/`: 웹·CLI 서비스가 실행하는 제품 코드
- `data/knowledge/`, `data/rules/`, `data/*.json`: 실제 생성에 사용하는 출처 기반 지식, typed 설계 규칙, 장치 데이터
- `scripts/`: 서비스 실행 및 런타임 인덱스 구축 도구
- `tests/fixtures/`: 소수의 테스트 입력 헬퍼. 모델 정답이나 제품 지식이 아님
- `tests/eval/`, `tests/benchmarks/`: 평가셋과 벤치 실행기
- `tests/artifacts/`: 테스트·벤치가 만든 산출물. Git에는 포함하지 않음
- `tests/tools/`: 데이터시트 인용 검증 도구

제품 코드와 서비스 스크립트는 `tests`를 import하지 않습니다.

`pytest`는 모델의 회로 설계 능력이나 PNG 가독성을 판정하지 않습니다. 그 역할은
실제 llama-server를 호출하는 벤치와 생성된 PNG의 별도 검토가 담당합니다. 테스트 종류,
허용하는 하드코딩의 근거, 제거한 과적합 경로는 [`docs/TESTING.md`](docs/TESTING.md)에
정리되어 있습니다.

## 리포트 읽는 법

웹·CLI 리포트는 캠페인과 같은 순서로 읽습니다. **ERC는 마지막입니다.**

| 항목 | 뜻 |
|---|---|
| 선택한 부품 | 사용자가 고른 부품이 보드에 있는지. 없으면 발주 금지 |
| 역할 동작 (`role_working` / `role_total`) | 부품이 놓여 있어도 전류가 흐를 수 없으면 동작하지 않은 것 |
| 커넥터 접점 | 요청 행·열·접점 수 vs 심볼 핀 수 vs footprint 패드 수 |
| **막는 문제(blocking)** | 위 불일치와 전원 핀이 레일에 안 닿는 것. **하나라도 있으면 발주 금지** |
| 확인 필요(warnings) | 요청서 해석 단계의 판단이라 사람이 확인해야 하는 항목 |
| KiCad ERC 위반 | 배선이 규칙에 맞는지. **0이라고 회로가 맞는 것은 아닙니다** |
| 넷리스트 왕복 일치 | 그린 도면이 의도한 연결과 같은지 |
| 배선 대 라벨 비율 | 실제 선으로 그린 비율(사람이 읽기 쉬운 정도) |

## 두 가지 모드

**요청에 연결 명세(넷리스트)가 들어 있으면 전사(transcription) 모드**로 동작합니다.
설계 판단을 하지 않고 쓰신 대로 옮깁니다 — SkiDL과 같은 계약입니다. 부품을 더하지도,
빠진 것을 채우지도, 순서를 바꾸지도 않습니다. 정답 판정이 **정확**합니다: 쓰신 모든
참조와 핀이 회로에 있거나, 없다고 이름을 대고 보고됩니다.

넷리스트 없이 기능만 설명하시면 **설계 모드**입니다. 아직 약합니다(아래).

```
[연결 명세 (Net List)]
- Net 'VIN': J1 Pin 1, U1 Pin 3(VIN), C1 Pin 1
- Net 'GND': J1 Pin 2, U1 Pin 1(GND), C1 Pin 2, D1 캐소드(K)
```

이렇게 써주시면 전사 모드로 갑니다.

## 지금 어디까지 되는가 (측정값)

**전사 모드** (seed 1, Qwen2.5-Coder-7B, commit 92036d9):

| 요청 | 결과 |
|---|---|
| AMS1117 LDO (부품 9개, 넷 4개) | 완주, KiCad ERC **0**, 배선율 **1.0**, 60초 |
| NE555 비안정 (부품 8개, 넷 7개) | 완주, KiCad ERC **0**, 배선율 0.67, 66초 |
| ATmega328P 최소회로 (부품 12개, 넷 12개) | ERC 19, 99초 — MCU의 **안 쓰는 GPIO 17개**를 KiCad가 세는 것 |

셋 다 **막는 문제 0건**. 부품 선택은 쓰신 넷리스트가 쓰는 핀으로 제약됩니다 —
"택트 스위치"는 2핀 부품, "1x6 헤더"는 6핀 커넥터가 됩니다.

**설계 모드** — 8개 회로군 벤치(`tests/eval/general_circuit_suite.json`).
**단일 점수를 만들지 않습니다**; 어느 회로군이 왜 안 되는지가 유일하게 쓸모 있는 정보입니다.

| 회로군 | 결과 |
|---|---|
| 수동(LED+스위치), 아날로그(비반전 증폭), 전원(LDO), 통신(CAN) | 완주, ERC 0, 모든 부품이 전기적으로 동작 |
| I2C 센서 | 풀업 저항 한 다리가 어느 넷에도 안 붙음 |
| STM32 최소회로 | 디커플링·크리스탈이 MCU에 닿지 않는 넷에 매달림 |
| 릴레이 드라이버 | 패턴이 `Relay:G5V-1`을 바인딩하지 못함, 대체 없이 거부 |
| 다중 블록 보드(4모터 등) | 주변장치 제어핀이 컨트롤러에 닿지 않는 경우가 남아 있음 |

전부 **문제를 이름 대고 보고합니다.** 조용히 틀린 보드를 내놓지 않는 것이
이 단계의 출시 기준입니다.

## 실행이 비교 가능합니다

리포트에 `commit · seed · prompt 해시 · model`이 찍힙니다. 모델은 temperature 0에서
결정론적이므로, **네 값이 같으면 결과가 같습니다.** 결과가 달라졌다면 그중 하나가
달라진 것입니다 — 개선을 주장하기 전에 먼저 확인할 값입니다.

## 작업 규칙

`docs/working-rules.md`를 먼저 읽으세요. 테스트를 통과시키려고 코드를 쓰지 않기,
점수 상승을 증거로 삼지 않기, 발견한 문제는 이월하지 않기 — 전부 이 저장소에서
실제로 일어난 실패에서 나온 규칙입니다. 현재 상태와 다음 지표 공백은
`docs/STATUS.md`에만 적습니다. 시스템 구조는 `docs/ARCHITECTURE.md` 하나로
봅니다. 구 계획·캠페인 상세는 저장소 밖 `create_circuit-docs-archive/`에
두었습니다. 새 계획 파일을 만들지 않습니다.
