# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-16 · catalog bind (exact unit0 단일유닛, full Lib:Id, Conceptual≠present)

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json`.
실행기: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정: `ko-step-006-catalog-bind` (seed 1; baseline `ko-step-003-header-geometry`).
직전 비교 기준은 `ko-step-005-supply-rail-reach` 아티팩트도 본다.
점수 변화로 개선을 주장하지 않는다.

### 이번에 고친 측정·바인딩 (회로 단위)

- `Timer:NE555D`: unit0 전원 핀 + **unit_count=1**만 exact 허용. 다중 유닛
  unit0_mix(4093 등)는 exact에 안 넣음 — 에미터 `unit0_pins_unsupported`.
- `Switch:SW_Push` 등 전체 `Library:Symbol` ID: FTS가 lib_id를 UNINDEXED라
  후보 0건이던 구멍을 `exact_lib_id`로 검색에 포함.
- `Conceptual:*`는 `part_present`/selected_parts에 안 잡힘 (005의 NE555D 거짓 초록).
- 값-only `search_query`(10kΩ): `value`로 옮기고 **역할 문자열**로 재검색.
  동의어 목록 없음.
- 빠진 패시브: C-only 후보면 레일↔GND에 디커플링 복원(멱등). R/RV/LS는
  **부유로 넣지 않고** 게이트면제 — role_present만 올리는 죽은 부품 금지.

### 케이스 (006 재측정; 005 대비)

- 1번: selected `Device:LED`+`Switch:SW_Push` 유지, 시트 생성. role_working은
  시드/추출 분산 가능 — 존재 약속은 유지.
- 4번: `Timer:NE555D` 실바인딩, Conceptual 없음. 역할·배선 품질은 별개 지표.
- 5번: LM386 유지. pot/speaker는 카탈로그 후보·면제 경로(부유 Device 배치 안 함).
  `role_working` 낮음·죽은 부품은 **다음 작업**.
- 2·3번: 레일 전도 miss는 측정만.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft: I2C 풀업, USB-C sink CC — 승격 금지

## 다음 작업 (규칙 9)

1. ~~커넥터 접점 / 카탈로그→Conceptual 거짓 / Lib:Id 검색 구멍~~
2. **역할이 전기적으로 일함** (`role_job_done`, `dead_components`) — 특히 오디오·
   타이머에서 부품은 있는데 핀·레일이 틀린 자리
3. 전원 핀→요청 레일 — 측정만; 도메인 반복 시 일반 수정(회로명 특례 금지)
4. visual 0 ≠ 육안 합격

## 데이터·학습

SchGen accepted **0**. QLoRA 금지.

## 하지 않을 일

- ERC/벤치 점수 자랑 · 패턴 apply_when · SchGen 승격 · 회로명 특례
- 반례 키워드로 패시브 클래스 추측 · 부유 부품으로 role_present 부풀리기
