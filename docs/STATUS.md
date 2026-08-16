# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-16 · HEAD `a7b5b28` + dirty (`supply_rail_reach` 측정 WIP)

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json` (36개 / 30 도메인).
실행기: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정: `ko-step-005-supply-rail-reach` (seed 1, baseline `ko-step-003-header-geometry`).
점수 변화로 개선을 주장하지 않는다. HEAD는 여전히 dirty — 아래는 그 아티팩트 + 이후
측정 코드 수정 상태를 정직하게 적은 것이다.

- 1번 `전원_엘이디_버튼`: 접점 1x2 일치. `supply_rail_reach` 비어 있음(보드에 PWRIN
  장치 없음). 역할 3/4. `compliance_errors` 1→2 — 일부는 이 체크가 추출기 환각 레일
  `+2V`(LED Vf 패러프레이즈)를 `requested_rail_absent` **error**로 올린 탓.
  이후 코드는 보드 전원처럼 보이지 않는 레일을 **warning**으로 내리도록 고침(재측정 전).
- 2번 `선형전원_삼점삼볼트` (전원·LDO 도메인): `supply_rail_reach_mismatches=2`.
  VI가 GND에 붙어 있고, GND 핀이 OUT에 있음. **요청 +5V/+3V3에 전도되지 않음.**
  두 번째 1x2 헤더 부재(형상 mismatch 1). AMS1117 특례로 고치지 않음 — 유지.
- 3번 `비반전_연산증폭기` (아날로그 도메인): V+→+3V3 일치. 추출기가 `+0V25` 레일을
  발명함. V- 이름 목록(`_is_return_or_ground_pin`)으로 GND를 자동 인정한 것은
  과적합이라 **되돌림** — 접지 도달은 `is_ground_pin`만 사용. 아티팩트
  `report.json`은 여전히 구코드 기준 `mismatches=1`(V-→GND). 캠페인을 이 수정으로
  다시 돌리기 전에는 문서와 아티팩트가 어긋날 수 있다.
- 4번 `타이머_발진_엘이디`: `supply_rail_reach`가 빈 이유는 **원인 미확인이 아님**.
  보드 IC가 `Conceptual:NE555D`라 카탈로그 PWRIN이 없어 레코드를 만들 수 없음.
  `selected_parts` 문자열 매치로는 NE555D가 보드에 있는 것처럼 보일 수 있음.
  이후 코드는 이 경우 `supply_rail_reach_unverifiable` warning을 냄.
- 5번 `오디오_소신호_증폭` (오디오 전원 도메인): 아티팩트상 LM386 V+→+9V·GND→GND
  `mismatches=0`. **이 0은 도메인이 건강하다는 증거가 아니다** —
  `wired_ratio=0.0`, 역할 1/7, U1(LM386)이 dead. 라벨 조인이라 배선 0에서도
  초록이 나온다. 이 0을 근거로 생성기 작업을 미루지 않는다.

커넥터 접점 계약: `compliance.check_connector_geometry`.
전원 레일 전도 계약: `compliance.check_requested_rail_reach` → `supply_rail_reach`
(측정만; 생성기 특례 없음).
합격 게이트로 ERC를 쓰지 않는다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft (`status=draft`, 합성에 안 넣음): I2C 풀업, USB-C sink CC.
  **git에 이미 있음**(`status=draft`로 추적). "git에 없음"이 아님.
  USB-C 780쪽 인용은 TVS로 검증 실패. 승격하지 않는다.

## 다음 작업 (규칙 9 순서)

1. ~~커넥터 접점 형상~~ — 측정·보고 있음.
2. **`conceptual_part_unresolved` / `wired_ratio=0` 죽은 보드** — 약속 1·2번이
   깨진 자리(4번 Conceptual IC, 5번 전선 0). 레일 지표 튜닝보다 먼저.
3. 전원 핀 → 요청 레일 전도 — 측정은 있음. **여전히 측정만.** LDO 핀→레일 miss는
   실재; 환각 레일·Conceptual 침묵·V- 이름 목록은 측정 쪽을 고쳤다.
4. 자동 visual 0 ≠ 육안 합격
5. 설계 모드 1..N 확장 시에도 지표 반복이 생성기 변경 조건

캠페인/파이프라인 SVG는 옆에 `.png`도 만들고, 라벨 디렉터리의 `previews/`에 모은다.
에디터·채팅 미리보기는 PNG를 연다. SVG는 웹 브라우저용이다 (`pip install -e ".[preview]"` → cairosvg).

## 데이터·학습 게이트

SchGen 128 홀드아웃: 결합 14, 왕복 6, 렌더 14, **accepted 0**. QLoRA 금지.

## 하지 않을 일

- ERC 숫자나 벤치 점수를 개선 증거로 쓰기
- RequirementSpec에 빈 물리 필드를 넓혀 모델이 패키지를 환각하게 하기
- `data/patterns/`에 캠페인 문구 `apply_when` 추가
- 실패할 때마다 데이터시트 추가
- SchGen candidate를 학습 정답·제품 규칙으로 승격 (accepted 0)
- 새 `docs/*-plan-*.md` 작성
- AMS1117·MCP6001 등 회로명 특례로 생성기 고치기
