# 출시 게이트 (2026-08-09)

> 범위: **"한계를 인지하고 먼저 출시하되, 출시본이 최소한 기능은 해야 한다"**는 요구에 따라
> 복구 큐 1·2번과 3번의 출시 필수 부분만 구현.
> 4번(eval 하니스 수정)·5번(홀드아웃 구축)과 3번의 나머지(패턴 lib_id 전면 해제)는 착수하지 않았습니다.

---

## 0. 왜 이 순서인가

출시본이 갖춰야 할 최소 성질은 "항상 맞는 회로를 낸다"가 아니라 **"틀렸을 때 틀렸다고 말한다"**입니다.
7B 모델과 10개 패턴으로 임의의 보드를 정확히 설계할 수는 없지만, 잘못된 결과를 성공으로 보고하는 시스템은
출시하면 안 됩니다. 그래서 이번 작업은 **정확도를 올리는 것이 아니라 정직성을 강제**하는 데 집중했습니다.

세 게이트 모두 **run을 중단하지 않습니다.** 회로도 파일은 그대로 나오고, 보고서가 무엇이 어긋났는지 함께 나옵니다.
`res.ok`는 이제 "ERC 통과"가 아니라 "ERC 통과 **그리고** 요구사항 충족"을 의미합니다.

| 항목 | 이전 | 이후 |
|---|---|---|
| 테스트 | 176개 / 186초 | **194개 / 189초** |
| 지정 부품 누락 | 조용히 대체, `ok=True` | `requested_part_missing` 오류, `stage=requirement-mismatch` |
| 전원 미연결 MCU | ERC 0, `ok=True` | `power_pin_unpowered` 오류 |
| 절대최대 초과 전압 | ERC 0, `ok=True` | `supply_over_absolute_maximum` 오류 |
| 보드 요청에 조각 패턴 응답 | 전체 보드가 8부품으로 치환 | 패턴 거부 → LLM 경로 |

---

## 1. 결함 재현 (수정 전)

세 건 모두 코드를 건드리기 전에 실행해서 확인했습니다.

### 1-1. 지정 부품 무단 대체
`"ESP32-C3에 BME280 i2c 센서를 연결해줘"` → 패턴 경로가 다음을 생성하고 성공으로 보고:

```
U1  MCU_ST_STM32G4:STM32G474RETx     ← ESP32-C3를 요청했음
U2  Sensor_Temperature:Si7050-A20    ← BME280을 요청했음
log: pattern synthesis: i2c_temperature_sensor instantiated deterministically
```

원인: `_pattern_synthesis`가 role의 `lib_id`를 고정으로 쓰고 `spec.parts_needed`는 **파라미터 값**에만 사용.
패턴 41개 role 중 31개가 lib_id 고정입니다.

### 1-2. 보드 규모 요청을 조각 패턴이 삼킴
`testprompt.md` 18개 보드 프롬프트를 실제로 돌려 확인한 결과 **3건**이 정확히 한 패턴에만 매칭되어
보드 전체가 그 조각으로 치환됩니다.

| 보드 | 요청 | 응답한 패턴 |
|---|---|---|
| #1 | STM32H743 + Ethernet + RS485(Modbus) + CAN-FD + SD카드 | `can_transceiver_interface` (8부품) |
| #6 | 24V PLC, DI 16채널 + 릴레이 8채널 + RS485 + Ethernet | `relay_driver` (5부품) |
| #11 | 6S BMS, 셀전압/온도 모니터링 + CAN | `can_transceiver_interface` (8부품) |

### 1-3. ERC가 볼 수 없는 전원 결함
STM32G474 보드 두 종류를 만들어 자체 ERC에 넣었습니다.

| 보드 | 자체 ERC 오류 |
|---|---|
| VDD/VDDA/VBAT 전부 NC (전원 없는 MCU) | **0건** (PWR_FLAG 포함 시) |
| VDD를 +5V에 연결 (절대최대 4.0 V) | **0건** |

