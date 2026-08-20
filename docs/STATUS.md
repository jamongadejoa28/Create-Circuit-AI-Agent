# 현황 — 이 파일만 다음 작업을 적는다

> 판정: [`working-rules.md`](working-rules.md) · 아키텍처: [`ARCHITECTURE.md`](ARCHITECTURE.md)
>
> 캠페인 상세·구 계획·핸드오버는 저장소 밖
> [`create_circuit-docs-archive`](../../create_circuit-docs-archive/) — 에이전트는 참고하지 않는다.

갱신: 2026-08-20 · 문서 통합 · functional pin gate

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 (요약)

고정 입력: `tests/eval/sequential_campaign_v1.json` · 실행: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정 기준: `ko-step-024-cs-gnd-s2` / `ko-step-025-wp-nc-s2` (7번 W25Q·레일·WP/HOLD).
상세 숫자·시드별 분산·012–023 캠페인 서사는 아카이브 `STATUS-full-2026-08-20.md`.

재현된 핵심:

- 리페어 루프: 게이트 거부 사유를 다음 라운드에 전달, `unknown_symbol` 예시만 해당 라운드에 한정.
- 단락 op 거부 게이트는 **삭제** (008에서 pin-not-connected로 증발만 바뀜).
- 오디오(5번): `_restore_passive_roles` + overflow budget으로 스피커·포트 복원 012 3/3.
- 7번 SPI: W25Q `/CS` GND 추적·풀업·WP/HOLD released·레일 별칭 합치기 순으로 정규화 보강.

## 최근 코드 (2026-08-20)

- **선정 부품 추가** — `enforce_requested_part_variants`: 가족 sibling 없으면 카탈로그 심볼을 새 ref로 추가.
- **W25Q SPI** — `ensure_cited_w25q_spi_bus_nets`: 떠 있는 CLK/DI/DO/CS → SCK/MOSI/MISO/NSS 라벨 net.
- **I2C SDA/SCL** — `ensure_named_i2c_pin_nets` + TMP100 ADD0/ADD1 float → NC.
- **Functional pin gate** — `functional_pins.check_functional_pin_completeness` in `erc.check_circuit()`.

## 연결 문제 3층 (A / B / C)

| 층 | IR | 사람이 보는 회로도 | 원인 |
| --- | --- | --- | --- |
| **A. IR 미연결** | 핀이 `ir.nets`에 없음 | 부품만 떠 있음 | 합성·정규화가 net membership을 안 만듦 |
| **B. Geometry** | IR 연결됨 | 거의 붙어 보이거나 KiCad ERC 단선 | pin 좌표 ≠ wire endpoint |
| **C. Label fallback** | 전기적으로 연결 | **선이 끊겨 보임** | `emit` stub+동일 net label |

현재 `connectivity_ok`와 KiCad ERC 0은 **C를 성공으로 취급**한다.
`route_metrics.wired_ratio`는 통계일 뿐 게이트가 아니다.

`emit.build_emit_plan` 순서: direct → L → tree(≤8 terminal) → **stubs**.
`routed_cells`로 선행 net이 후행 net 경로를 막고 rip-up 없음.

Visual QA(`visual.py`)는 semantic geometry만 검사 — stub+label은 오류가 아니다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft: I2C 풀업, USB-C sink CC — 승격 금지
- 지식: `data/knowledge/manufacturer-datasheets.json` (provenance `datasheet`, pdf 페이지 인덱스 필수)
- 전압 한도: `data/device_limits.json` — STM32G474, TMP100 (SBOS231I)

## 다음 작업 (규칙 9)

**우선순위 (사용자·코드 분석 합의):**

1. **IR connectivity completeness (진행 중)** — `functional_pins` self-ERC. I2C/SPI/UART 이름 핀 미연결·허브 미도달 net은 emission 전 error. 후처리 normalize만 늘리지 않는다.
2. **stubs ≠ 읽을 수 있는 성공** — `critical_stub_nets` 등 net-kind별 wired 지표; local functional net은 실선 요구.
3. **route-aware placement + rip-up/reroute** — one-pass `routed_cells` 한계.
4. **multi-terminal bus/trunk router** — `TREE_MAX_NODES=8` 초과 I2C/SPI.
5. **benchmark debug overlay** — pin/wire/junction/route-mode SVG.

7번만 반복하지 않는다. I2C/SPI별 normalize 규칙 추가는 1번 게이트·측정 없이 하지 않는다.

## 데이터·학습

제조사 PDF: `data/datasheets/`. QLoRA 없음. SchGen accepted **0**, 승격 금지.

## 하지 않을 일

- ERC/벤치 점수 자랑 · 패턴 apply_when · SchGen 승격 · 회로명 특례
- 반례 키워드로 패시브 클래스 추측 · 부유 부품으로 role_present 부풀리기
- 1–5번 시퀀셜을 기본 작업 큐로 되돌리기
- 단락 op 게이트 재도입 (008에서 삭제한 이유)
- 구 `docs/*.md` 계획·핸드오버를 저장소에 다시 추가
