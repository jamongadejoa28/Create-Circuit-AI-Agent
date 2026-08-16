# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-16 · 워킹 트리 dirty (설계 측정·웹 리포트·전사 스위트·SchGen 결합 도구)

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json` (36개 / 30 도메인).
실행기: `tests/benchmarks/run_sequential_campaign.py`.

- 생성·이미지 검토가 끝난 범위: **1~3번**
  (`tests/artifacts/benchmarks/sequential/ko-step-003-header-geometry/`)
- 1번 `전원_엘이디_버튼`: 요청 1x2 → 심볼 `Conn_01x02` 핀 1·2. 접점 계약 일치.
  선택 부품(LED, 택트)은 보드에 있음. D1이 단락되어 역할 3/4. ERC 0.
- 2번 `선형전원_삼점삼볼트`: J1·J2 모두 `Conn_01x02`. AMS1117-3.3는 보드에 있음.
  U2 핀 3(VI)이 레일에 안 붙고 커패시터·LED·저항 다수가 부유. 역할 1/5.
  **AMS1117 특례로 고치지 않음.** ERC 28은 마지막 지표.
- 3번 `비반전_연산증폭기`: MCP6001 보존, J1/J2가 1x2, 단계 `done`. 역할 3/7
  (피드백·바이패스 역할은 이름만으로 검증 불가). 육안상 비반전 되먹임은 그려져 있음.

커넥터 접점 계약은 `compliance.check_connector_geometry` (수량 포함).
합격 게이트로 ERC를 쓰지 않는다.

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft (합성에 안 넣음): I2C 풀업, USB-C sink CC

## 다음 지표 공백

1. ~~커넥터 요청 행·열·접점 수 vs 심볼 핀 수 vs footprint pad 수~~ — 측정·보고 있음.
   `_gather`는 요청 기하가 있으면 그 카탈로그 심볼만 후보로 둔다. 없으면 4핀을 발명하지 않는다.
2. **전원 핀이 요청 레일에 전도되는가** (여러 전원 회로군에서 같은 지표로. 부품명 규칙 금지)
3. 자동 visual 0 ≠ 육안 합격
4. 설계 모드 1..N을 4번 이후로 넓힐 때, 생성기 변경은 **두 도메인 이상에서 같은 지표가 깨질 때만**

생성기 변경 후 항상 `run_sequential_campaign.py --step N`으로 1..N 재실행.
회로명·프롬프트 부분문자열로 코드를 쓰지 않는다.

## 데이터·학습 게이트

SchGen 128 홀드아웃 (`tests/artifacts/datasets/schgen-full-2026-08-16/`):
결합 14, 왕복 6, 렌더 14, **accepted 0**. 사람 전기 검토·라이선스 검토 남음.
candidate를 학습/패턴/`data/`로 승격하지 않는다. QLoRA 금지.

## 하지 않을 일

- ERC 숫자나 벤치 점수를 개선 증거로 쓰기
- RequirementSpec에 빈 물리 필드를 넓혀 모델이 패키지를 환각하게 하기
- `data/patterns/`에 캠페인 문구 `apply_when` 추가
- 실패할 때마다 데이터시트 추가
- SchGen candidate를 학습 정답·제품 규칙으로 승격 (accepted 0)
- 새 `docs/*-plan-*.md` 작성