ERC는 NC 핀을 건너뛰고 전압 정격 개념이 없습니다. 실제로 이 두 형태가 벤치 "통과" 보드로 출하됐습니다.

---

## 2. 구현

### 2-1. `src/circuitgen/compliance.py` (신규)

**요구사항 정합성.** 프롬프트와 spec에서 부품 번호를 추출해 최종 IR에 존재하는지 확인합니다.

- 토큰 규칙: 문자 1~6 + 숫자 2자리 이상 + 접미사, 최소 5자.
  `BME280`·`SHT30`·`LM358`·`RP2040`·`ESP32-C3`·`STM32G474RET6`는 잡고,
  `PA15`·`I2C1`·`USART1`·`24V`·`0805`는 잡지 않습니다.
  프로토콜/등급 토큰(`RS485`, `CANFD`, `IP65`, `USB20` …)은 별도 제외 목록.
  (기존 `agent._ensure_named_parts`의 정규식은 `BME280`을 **놓칩니다** — 숫자 뒤 3자를 요구.)
- 매칭은 KiCad의 오더링 코드 관례를 허용: 요청 `STM32G474RET6` ↔ 심볼 `STM32G474RETx` 충족.

**전원 무결성.** 두 층으로 나눴습니다.

1. **구조 검사 — 데이터 불필요, 모든 부품에 적용.** PWRIN 핀이 NC이거나 신호 네트에 있으면 오류.
   근거는 보편 규칙이자 데이터시트 각주: *"All main power (VDD, VDDA, VBAT) and ground (VSS, VSSA)
   pins must always be connected to the external power supply, in the permitted range."*
2. **전압 검사 — 데이터시트에 기록된 부품만.** `data/device_limits.json`에 항목이 없으면 **검사하지 않은 것**이고,
   보고서의 `voltage_checked_devices`가 무엇을 실제로 검증했는지 명시합니다. 침묵은 합격이 아닙니다.

`data/device_limits.json` 최초 항목은 저장소의 `DS_stm32g474ve.pdf`에서 PyMuPDF로 직접 읽었습니다.

| 값 | 출처 |
|---|---|
| 절대최대 4.0 V | DS12288 Rev 6, Table 14 Voltage characteristics, pdf p.81 (인쇄 82/236) |
| 동작 1.71–3.6 V | DS12288 Rev 6, Table 17 General operating conditions, pdf p.83 (인쇄 84/236) |

테스트가 **모든 항목에 인용(document + table + pdf_page_index + text)이 있는지** 강제합니다.
검증하지 않은 절대최대값은 항목이 없는 것보다 나쁩니다 — 아무도 확인하지 않은 판정을 내리게 되므로.

### 2-2. 패턴 범위 가드 (`patterns.py`)

패턴은 **기능 하나**를 구현합니다. 요청이 여러 기능 서브시스템을 이름으로 부르는데
패턴이 그중 일부만 제공하면 거부합니다.

- `SUBSYSTEM_KEYWORDS` 25종(ethernet/rs485/can/i2c/uart/motor/relay/battery/display/…), 한·영.
- ASCII 키워드는 단어 경계 필수 — `"this can be assembled"`가 CAN으로 잡히면 안 됩니다.
- 판정은 **프롬프트만** 사용합니다. 추출된 spec은 LLM의 의역이라 run마다 흔들려 게이트가 깜빡입니다.
- 요청 서브시스템이 **2개 이상**일 때만 발동. 1개짜리 요청은 패턴이 존재하는 바로 그 경우이고,
  거기서 어휘 일치를 요구하면 동의어 하나 없다고 멀쩡한 보드를 버립니다.
- 각 패턴 JSON에 `provides` 필드를 명시했습니다(10개 전부).

측정 결과 — 보드 3건 전부 거부, 벤치 8건 전부 영향 없음:

```
board1   subsystems=[can debug ethernet power_supply rs485 sdcard] -> declined
board6   subsystems=[digital_io ethernet relay rs485]              -> declined
board11  subsystems=[battery can temperature_sensor]               -> declined
bench_can/i2c/uart/relay/led/reg/opamp                             -> 그대로 통과
```

