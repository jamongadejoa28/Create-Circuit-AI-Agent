# Codex 진행 기록 — I²C 실행 패턴과 범용 MCU 정규화 수정

> 이 문서는 `claude-session-handover-2026-08-09.md` 이후 Codex가 수행한 작업만 기록한다.

## 결과

- `sensor_i2c`를 자유 LLM 합성에서 검증된 `CircuitPattern` 고속 경로로 전환했다.
- Qwen2.5-Coder-7B 실서버 벤치에서 KiCad ERC 0, IR↔KiCad 넷리스트 일치,
  시각 이슈 0으로 통과했다.
- 전체 회귀 테스트는 165개가 통과했고, 의미 없는 판정 테스트 1개를 정리한
  최종 테스트 집합은 164개다(나머지 코드는 동일).

실측 산출물:

- `out/bench_general/pattern-fastpath-v9-i2c.jsonl`
- `out/bench_general/pattern-fastpath-v9-i2c/sensor_i2c-r1/`

## 제품 코드 변경

### 1. 허브 부품의 부분 패턴 바인딩

`CircuitPattern.roles.<role>.allow_unbound_pins`를 추가했다. MCU처럼 핀이 많은
허브 부품만 명시적으로 사용할 수 있다. 패턴이 사용한 핀을 제외한 가시 핀은
NC로 닫고, 이후 검증된 기기 규칙이 전원·리셋·부트·디버그 핀을 해당 넷으로
옮긴다. 수동소자나 일반 IC에 이 옵션을 쓰면 패턴 검증이 거부된다.

### 2. I²C 온도 센서 실행 패턴

`data/patterns/i2c_temperature_sensor.json`은 다음 토폴로지를 직접 IR로 만든다.

- STM32G474RETx의 검증된 I2C1 핀: PB9/SDA, PA15/SCL
- Si7050-A20의 SDA/SCL/VDD/GND
- SDA와 SCL 각각 하나의 10 kΩ 풀업
- 센서 로컬 100 nF 디커플링
- 기존 STM32G4 규칙이 추가하는 전원 디커플링, VDDA 비드, NRST, BOOT0, SWD

출처는 ERC 0 골든 회로 `goldens.py::golden3_mcu_i2c_ir`와 지식 항목
`pattern_i2c_sensor_pullups`이다.

### 3. 범용성 결함 수정

기존 파이프라인은 STM32G474가 들어간 모든 회로에 BLDC FOC 고정 핀맵을
적용했다. 이 때문에 단순 I²C 회로에도 PWM/SPI/CAN 단일핀 넷이 생성되어 ERC가
실패했다. 이제 승인된 요구사항에 BLDC/FOC/모터 제어 의도가 있을 때만
`apply_stm32g474ret6_foc_pinmap`을 실행한다.

## 검증 계약

- 패턴 스키마가 잘못된 `allow_unbound_pins` 사용을 거부한다.
- 실제 KiCad 심볼에서 MCU 및 센서 핀 번호가 각각 62/51, 1/6/5/2로 바인딩된다.
- 에이전트 통합 테스트는 LLM 호출이 요구사항 추출 1회뿐임을 확인한다.
- 최종 회로에서 MCU와 센서가 SDA/SCL 두 신호 넷을 공유하고 각 넷에 전원
  풀업이 존재하는지 확인한다.
- KiCad CLI ERC와 넷리스트 왕복 비교를 모두 통과해야 한다.

## 다음 우선순위

1. CAN 트랜시버 + 선택형 120 Ω 종단 + TVS + 커넥터를 하나의 인용 가능한
   실행 패턴으로 편입한다. 현재 `ensure_canfd_bus_protection`의 토폴로지는 있으나
   CAN 물리계층의 외부 검증 출처를 지식 항목으로 먼저 확보해야 한다.
2. `debug_uart`용 일반 MCU 최소회로 패턴을 만든다. MCU 선택 정책을 현대 계열,
   디버그 인터페이스, 공급전압, 패키지 가용성으로 점수화해 68HC12/68000 선택을
   방지한다.
3. 일반 벤치 전체를 동일 seed로 재실행해 기존 4/8에서 I²C가 추가된 5/8 이상인지
   확인한다.
