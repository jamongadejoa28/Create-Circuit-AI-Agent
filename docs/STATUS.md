# 현황 — 이 파일만 다음 작업을 적는다

> 다른 `docs/*.md`는 역사 기록이다. 새 계획 파일을 만들지 않는다.
> 판정 기준: [`working-rules.md`](working-rules.md).

갱신: 2026-08-16 · HEAD `917e374` + dirty (순차 캠페인·pin roles·SchGen 도구가 워킹 트리에 있음)

## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 검증 범위

고정 입력: `tests/eval/sequential_campaign_v1.json` (36개 / 30 도메인).
실행기: `tests/benchmarks/run_sequential_campaign.py`.

- 생성·이미지 검토가 끝난 범위: **1~2번**
- 1번: ERC 0이지만 요청 1x2 헤더가 1x4로 그려짐 → 접점 형상 미측정
- 2번: AMS1117 보존, 레귤레이터 입력 부유, 역할 3/6 동작. AMS1117 특례로 고치지 않음

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft (합성에 안 넣음): I2C 풀업, USB-C sink CC

## 다음 지표 공백

1. 커넥터 요청 행·열·접점 수 vs 심볼 핀 수 vs footprint pad 수
2. 전원 핀이 요청 레일에 전도되는가 (여러 전원 회로군에서 같은 지표로. 부품명 규칙 금지)
3. 자동 visual 0 ≠ 육안 합격

생성기 변경 후 항상 `run_sequential_campaign.py --step N`으로 1..N 재실행.
회로명·프롬프트 부분문자열로 코드를 쓰지 않는다.

## 하지 않을 일

- ERC 숫자나 벤치 점수를 개선 증거로 쓰기
- RequirementSpec에 빈 물리 필드를 넓혀 모델이 패키지를 환각하게 하기
- `data/patterns/`에 캠페인 문구 `apply_when` 추가
- 실패할 때마다 데이터시트 추가
- SchGen candidate를 학습 정답·제품 규칙으로 승격 (accepted 0)
- 새 `docs/*-plan-*.md` 작성
