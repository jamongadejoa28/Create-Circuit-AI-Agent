# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-17 · 제조사 데이터시트 RAG. 1–5번 캠페인 연마는 멈춤.

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json`.
실행기: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정: `ko-step-013-symbol-qty-s{1,2,3}`. 직전: `ko-step-012-place-passives-s{1,2,3}`.
010은 스키마 상한 8 시도(되돌림). 기준: `ko-step-006-catalog-bind`.
점수 변화로 개선을 주장하지 않는다.

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

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft: I2C 풀업, USB-C sink CC — 승격 금지
- 지식: `data/knowledge/manufacturer-datasheets.json` (provenance `datasheet`, pdf 페이지 인덱스 필수)

## 다음 작업 (규칙 9)

1. RAG 검색은 붙는다 (`ko-step-014-knowledge-rag-s1` 6번 knowledge_trace:
   `tmp100-i2c-pullup-and-bypass`, `tmp100-address-pins`,
   `stm32g474-vdd-vdda-decoupling` 주입). 7B가 그걸 배선으로 옮기지는 못했다.
2. 6번 seed 1 회로 사실: 선정 부품 U1 `STM32G474RETx` + U2 `TMP100` 있음.
   J1 1x4는 +3V3/SDA/SCL/GND. R3·R4 10k가 SDA·SCL을 +3V3로 당김.
   데이터시트 권장은 5 kΩ — 4.7 kΩ(R1/R2)는 +3V3–GND에만 있고 버스가 아니다.
   U2.4(V+)가 SCL, U2.3(ADD1)이 SDA, U2.5(ADD0)만 GND. TMP100 전용 바이패스 없음.
   MCU 쪽 100 nF/10 nF/1 µF/4.7 µF는 기존 DS12288 정규화.
3. 다음 측정은 seed 2·3의 6번, 또는 7번 이후. V+를 SCL에 올린 것을 케이스
   특례 코드로 고치지 않는다. 1–5번 연마로 돌아가지 않는다.
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
