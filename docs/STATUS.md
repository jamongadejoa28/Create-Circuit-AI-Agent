# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-17 · I2C 버스 가로지르는 C는 그 소자의 V+/GND로.

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json`.
실행기: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정: `ko-step-017-i2c-af-s2` (6번까지, seed 2). 직전: `ko-step-016-i2c-hub-s2`.

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
같은 정의)으로 고르고, 핀은 `pinfunctions`가 DS12288 Table 12에서 고른
I2C*_SDA/SCL이다. 자유 GPIO 라운드로빈은 PC13/PC14를 버스에 올렸다.
표에 없는 허브(ESP32 모듈)는 GPIO를 지어내지 않고 보고한다. 이미 버스에
있는 허브 핀이 기록된 AF가 아니면 옮기고 NC로 둔다. PB6/PB7 리터럴 없음.
패스는 `_normalize` 안(전사 모드 제외). NC는 점유가 아니다
(`wire_mcu_interfaces`와 같음).

`Capacitor:Cap_0603`은 Device:C가 FTS 히트가 아니라 `CAP006DG`가 됐다.
전사 모드는 이미 Device:R/C/L을 쓴다. 설계 모드는 핀이 맞으면 그 세
제네릭을 접두어와 무관하게 후보에 넣고, 핀 수 적은 쪽을 접두어보다
앞세운다. U1 Capacitor:Cap_0603도 Device:C (CAP006DG 8핀이 짐). 핀 8을
쓴 C1은 Device:C가 못 받아 거부. 라이브러리명 세 단어 목록은 넣었다가
삭제했다.

지식 토픽: `search_query`가 공백 없는 partish 토큰일 때만 (TMP100,
STM32G474RET6). `resistor`와 `connections_intent` 문장은 넣지 않는다.

### 016 seed 2 6번 회로 사실 (`ko-step-016-i2c-hub-s2`)

실행기 exit 3은 2·5번 vs 015 회귀 카운트. 1–5번을 고치러 가지 않는다.

- 선정 부품 있음. C1–C6는 로그 `Capacitor:Cap_0603 -> Device:C`. CAP006DG 없음.
- 로그 `wired U1.2 to dangling interface net SCL`, `wired U1.3 … SDA`.
  SDA: J1.2, R3.1, U1.3, U2.3, U2.6. SCL: J1.3, R4.1, U1.2, U2.1.
  U1이 버스에 있다. 핀 번호 2·3은 PC13/PC14 — I2C AF 아님.
- U2.4 V+ = +3V3, U2.2 GND. ADD0(U2.5)=GND. **ADD1(U2.3)이 SDA 위** —
  주소 핀이 데이터 버스에 있다.
- J1 1=+3V3 / 2=SDA / 3=SCL / 4=GND. R3·R4 10k 버스 풀업.
  R1·R2 4.7k는 또 +3V3–GND만. knowledge 주입 tmp100 세 개. 토픽에 `resistor` 없음.
- stage=`done`, self-ERC 0, kicad 0, compliance error 0, dead 없음.
  `wired_ratio` 0.5. 점수 자랑으로 쓰지 않는다.

### 017 seed 2 6번 회로 사실 (`ko-step-017-i2c-af-s2`)

실행기 exit 3은 1·2·4·5·6번 vs 016 회귀 카운트. 1–5번을 고치러 가지 않는다.
6번의 점수 변화도 자랑하지 않는다.

- 선정 부품 있음 (STM32G474RET6, TMP100). C는 Device:C. CAP006DG 없음.
- 로그 `moved U1.26 off I2C net SDA` (PB2), `wired U1.50 … I2C1_SDA -> PA14`
  (DS12288 Table 12). `moved U1.25 off SCL` (PB1), `wired U1.49 … I2C1_SCL -> PA13`.
  최종 SDA: U4.6, J1.SDA, **U1.50**, R1.2, C1.1, R3.1.
  최종 SCL: U4.1, J1.SCL, R4.1, **U1.49**, R2.2, C1.2.
- U4.4 V+ = +3V3, U4.2 GND. ADD0(U4.5)·ADD1(U4.3)=GND. 주소 핀 특례 없음.
- J1이 핀 **이름**(SDA/SCL/VDD/GND)으로 달려 숫자 1–4는 넷 없음 —
  compliance `J1 … pin 1, 2, 3, 4 is on no net`. I2C_VDD 넷에 R1.1/R2.1/C1.3.
  C1은 Device:C인데 핀 1–4를 씀. R3·R4 10k는 +3V3 풀업.
- stage=`repair-2`, self-ERC 12, kicad 6, compliance error 1.
  지식 토픽: spec summary 문장 + `Conn_01x04`. `connections_intent` 없음.

### 익명 헤더 핀 토큰이 숫자 접점이 되지 않던 것

`Conn_01x04` 핀 이름은 `Pin_1`..`Pin_4`다. 모델이 넷 역할을 핀 id로 쓰면
(`J1.SDA`) `resolve_pin_names`의 이름 일치도, 리페어 게이트의 핀 존재 검사도
실패해서 패드 1–4가 비고 헤더가 죽는다. 고친 정의는 심볼 사실이다: 보이는
핀의 이름이 번호(또는 `Pin_N`/`~`)뿐이면 남은 번호가 그 접점이다. 정규화와
게이트가 `anonymous_header_contact`를 공유한다. SDA→2 표는 없다. USB-C처럼
이름 있는 접점은 그대로 이름 매칭. Device:R도 이름이 비어 익명이지만
헤더가 아니다 — geometry 검사기와 같은 `_header_like_component`(KiCad
prefix J / `conn_` / pinheader 풋프린트). 이미 그 넷에 숫자 접점이 있으면
리페어 `connect J1.SDA`는 빈 번호를 새로 쓰지 않고 그 접점을 다시 쓴다.

### I2C 역할이 넷 멤버십만 보던 것

`role_jobs_done`은 전도만 봤다. MCU GPIO가 SDA에 있어도 부품은 살아 있어
`role_working`이 올랐다. 검사 `erc.i2c_hub_af_failures`는 조인과 같은 네 사실
(`is_i2c_net`, `i2c_line_role`, `hub_ref`, `pin_carries_function_ending`)을
쓴다. 표가 없으면 침묵(ESP32). 016 IR: U1.3 SDA·U1.2 SCL →
`i2c_hub_pin_not_recorded` error, compliance not ok. MCU 역할은
`part_present`(RET6↔RETx)로 U1에 붙고 `role_working`이 떨어진다. 017 IR:
AF 핀이라 이 규칙은 0건.

### SPI가 넷 라벨도 기록된 클럭도 없이 허브 GPIO에 있던 것

I2C와 같은 구멍이다. MCU 표는 DS12288 Table 12의
`SPI*_SCK`/`MOSI`/`MISO`/`NSS`. Table 12 추출이 pdf index 60부터라
PA5(SPI1_SCK)가 JSON에 없었다 — 올바른 PA5 배선이 GPIO로 오판됐을
자리라 추출을 56–71로 넓혔다. SCK/MOSI/MISO는 기록된 AF가 아니면
`spi_hub_pin_not_recorded`. CS/NSS는 GPIO여도 된다(소프트웨어 칩 선택).
WP/HOLD 특례와 W25Q 핀 번호 목록은 없다. 표가 없으면 침묵(ESP32).

핀 이름 별칭 CLK/DI/DO/CS를 전 심볼에 쓰던 것은 지웠다. 4017 CLK와
W25Q CLK가 같은 토큰이라, FOO 넷에서 조인이 PC13을 PA5로 옮겼다.
저장소에 W25Q 데이터시트가 없어 플래시 표는 만들지 않았다. 이름 없는
넷의 W25Q CLK는 SCK가 아니다. MCU가 아직 없고 넷이 SCK이면 라벨이
버스다. MCU가 이미 SCK에 있으면 라벨은 증거가 아니다 — 25LC처럼 핀
이름이 SCK일 때만 조인이 GPIO를 AF로 옮긴다.

### 넷 라벨만으로 SPI/I2C를 정하던 것

같은 토폴로지(STM32 GPIO + 저항)인데 넷 이름이 `FOO`이면 버스가 아니고
`SCK`/`SDA`이면 버스였다. 조인이 PC13을 PA5로 옮겼다. ESP32 IO21은 핀
이름도 AF 표도 없어서, 넷 이름 `SDA`가 풀업 검사의 근거였다.

한 정의(`i2c_line_role` / `spi_line_role`): 멤버 핀 이름이 Table 12
접미사(SDA/SCL, SCK/MOSI/MISO/NSS) → 기록된 AF → 넷 라벨은 **그 넷에
`device_for` 부품이 없을 때만**. 핀 수 `<=2` 게이트는 없음 — Conn_01x02와
Conn_01x03이 같은 넷 멤버십인데 갈렸다. MCU가 멤버이면 4017 CLK on `SCK`도
버스가 아니다. TMP100 `U1.6` on `BUS_A`와 25LC `SCK` on `FOO`는 핀 이름.
PA5 on `FOO`는 Table 12. 헤더+풀업만 있는 `SDA`와 MCU가 아직 없는 `SCK`는
라벨. 표 없는 ESP32 `SDA`는 라벨.

### 심볼에 없는 핀이 넷 멤버로 남던 것

017 C1은 Device:C인데 핀 1–4가 넷에 있었다 (SDA/SCL/I2C_VDD/GND).
R1·R2도 Device:R 핀 3이 GND에 있었다. `resolve_unknown_symbols`는
이미 카탈로그에 있는 lib_id는 핀을 다시 안 본다. 리페어 게이트
`absent_pin`은 새 op만 막는다. 합성 IR의 유령 핀은 ERC `unknown_pin`과
같은 사실(`SymbolDef.has_pin`)로 넷에서 지운다. 익명 헤더 토큰
(`J1.SDA`)은 그 전에 숫자 접점이 되므로 지우지 않는다. NC에 있는 익명
헤더 토큰도 같은 정의로 묶는다. 지우지 않는 것: C1.1은 SDA, C1.2는 SCL에
남는다. 유령 핀을 지운 뒤에도 커패시터는 버스 두 선을 잇는다. 그건
지지 회로이고 이 패스의 대상이 아니다.

### 커패시터가 SDA와 SCL을 잇던 것

유령 핀을 지운 017 C1은 Device:C 핀 1=SDA, 핀 2=SCL로 남았다. SBOS231I
Figure 12 (pdf index 18)는 0.01 µF를 **Supply Bypass**로 V+–GND에 그린다.
같은 페이지 §8.2.2는 열원 가까이 두라는 배치이지 바이패스 위치가 아니다.
공급·접지 핀 가까이에 두라는 문장은 §9 / §10.1 (pdf index 20)이다.
`decoupling-cap-per-ic`와 같다. 한 정의 `capacitors_across_i2c_lines`: prefix C가
`i2c_member_role`(핀 이름 SDA/SCL 또는 기록된 AF)인 두 넷을 잇는가.
`two_pin_bridges`와 같은 prefix C다(대소문자 무시) — 버스 양단 저항을 바이패스로 보지
않는다. 심볼 핀 수가 아니라 연결된 넷이 둘인지만 본다. 풀업이 쓰는
`is_i2c_net`의 넷 라벨 폴백은 쓰지 않는다 — 라벨만 SDA/SCL인 555 타이밍 C를
옮기지 않기 위함이다. 옮기는 위치는 그 버스에 있는 IC에서 **가장 많이
묶인** 비접지 PWRIN 넷과 접지 핀 넷이지, 심볼 목록 첫 PWRIN(STM32G474는
VBAT)이나 목록 첫 gnd, 호출자가 넘긴 레일 이름이 아니다. 동점이면
`UNAMBIGUOUS_SUPPLY_NAMES`(VDD/VCC/V+)가 있는 넷. 그래도 동점이면
버스에서만 뗀다. VBAT 이름 특례는 없다. 핀 이름이 SDA/SCL인 소자가 AF만
있는 MCU보다 앞선다. 이름 있는 소자가 둘인데 (공급, 접지) 쌍이 다르면
넷 노드 순서로 고르지 않고 버스에서만 뗀다. 한 C가 두 레일의 바이패스가
될 수 없다. 그 소자의 공급 핀이 아직 넷에 없으면 MCU 쪽 다수 레일로
떨어진다. 세 번째 패드가 버스에 있어도 검사와 수정이 같다 — 두 핀은
레일·GND, 나머지는 NC. SDA와 SCL 멤버 넷이 있으면 세 번째가 레일이어도
버스를 가로지른 것으로 본다. 그 패드가 소자 공급·GND가 아닌 넷(+5V 등)에
있으면 NC해서, 옮긴 뒤 연결 넷이 둘이 되게 한다. 노트와
`two_pin_bridges` 디커플링이 같아야 한다. `two_pin_bridges`는 **연결된 넷이 둘**일
때만이다. 미사용 패드는 세지 않고, 4핀 션트나 SDA까지 닿는 피드스루는
2단자 풀업·디커플링이 아니다. 레일 쌍이 갈려 NC할 때 노트는 “공급 넷이
없다”가 아니라 버스의 I2C 소자들이 한 쌍을 공유하지 않는다고 적는다.
핀 이름이 SDA인 경우로 한정하지 않는다.
SDA–GND 필터 캡과 이미 레일에 있는 디커플링은 그대로.
TMP100 핀 번호 특례는 없다. 남는 것: 표 없는 ESP32만 있고 핀 이름도 SDA가
아닌 보드에서 라벨만 SDA/SCL인 C는 이 패스가 옮기지 않는다. 풀업은 그
라벨을 여전히 버스로 본다. prefix만 다른 2단자(C가 아닌 X)는 커패시터로
보지 않는다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft: I2C 풀업, USB-C sink CC — 승격 금지
- 지식: `data/knowledge/manufacturer-datasheets.json` (provenance `datasheet`, pdf 페이지 인덱스 필수)
- 전압 한도: `data/device_limits.json` — STM32G474, TMP100 (SBOS231I)

## 다음 작업 (규칙 9)

측정된 다음 회로 사실이 생기기 전에는 큐를 비운다. 1–5번 연마·SchGen
승격·QLoRA·UART 8번을 “다음”으로 올리지 않는다.

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
