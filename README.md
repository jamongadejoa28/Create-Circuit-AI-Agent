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

필요한 것:

- Windows 쪽 `llama-server.exe` (Qwen2.5-Coder-7B, `--temp 0`) — WSL2에서
  `http://localhost:PORT`로 접근됩니다 (`.wslconfig`의 `networkingMode=mirrored`)
- Windows 쪽 KiCad 10 (`kicad-cli.exe`)
- 부품/지식 인덱스: `scripts/build_part_index.py`, `scripts/build_knowledge_index.py`

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

CLI만 쓰려면 `scripts/run_agent.py`, 측정은 `scripts/bench_general.py`입니다.

## 리포트 읽는 법

| 항목 | 뜻 |
|---|---|
| **막는 문제(blocking)** | 요청한 부품이 없음 / 전원 핀이 레일에 닿지 않음 / 부품에 전류가 흐를 수 없음. **하나라도 있으면 발주 금지** |
| 확인 필요(warnings) | 요청서 해석 단계의 판단이라 사람이 확인해야 하는 항목 |
| KiCad ERC 위반 | 배선이 규칙에 맞는지. **0이라고 회로가 맞는 것은 아닙니다** |
| 넷리스트 왕복 일치 | 그린 도면이 의도한 연결과 같은지 |
| 배선 대 라벨 비율 | 실제 선으로 그린 비율(사람이 읽기 쉬운 정도) |

## 지금 어디까지 되는가 (측정값, seed 31)

8개 회로군 벤치(`data/eval/general_circuit_suite.json`) 기준입니다.
**단일 점수를 만들지 않습니다** — 어느 회로군이 왜 안 되는지가 유일하게 쓸모 있는 정보입니다.

| 회로군 | 결과 |
|---|---|
| 수동(LED+스위치), 아날로그(비반전 증폭), 전원(LDO), 통신(CAN) | 완주, ERC 0, 모든 부품이 전기적으로 동작 |
| I2C 센서 | 풀업 저항 한 다리가 어느 넷에도 안 붙음 |
| STM32 최소회로 | 디커플링 6개·크리스탈 2개가 MCU에 닿지 않는 넷에 매달림 |
| 릴레이 드라이버 | 패턴이 `Relay:G5V-1`을 바인딩하지 못함(핀 번호 규약 불일치), 대체 없이 거부 |
| 카탈로그 외 모듈 | MCU 핀을 번호가 아니라 이름(TX/RX)으로 연결 |

넷 다 **문제를 이름 대고 보고합니다.** 조용히 틀린 보드를 내놓지 않는 것이
이 단계의 출시 기준입니다.

## 작업 규칙

`docs/working-rules.md`를 먼저 읽으세요. 테스트를 통과시키려고 코드를 쓰지 않기,
점수 상승을 증거로 삼지 않기, 발견한 문제는 이월하지 않기 — 전부 이 저장소에서
실제로 일어난 실패에서 나온 규칙입니다.
