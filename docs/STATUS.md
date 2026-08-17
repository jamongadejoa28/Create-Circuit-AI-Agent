# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-17 · MCU를 I2C 버스에 올리고 Capacitor:Cap_0603이 8핀 IC가 되지 않게. 1–5번 연마는 멈춤.

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json`.
실행기: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정: `ko-step-016-i2c-hub-s2` (6번까지, seed 2). 직전: `ko-step-015-rail-reach-s2`.

### 리페어 루프가 아무것도 배선하지 않고 있었다

4개 도메인 아티팩트 집계: 리페어 op **115건 중 89건이 "같은 ref를 같은 lib_id로
다시 add_component"** — 게이트가 전량 거부했고, 그 라운드는 IR을 못 바꾸므로
루프는 "same problems twice"로 종료됐다. 즉 미연결 핀을 고치는 라운드가 실제로
핀을 연결한 적이 거의 없었다.

원인 두 가지, 둘 다 회로 지식이 아니라 프롬프트·루프 구조 문제:

- 리페어 프롬프트의 **유일한 예시가 교체**였다("lib_id가 라이브러리에 없으면
  같은 ref로 add_component"). 이 문장은 `unknown_symbol` 문제에만 유효한데
  매 라운드 붙었고, 7B는 다른 문제에도 그대로 복사했다. → 그 문장을
  `unknown_symbol`이 실제로 보고된 라운드로 한정한다. 보드에 넷 없는 핀이
  있을 때만 `REPAIR_PATCH`의 op 어휘를 적는다(넷 없는 핀=`connect`, 열어둘
  핀=`set_nc`). 그 문장을 매 라운드 붙이지 않는다.
- 거부된 라운드는 **모델이 두 번 물어본 라운드가 아니다** — 거부 사유는 모델이
  못 본 정보다. 게이트 사유를 다음 라운드 프롬프트로 넘기고, 같은 문제에 넘길
  새 사유가 없을 때만 종료한다. 종료를 보장하는 것은 기존 `MAX_REPAIRS=3`
  상한이며, 새 조건은 라운드 낭비만 줄인다.

공정 지표(적용/거부 op, 5케이스 합): 006 = **6/37** → 009 seed 1·2·3 =
**50/4, 53/8, 65/5**. 라운드가 실제로 배선을 한다.

제품 지표는 **케이스마다 다르고, 합계로는 판정할 수 없다**(죽은 부품 합
009 = 12 / 2 / 64). 세 시드 모두에서 재현된 것만 적는다:

- 4번(타이머): 죽은 부품 1/6 → **0/6**, compliance error 1 → **0**,
  `role_working` 3 → **4**. 3/3 시드.
- 2번(LDO): self-ERC 28 → **0 / 1 / 0**. 3/3 시드. 죽은 부품은 8/0/8로 갈린다.
- 1번(LED·버튼)·5번(오디오): 시드 간 방향이 갈린다 — 아래 분산 항목.

### 시도했다가 되돌린 것 — 단락 op 거부 게이트

op가 적용되기 시작하자 모델은 "핀이 넷에 없다"에 **그 핀을 GND에 던져** 답했다.
`connect`/`set_nc`가 부품의 모든 핀을 한 넷에 올리면 거부하는 게이트를 넣었고,
캠페인으로 판정한 결과 **삭제**했다. 이유는 점수가 아니라 재현된 결함이다:

- 사망 사유만 바뀌고 생존은 그대로였다. 007: 단락 5개 사망 → 008: 단락 0,
  대신 `pin is on no net` 35개. 008에서 단락 거부된 R5..R16 12개는 전부
  그대로 죽은 부품 목록에 남았다.
- 검사기의 정의를 **공유하지 않고 한 절만 복제**했다(규칙 4). `analyze_conduction`의
  나머지 두 사유(단독 넷 / 넷 없음)는 통과시켰고, 008의 죽은 37개 중 35개가
  바로 그 통과된 사유다.
- op 순서에 의존했다. 같은 수리를 `[R1.2→GND, R1.1→SIG]` 순으로 쓰면 앞 op가
  거부되고, 뒤집어 쓰면 둘 다 통과했다.
- 앞에 `disconnect` 하나를 두면 우회됐다(게이트는 넷 이름과 무관하게 지우고
  `apply_patch`는 이름이 맞을 때만 지운다).

"미연결 핀을 GND에 던진다"는 문제는 **미해결로 남는다**. 고칠 자리는 op를 막는
게이트가 아니라, 모델이 넷을 지어내게 만드는 지점이다.

### e1fef4f 두 번째 검토 (서브에이전트는 API 한도로 실패, 같은 기준으로 재검토)

- STATUS 009 숫자는 아티팩트와 일치한다 (타이머 0/6 3/3, LDO self-ERC 0/1/0, op 50/4·53/8·65/5).
- 단락 게이트는 커밋에 없다. 되돌린 이유가 재현된 결함이라 규칙 8에 맞다.
- **틀린 원인 진술**: "시드도 코드 변경도 아니고… 슬롯/캐시 미확인". 009는 `--seed 1,2,3`이다.
  spec은 세 시드가 같고, 갈리는 단계는 `synthesize_block`이다. 시드 3의 SPEAKER/
  VOLUMECONTROL이 `CIRCUIT_IR.maxItems=30`을 각각 꽉 채웠다 (합쳐서 Device:R 54개).

### 블록이 스키마 30칸을 채우던 것

역할은 세 시드 모두 `speaker` 1개. 잘 나온 템플릿은 6~7부품, 나쁜 템플릿은 정확히 30.

시도했다가 되돌린 것: 스키마 `maxItems`를 핀 수로 8까지 낮춤 (`ko-step-010`).
모델이 8칸을 패시브로 채워 **스피커를 안 넣었다**. 상한을 줄이는 것은 모델에게
역할 장치를 포기하라고 하는 것과 같다.

남긴 것: 합성 후, 템플릿이 핀 수 예산을 넘으면 `topology.analyze_conduction`이
죽었다고 한 부품을 지운다. 요청 역할의 마지막 1개는 안 지운다. 예산 이하면
무동작 — 아직 안 꽂힌 6부품 템플릿을 수리 전에 비우지 않기 위함.
(`_block_component_budget`: `min(30, max(8, pins//2+4))`. 바닥 8은 측정된
정상 템플릿이 4~7부품이라서.)

011 seed 1·3 오디오에서 overflow가 25~26개를 지웠다. 다른 케이스는 한 번도
안 탔다.

### 빠진 스피커·포트를 면제하지 않고 카탈로그에 넣는다

`_restore_passive_roles`가 카탈로그에 있는 패시브를 게이트에서 면제해서, 모델이
Device:R로 템플릿을 채운 뒤 스피커가 보드에 없었다. 일반 저항(prefix `R`)은
그대로 면제한다 — 부유 Device:R는 role_present만 올린다. 레일 커패시터는
기존처럼 레일↔GND. **그 외 카탈로그 패시브**(KiCad prefix RV/LS/L)는 첫
non-`Simulation_*` 후보를 배치한다. 배선은 리페어.

012 seed 1·2·3 오디오 **3/3**: `Device:Speaker`와 `Device:R_Potentiometer`가
보드에 있다. 리페어가 RV1을 IN/OUT/GND, LS1을 OUT/GND에 연결했다. 두 부품은
dead_components에 없다. `role_working` 2~3/7 → **4/7**. 죽은 부품은 C12 단락
하나. `placed dropped` 로그는 오디오에서만 났다.

### 케이스 (012 seed 1/2/3, dead/부품수)

- 1번: 3/5, 5/9, 1/5. place 로그 없음 — 시드 분산.
- 2번: 8/13, 3/13, 4/13.
- 3번: 0/5.
- 4번: 0/6.
- 5번: **1/19, 1/19, 1/19** (011은 1/19, 5/22, 4/18; 009는 3/21, 1/21, 53/69).

### 가짜 심볼이 라디오 IC가 되고, Device:LED가 저항처럼 복제되던 것

1번 보드에서 측정된 구멍 두 개, 둘 다 회로명 특례가 아니다.

- `search_parts("SW")`의 1순위는 `RF_AM_FM:Si4734-D60-GU`(24핀). 핀 1·2가 있어서
  옛 `resolve_unknown_symbols`는 첫 히트에 `Device:SW`를 라디오로 바꿨다.
  012 seed 2: SW1이 그 IC이고, 진짜 `Switch:SW_Push`는 SW2. 고친 순위는
  **기존 ref 접두가 맞고 핀이 가장 적은 후보**. 사용자가 고른 부품은 심볼 이름이
  가짜 이름의 어간이거나 그 반대일 때만 같은 풀에 넣는다 (`SW` ⊂ `SW_Push`,
  `LED` ⊄ `SW`).
- `_limit_main_device_copies`와 리페어 중복 게이트가 **모든 `Device:*`를 면제**해서
  수량 1 LED 역할에 D2가 추가됐다. 012 seed 1·3 리페어: `added D2` 후 양핀을
  `R_LED`에 연결. 면제 범위는 KiCad prefix **R/C/L만** (풀업·직렬 R). LED(prefix D)
  는 IC와 같이 수량·중복 거절. 거절된 add의 배선이 `pending_adds`로 통과하던
  구멍도 막았다.

013 1번 **3/3**: 보드에 `Device:LED` + `Switch:SW_Push`, Si4734 없음, D2 없음.

- seed 2: `dead={}`, `role_working` 4/4, stage=`done`. SW1.1=+5V, SW1.2=R_LED,
  D1·R1이 R_LED↔GND. (직렬 전류제한은 아님 — 도통만 판정.)
- seed 1·3: D2 거절 로그 있음. D1 양핀이 `R_LED`, SW 양단이 같은 전위.
  같은 패치의 `connected D1.2 to R_LED`는 살아남음. 단락 op 게이트는 다시 넣지 않음.

### 타이머 012 dead=0 은 U2 유령 노드였다

012 타이머 3/3: 부품 목록에 U2가 없는데 TRIG/THRES/DISCH/RST/CONT 넷에
`U2.2` 등이 있다. `add U2 (Timer:NE555D)`는 예전부터 중복 거절됐지만
connect는 pending add로 통과해 **없는 부품의 노드**가 전도 상대가 됐다.
013은 그 connect를 거절한다. U1 제어핀은 단독 넷 — 점수는 내려가고 보드와
IR이 같아진다. `apply_patch`도 없는 ref의 connect를 쓰지 않는다.

실행기 regressions(타이머 compliance 0→1, LDO 에러 증가)는 이 정직화와
시드 분산이 섞여 있다. 합계로 되돌리지 않는다.

### 013 검토 (circuit-review 서브에이전트는 사용량 한도, 같은 문서 기준)

- 회로명·LED 케이스 특례 없음. 면제는 KiCad prefix R/C/L (`_restore_passive_roles`와
  같은 분류). 심볼 치환은 카탈로그 핀·접두·요청 부품 어간.
- 단락 op 게이트는 다시 넣지 않음. 008에서 삭제한 이유와 충돌하지 않음.
- 타이머 점수 하락은 유령 U2 노드 제거의 재현이지 회귀로 되돌릴 대상이 아님.
- `_symbol_name_related`는 카탈로그 심볼 이름 어간이지 반례 키워드 목록이 아니다.
  `SW`⊂`SW_Push`, `LED`⊄`SW` 테스트가 있다.

### 013 케이스 (seed 1/2/3, dead/부품수)

- 1번: 2/8, **0/8**, 2/8. 012는 3/5, 5/9, 1/5.
- 2번: 15/20, 7/15, 7/19 — 시드 분산, 이 변경의 로그 없음.
- 3번: 0/9.
- 4번: U1 제어핀 단독 넷 3/3 (위 유령 노드).
- 5번: 2/22, **0/22**, 3/26. seed 2는 stage=`done`·role 7/7 — 원인 미확인
  (이 변경은 오디오 경로를 안 건드림). seed 3는 schematic=None (풋프린트
  Package:Speaker / LEMO2 실패 로그). 스피커·포트는 보드에 있음.

## 전원 핀이 신호망에 있으면 ERC가 0이 되던 것

6번 seed 1 (`ko-step-014-knowledge-rag-s1`): 선정 부품 U1 `STM32G474RETx` +
U2 `TMP100` 있음. J1 1x4는 +3V3/SDA/SCL/GND. R3·R4 10k가 SDA·SCL을 +3V3로
당김. 데이터시트 권장은 5 kΩ — 4.7 kΩ(R1/R2)는 +3V3–GND에만 있고 버스가
아니다. U2.4(V+)가 SCL, U2.3(ADD1)이 SDA, U2.5(ADD0)만 GND. TMP100 전용
바이패스 없음. MCU 쪽 100 nF/10 nF/1 µF/4.7 µF는 기존 DS12288 정규화.
knowledge_trace에 `tmp100-i2c-pullup-and-bypass`, `tmp100-address-pins`,
`stm32g474-vdd-vdda-decoupling` 주입. 7B가 그걸 배선으로 옮기지는 못했다.

U2.4가 SCL 위에 있고, `ensure_pwr_flags`가 SCL에 PWR_FLAG를 붙였다.
self-ERC 0 → 리페어 루프는 `while not pr.ok`라서 한 번도 안 돌았다.
`check_requested_rail_reach`는 그 뒤에야 `power_pin_misses_requested_rail`을
보고했다. 검사기와 수정기가 같은 정의를 공유하지 않은 구멍(규칙 4).
TMP100/케이스 특례로 V+를 옮기지 않는다.

고친 것:

- `ensure_pwr_flags`는 보드 공급 넷(이름 `is_supply`/`is_ground`, 또는
  `power:*` 심볼. PWR_FLAG는 증거로 안 친다)에만 깃발을 단다. 신호망에 이미
  붙은 깃발은 제거한다.
- `detach_supply_pins_from_nonsupply_nets`가 같은
  `check_requested_rail_reach` 기록을 보고, 보드 공급이 아닌 넷에서 PWRIN을
  떼어 미연결로 둔다. 기존 `complete_generic_power_pins`가 인용된 전압 범위가
  있으면 로직 레일에 붙인다. 이미 `+5V` 같은 레일 위에 있으면 안 옮긴다
  (`test_generic_completion_never_overrides_an_existing_connection`과 같음).
- `UNAMBIGUOUS_SUPPLY_NAMES`에 단독 `V+`를 넣는다. V+/V- 쌍은 이름 두 개라
  기존처럼 거부.
- `data/device_limits.json`에 TMP100 2.7–5.5 V / abs 7.5 V
  (SBOS231I §6.3·§6.1, pdf 페이지 인덱스 3). STM32 항목의 `file`은 실제 파일명
  `stm32g474xB-xC-xE_DS12288_rev6.pdf`로 맞춤.
- ERC가 깨끗해도 compliance 에러가 있으면 리페어가 돌아간다. 합성에서 모은
  KNOWLEDGE 스니펫을 리페어 프롬프트에도 넣는다.

### 015 seed 2 6번 회로 사실 (`ko-step-015-rail-reach-s2`)

실행기 exit 3은 1·2·4·5번 vs 014 baseline 회귀 카운트다. 1–5번을 고치러 가지 않는다.

- 선정 부품 보드에 있음: U1 `MCU_ST_STM32G4:STM32G474RETx`, U2 `Sensor_Temperature:TMP100`.
- U2.4(V+)는 **+3V3**, U2.2 GND. `supply_rail_reach_mismatches` 0.
  로그에 `disconnected U2.4 from SCL`이 없다 — 이 시드는 모델이 V+를 레일에
  둔 것으로 본다. detach 패스가 이 보드에서 발동했다고 주장하지 않는다.
  PWR_FLAG는 +3V3·GND에만 있다 (SCL 없음).
- U2.1 SCL, U2.6 SDA, J1 1=+3V3 / 2=SDA / 3=SCL / 4=GND.
  ADD1(U2.3)·ADD0(U2.5)는 둘 다 GND (SBOS231I Table 2의 1001000).
- **SDA/SCL 넷에 U1 핀이 없다.** TMP100·헤더·10k 풀업(R3/R4)만 버스에 있다.
  MCU는 전원만 연결. `role_working` 4/4는 이 사실과 별개다.
- R1/R2 4.7k는 또 +3V3–GND에만 있다 (버스 풀업 아님).
- 모델이 `Capacitor:Cap_0603`을 썼고 `resolve_unknown_symbols`가
  `Power_Management:CAP006DG`(8핀)로 바꿨다. 리페어가 그 핀을 GND/+3V3에
  던졌다. C5.8·C6.4/5/7/8은 여전히 미연결 → compliance
  `component_does_no_work` 2건, stage=`repair-3`, self-ERC 6, kicad 5.
  단락 op 게이트는 다시 넣지 않는다.
- knowledge 주입: `tmp100-i2c-pullup-and-bypass`, `tmp100-address-pins`,
  `stm32g474-vdd-vdda-decoupling`. `resistor-e24-series`/`e12-series`도
  슬롯을 쓴다 (토픽 `resistor`).

### MCU가 I2C에 없고 커패시터가 8핀 IC가 되던 것

`wire_mcu_interfaces`는 블록 머지 뒤에만 돌았다. 역할 4개인 I2C 보드는
`BLOCK_THRESHOLD=5` 아래라 단일 합성이고, SDA는 센서·헤더·풀업 세 멤버라
`alone`(1핀 넷)에도 안 걸렸다. 허브 연결은 `erc.is_i2c_net`(풀업 검사기와
같은 정의)으로 카탈로그를 만들고 `extra_alone=False`로 기존 패스를 호출한다.
어떤 GPIO가 I2C1_SDA인지는 AF 표 문제라 이 패스가 답하지 않는다. PB6/PB7
특례 없음.

`Capacitor:Cap_0603`은 Device:C가 FTS 히트가 아니라 `CAP006DG`가 됐다.
전사 모드는 이미 IEEE 315 R/C/L을 Device:R/C/L에 묶는다. 설계 모드
`resolve_unknown_symbols`에도 같은 제네릭을 후보로 넣는다. 핀 8을 쓴 C1은
Device:C가 못 받아 거부.

지식 토픽: `search_query`가 공백 없는 partish 토큰일 때만 (TMP100,
STM32G474RET6). `resistor`는 넣지 않는다.

### 016 seed 2 6번 회로 사실 (`ko-step-016-i2c-hub-s2`)

실행기 exit 3은 2·5번 vs 015 회귀 카운트. 1–5번을 고치러 가지 않는다.

- 선정 부품 있음. C1–C6는 로그 `Capacitor:Cap_0603 -> Device:C`. CAP006DG 없음.
- 로그 `wired U1.2 to dangling interface net SCL`, `wired U1.3 … SDA`.
  SDA: J1.2, R3.1, U1.3, U2.3, U2.6. SCL: J1.3, R4.1, U1.2, U2.1.
  U1이 버스에 있다. 핀 번호 2·3이 I2C AF인지는 이 패스가 보장하지 않는다.
- U2.4 V+ = +3V3, U2.2 GND. ADD0(U2.5)=GND. **ADD1(U2.3)이 SDA 위** —
  주소 핀이 데이터 버스에 있다.
- J1 1=+3V3 / 2=SDA / 3=SCL / 4=GND. R3·R4 10k 버스 풀업.
  R1·R2 4.7k는 또 +3V3–GND만. knowledge 주입 tmp100 세 개. 토픽에 `resistor` 없음.
- stage=`done`, self-ERC 0, kicad 0, compliance error 0, dead 없음.
  `wired_ratio` 0.5. 점수 자랑으로 쓰지 않는다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft: I2C 풀업, USB-C sink CC — 승격 금지
- 지식: `data/knowledge/manufacturer-datasheets.json` (provenance `datasheet`, pdf 페이지 인덱스 필수)
- 전압 한도: `data/device_limits.json` — STM32G474, TMP100 (SBOS231I)

## 다음 작업 (규칙 9)

1. 6번 seed 2에서 MCU는 SDA/SCL에 있다. 남은 전기 사실: ADD1이 SDA에 있다.
   TMP100 주소 핀 특례로 고치지 않는다.
2. 다음 측정: seed 3, 또는 7번(W25Q32JVSS SPI). SPI는 `is_i2c_net` 같은
   공유 검사기가 없다.
3. `connections_intent` 문장 전체가 지식 토픽으로 남는다. 1–5번 연마 금지.
4. SchGen 격리, QLoRA 없음.

## 데이터·학습

제조사 PDF는 `data/datasheets/` (2026-08-17 추가: NE555 SLFS022K, LM386 SNAS545D,
MCP6001 DS20001733L, TMP100 SBOS231I, AMS1117 ds1117, STM32F103 DS5319 Rev 18,
STM32H743 DS12110 Rev 10, AN2867 Rev 9). st.com 직접 수신은 타임아웃이라
Farnell/동일 ST 문서 사본. Keil 995쪽 파일은 RM0008이라 버렸다.

QLoRA 없음. SchGen accepted **0**, 승격 금지.

## 하지 않을 일

- ERC/벤치 점수 자랑 · 패턴 apply_when · SchGen 승격 · 회로명 특례
- 반례 키워드로 패시브 클래스 추측 · 부유 부품으로 role_present 부풀리기
- 1–5번 시퀀셜을 기본 작업 큐로 되돌리기
- 단락 op 게이트 재도입 (008에서 삭제한 이유)