### 2-3. 지정 부품 우선 바인딩 (`agent._pattern_synthesis`)

- 요청이 이름으로 부른 부품의 lib_id를 **모든 role의 후보 맨 앞**에 넣습니다.
  패턴이 고정한 lib_id보다 우선합니다.
- 바인딩이 끝난 뒤, 이름으로 부른 부품 중 **어느 role에도 들어가지 못한 것**이 있으면 패턴을 거부하고
  LLM 경로로 넘깁니다. 조용한 대체보다 폴백이 낫습니다.

효과(테스트로 고정):
- `Si7051` 요청 → 패턴이 기본값 `Si7050-A20` 대신 **`Si7051-A20`을 바인딩**하고 ERC 0으로 완주. (능력 향상)
- `TMP100` 요청 → 핀 구성이 패턴과 맞지 않아 거부 → LLM 폴백. (조용한 대체 제거)

### 2-4. 보고 경로

- `AgentResult.compliance` 추가, `run.json`에 `compliance` 전문과 `result.compliance_ok` 기록.
- `res.ok = pipeline.ok and compliance.ok`. ERC는 통과했지만 요구사항이 어긋나면 `stage="requirement-mismatch"` —
  **회로도 파일은 그대로 존재합니다.**
- `scripts/run_agent.py`가 요청 부품 / 누락 부품 / 각 위반 / 전압을 실제로 검증한 부품 목록을 출력.
- `bench_general.py`·`bench_boards.py`가 `compliance_ok`를 기록하고, 벤치 점수 기준에 포함시켰습니다.
  **이 변경으로 보고되는 점수는 내려갈 것이고, 그것이 목적입니다.**

---

## 3. 측정

- 전체 테스트 **194개 통과 / 189초** (기존 176개 전부 유지 + 신규 18개).
- 골든 회로 4종(MCU 계열)에 전원 무결성 검사를 직접 돌려 **오류 0, 경고 0**.
  알려진 정상 설계에서 오탐이 없고, STM32G474 전압 항목이 실제로 동작함을 확인했습니다.
- 패턴 경로 테스트 5종(opamp·regulator·i2c·can·uart)이 정합성 게이트가 켜진 상태로 그대로 통과 —
  이 보드들의 전원망은 실제로 건전합니다.
- 개념 심볼(`Conceptual:*`)은 PASSIVE 핀만 가지므로 전원 검사에 걸리지 않습니다(오탐 없음 확인).

---

---

## 3-1. 벤치 재측정 (Qwen2.5-Coder-7B, seed 7, 1회)

게이트를 켜고 8케이스를 다시 돌렸습니다(`out/bench_general/gate-v12`). 결과 **4/8**, 그리고 그 안에
**감사 문서가 지목했던 죽은 보드 두 건이 정확히 재현**됐습니다.

| 케이스 | 게이트가 잡은 것 |
|---|---|
| `sensor_i2c` | STM32G474 supply 핀 전부 미연결 (`power_pin_unpowered` ×6) — spec rails가 `[GND]` 뿐 |
| `communication_can` | VDD/VDDA/VBAT가 **+5V** (`supply_over_absolute_maximum` ×6) — spec rails가 `[+5V, GND]` |
| `power_regulator` | **제 변경의 오탐** — 아래 참조 |
| `unknown_module` | 무관: MCU 블록이 `finish_reason=length`로 2회 실패 (기존 7B 한계) |

### 오탐 수정 (A): 요구사항은 프롬프트에서만 나온다

프롬프트는 부품을 지정하지 않았는데 7B가 spec `value`에 **LM2596**을 써넣었고, 그 발명품이
인용된 LDO 패턴을 거부시켰습니다. 이름 지정 부품은 **프롬프트에서만** 읽도록 바꿨습니다.
spec은 LLM의 의역이며, 모델이 지어낸 부품번호가 검증된 패턴을 이기는 건 정확히 거꾸로입니다.
(서브시스템 게이트에 이미 적용했던 원칙과 동일합니다.)

