# 현황 — 이 파일만 다음 작업을 적는다

> 판정: [`working-rules.md`](working-rules.md) · 아키텍처: [`ARCHITECTURE.md`](ARCHITECTURE.md)
>
> 캠페인 상세·구 계획·핸드오버는 저장소 밖
> [`create_circuit-docs-archive`](../../create_circuit-docs-archive/) — 에이전트는 참고하지 않는다.

갱신: 2026-08-21 · 테스트/평가 경계 감사 및 route telemetry 완료

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
- **typed interface contract** — BlockPlan의 각 interface가 `peer`
  (`controller|external|block`), `protocol`, `required`를 가지고
  `CircuitIR.controller_refs`/`interface_contracts`까지 보존된다.
- **IR connectivity gate** — 최대 핀 수 부품을 허브로 추측하지 않는다. controller 없음,
  계약 net/owner/controller endpoint 누락, I2C/SPI/serial 주변장치의 controller 미도달을
  emission 전 error로 보고한다. PWM/DIR/FAULT도 `generic_control` 계약으로 같은 검사를 받는다.
- **SPI/UART 보강** — 활성 SCK/MOSI/MISO가 있는 주변장치의 CS/NSS NC를 거부한다.
  UART controller pin은 기록된 datasheet AF를 확인한다. TXD/RXD는 계약/라이브러리 문맥이
  있을 때만 UART/CAN으로 분류하고 그 외에는 SERIAL로 보고한다.
- **C층 측정** — `route_metrics`가 `stub_net_names`,
  `critical_stub_nets`, protocol별 critical 수치를 기록하고 design `run.json` 및 sequential
  benchmark에도 보존한다. 정적 SVG 존재 여부를 회귀 검사로 가장하던 fixture는 제거했다.
- **route failure telemetry** — tree router의 무정보 `None`을 `terminal_limit`,
  `off_grid_terminal`, `escape_blocked`, `astar_no_path`, `foreign_geometry`,
  `occupied_by_net` 등으로 구조화했다. 기존 배선 없이 진단 재시도가 성공한 경우에만
  occupancy 실패로 판정하고 방해 net 이름을 함께 기록한다. sequential benchmark와
  추적 metrics에서 SDA/SCL의 현재 원인은 `terminal_limit`로 확인된다.
- **unified routing occupancy** — direct/L/tree가 하나의 `RoutingContext`에서 symbol box,
  foreign pin과 잠재 stub corridor, 선행 net wire cell을 검사한다. 어떤 route mode도 공유
  validator를 우회해 다른 net을 가로지를 수 없으며, 거부된 교차는 blocker net telemetry로
  남는다.

## 연결 문제 3층 (A / B / C)

| 층 | IR | 사람이 보는 회로도 | 원인 |
| --- | --- | --- | --- |
| **A. IR 미연결** | 핀이 `ir.nets`에 없음 | 부품만 떠 있음 | 합성·정규화가 net membership을 안 만듦 |
| **B. Geometry** | IR 연결됨 | 거의 붙어 보이거나 KiCad ERC 단선 | pin 좌표 ≠ wire endpoint |
| **C. Label fallback** | 전기적으로 연결 | **선이 끊겨 보임** | `emit` stub+동일 net label |

현재 `connectivity_ok`와 KiCad ERC 0은 **C를 성공으로 취급**한다.
`route_metrics.wired_ratio`와 `critical_wired_ratio`는 통계일 뿐 게이트가 아니다.
9-terminal I2C 재현 테스트에서 SCL/SDA는 `terminal_limit`으로 stub fallback된다.

`emit.build_emit_plan` 순서: direct → L → tree(≤8 terminal) → **stubs**.
세 route mode의 occupancy 정책은 통합됐다. 다만 IR net 순서대로 선행 net이 공간을
선점하는 one-pass이고 priority/rip-up은 아직 없다.

Visual QA(`visual.py`)는 semantic geometry만 검사한다. 종횡비와 detour 길이는 오류가
아니라 관측값이며, stub+label도 전기 오류로 취급하지 않는다. PNG 가독성은 별도 검토다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft rule 파일은 보관하지 않는다. 검증되지 않은 규칙은 제품/평가 입력이 아니다.
- 지식: `data/knowledge/manufacturer-datasheets.json` (provenance `datasheet`, pdf 페이지 인덱스 필수)
- 전압 한도: `data/device_limits.json` — STM32G474, TMP100 (SBOS231I)

## 다음 작업 (규칙 9)

**우선순위 (사용자·코드 분석 합의):**

1. ~~**critical stub 측정**~~ — 이름·protocol별 지표와 실패 원인 재현 완료.
2. ~~**route failure telemetry**~~ — 실패 stage/reason/blocker net 및 benchmark 노출 완료.
3. ~~**unified routing occupancy**~~ — direct/L/tree 공유 `RoutingContext`와 validator 완료.
4. **critical-first + rip-up/reroute** — typed required controller 계약 net을 먼저
   routing하고, `occupied_by_net` blocker를 제한적으로 걷어낸 뒤 재시도한다.
5. **route-aware placement** — critical 실패 net에만 거리·pin 방향·장애물 밀도 기반
   local placement search를 적용한다.
6. **multi-terminal bus/trunk router** — 재현된 SDA/SCL처럼 `TREE_MAX_NODES=8`을
   초과한 공유 bus를 trunk+branch 실선으로 만든다.
7. **benchmark debug overlay** — pin/wire/junction/route-mode/failure SVG.

7번만 반복하지 않는다. I2C/SPI별 normalize 규칙 추가는 IR connectivity gate·측정 없이 하지 않는다.

## 데이터·학습

제조사 PDF: `data/datasheets/`. QLoRA 없음. SchGen accepted **0**, 승격 금지.

## 하지 않을 일

- ERC/벤치 점수 자랑 · 패턴 apply_when · SchGen 승격 · 회로명 특례
- 반례 키워드로 패시브 클래스 추측 · 부유 부품으로 role_present 부풀리기
- 1–5번 시퀀셜을 기본 작업 큐로 되돌리기
- 단락 op 게이트 재도입 (008에서 삭제한 이유)
- 구 `docs/*.md` 계획·핸드오버를 저장소에 다시 추가