### 진짜 결함 수정 (B): 회로에 들어간 부품이 필요한 레일을 요구한다

패턴은 **자기 MCU를 스스로 주입**하므로 추출된 spec에는 MCU 역할도, 로직 레일도 없을 수 있습니다.
그런데 하위 전원 패스는 전부 `"+3V3" in rails`에 걸려 있어서, 레일이 없으면 통째로 건너뜁니다.
그 결과가 위 두 보드입니다.

`compliance.ensure_device_supply_rails(spec, ir)` — **회로에 실제로 들어간 부품**을 보고,
데이터시트 동작 범위 안에 드는 레일이 하나도 없으면 표준 레일 중 가장 높은 것을 추가합니다.
STM32G474(1.71–3.6 V) → `+3V3`. 기록된 한계가 없는 부품은 건드리지 않습니다.
LLM 경로·블록 경로에도 같이 적용되도록 합성 직후 한 곳에서 호출합니다.

### 재측정 결과 (`gate-v13`, seed 7)

```
passive_led / analog_opamp / power_regulator / sensor_i2c
communication_can / driver_relay / debug_uart        -> 전부 done, ERC 0, compliance True
unknown_module                                        -> functional-completeness (7B 컨텍스트 초과)

release score: 7/8   (compliance 포함 기준)
```

숫자는 이전과 같은 7/8이지만 **의미가 다릅니다.** 이전 7/8에는 전원이 없는 MCU 보드와
절대최대를 넘긴 보드가 "통과"로 들어 있었습니다. 지금은 두 보드 모두 MCU가 실제로 `+3V3`에
물려 있고(IR에서 확인), 전압이 데이터시트 대비 검증된 상태로 통과합니다.

고친 세 케이스는 **다른 시드(21)로 3회 반복 시 9/9** — 패턴 경로 결정성이 유지됩니다.

---

## 4. 이번에 하지 않은 것 (의도적)

- **`unknown_module` 수정.** MCU 블록이 `finish_reason=length`로 죽습니다. 컨텍스트를 줄여 재시도하거나
  실패를 명시적으로 처리하는 별도 작업이며, 이 게이트와 무관한 기존 한계입니다.
- **패턴 lib_id 전면 해제(복구 큐 3번의 나머지).** 지금은 "이름을 부른 부품이 우선하고, 안 되면 거부"까지입니다.
  role이 토폴로지 + 핀 능력만 들고 부품은 요구사항에서 오는 구조는 별도 작업입니다.
- **repair 루프의 completeness 우회(`completeness = [] if ctx.get("pattern")`).** 그대로 둡니다.
  이름으로 부른 부품이 repair 중 사라지면 최종 게이트가 잡지만, 일반 role이 사라지는 경우는 잡지 못합니다.
  이름 기반 게이트는 오탐으로 멀쩡한 run을 죽인 이력이 있어 별도 설계가 필요합니다.
- **eval 하니스 수정(4번)·홀드아웃(5번).** `contract_ok` 동어반복과 무시드 단일 샘플 문제는 그대로입니다.
- **device_limits 확장.** 현재 STM32G474 1건뿐입니다. 다른 부품은 구조 검사만 받습니다.
  확장 시 반드시 저장소 내 데이터시트에서 읽고 인용을 남겨야 합니다.

## 5. 지켜볼 것

- `mark_documented_no_connects`가 PWRIN 핀을 NC 처리하는 부품이 나오면 `power_pin_unpowered` 오탐이 됩니다.
  현재 테스트 범위에서는 발생하지 않았습니다.
- 서브시스템 어휘는 프롬프트 표현에 의존합니다. 새 표현이 나오면 누락 방향(= 패턴 유지)으로 실패하므로
  안전하지만, 보드가 조각으로 답해지는 사례가 다시 보이면 어휘를 늘려야 합니다.
