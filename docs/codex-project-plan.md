# 프롬프트 기반 KiCad 회로도 AI 에이전트 개발 계획 (Codex)

> 이 문서는 `docs/project-plan.md`의 Claude 계획서와 구분되는 Codex 작성 계획서다.

## 1. 목표와 MVP 기준

자연어 프롬프트를 입력받아 요구사항을 확인하고, 로컬 부품 라이브러리만 사용해 검증 가능한 KiCad 10 회로도를 생성하는 완전 오프라인 웹 애플리케이션을 만든다.

MVP 처리 흐름:

```text
프롬프트
→ 요구사항 JSON 생성·사용자 승인
→ 기능 블록 구성
→ 부품 검색·핀 검증
→ Circuit IR 합성
→ 자체 ERC
→ 회로도 배치·배선
→ .kicad_sch 생성
→ KiCad 10 ERC·SVG 렌더
→ 오류 자동 수정(최대 3회)
→ 사용자 최종 승인
```

MVP 산출물은 다음으로 고정한다.

- KiCad 10 `.kicad_sch`
- BOM CSV
- 정규화된 요구사항과 Circuit IR JSON
- 자체 ERC 및 KiCad ERC JSON 보고서
- SVG 회로도 미리보기
- 부품·규칙·이론 지식 출처가 포함된 설계 설명서

PCB 배치·배선, 온라인 부품 검색, 데이터시트 자동 다운로드, 모델 학습은 후속 버전으로 제외한다.

## 2. 시스템 구조와 인터페이스

### 실행 환경

- WSL2: FastAPI 기반 에이전트, 부품 인덱스, Circuit IR, EDA 생성·검증 엔진 실행
- Windows: `llama-server.exe`와 KiCad 10 CLI 실행
- UI: FastAPI + Jinja2 + HTMX + SVG, 진행 상황은 SSE로 전달
- 저장소: 프로젝트별 입력·승인본·생성 리비전·검증 로그·산출물을 별도 디렉터리에 보존
- 데이터베이스: SQLite FTS5로 부품과 이론 지식 검색

기본 모델은 `Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf`로 고정한다. `llama-server.exe`는 `127.0.0.1:8080`, context 8192, GPU layer 99, 동시 요청 1로 시작하고 OOM 시 GPU layer만 낮춘다. Qwen3.5-9B는 thinking 폭주 이력 때문에 MVP 경로에서 비활성화한다.

PyTorch·LibTorch CUDA는 MVP에 사용하지 않는다. 추론이 Windows `llama.cpp`에서 이루어지므로 GPU 메모리를 중복 점유할 이유가 없으며, 향후 임베딩·재랭커·파인튜닝 단계에서 별도 도입한다.

### 주요 API와 타입

- `RequirementSpec`: 전원, MCU, 인터페이스, 센서, 커넥터, 수량, 패키지 선호, 명시적 NC, 제약과 미확정 사항
- `CircuitIR`: `Project → Sheet/Block → Component → Pin ↔ Net/Bus` 구조
- `ComponentRef`: 정확한 `library:symbol`, 값, 풋프린트, 속성, 원본 라이브러리와 라이선스
- `ValidationIssue`: 검사기, 규칙 ID, 심각도, 객체 경로, 설명, 수정 가능 여부
- `LayoutIR`: 심볼 좌표·회전, 필드 위치, 라벨, 와이어, 버스, 접합점, 시트 포트

웹 API는 프로젝트 생성, 요구사항 분석·승인, 생성 실행, SSE 상태 조회, 최종 승인, 산출물 다운로드를 제공한다. LLM에는 자유로운 파일·셸 접근을 주지 않고 아래 도구만 노출한다.

- `search_parts`, `get_part_pins`
- `search_knowledge`
- `validate_requirements`, `validate_circuit`
- `propose_patch`
- `request_user_decision`

모든 LLM 응답은 JSON Schema로 강제하고, 존재하지 않는 부품·핀·풋프린트는 결정론적 검증기에서 거부한다.

## 3. 핵심 구현

### 부품 검색과 지식 기반

KiCad 10 공식 라이브러리와 현재 저장소의 SparkFun, DigiKey, ESP, OLIMEX 라이브러리를 파싱해 SQLite 인덱스를 구축한다.

우선순위는 `KiCad 공식 → DigiKey/ESP/SparkFun → OLIMEX 최신 형식 → legacy 제외`로 고정한다. 동일 이름은 덮어쓰지 않고 라이브러리 네임스페이스로 분리한다.

인덱스에는 다음을 저장한다.

- 심볼 ID, 설명, 키워드, 참조 접두어
- 핀 번호·이름·전기 타입·유닛
- 기본 및 허용 풋프린트
- 전원 핀, 숨김 핀, NC 핀
- 원본 경로, 라이선스, 파일 체크섬

전공서적 PDF는 페이지 단위로 텍스트를 추출해 FTS5 지식 인덱스를 만들되 외부 재배포하지 않는다. 검색 결과에는 파일·페이지를 기록하고, 회로 생성에는 큐레이션된 규칙만 자동 적용한다. 부품 정격처럼 로컬 자료에 없는 사실은 추측하지 않고 사용자 확인 항목으로 남긴다.

### Circuit IR와 ERC

SKIDL을 프로덕션 런타임으로 직접 의존하지 않고, MIT 라이선스 고지와 함께 핵심 데이터 모델과 검증 방식을 독립 IR에 반영한다.

참고할 구현 근거:

- `Circuit`, `Part`, `Net`, `Bus`, `Pin`의 역할은 [`circuit.py`](../skidl/src/skidl/circuit.py#L52), [`part.py`](../skidl/src/skidl/part.py#L137), [`net.py`](../skidl/src/skidl/net.py#L120), [`bus.py`](../skidl/src/skidl/bus.py#L52), [`pin.py`](../skidl/src/skidl/pin.py#L184)를 기준으로 삼는다.
- 계층 블록은 SKIDL의 `Node/SubCircuit` 개념과 [`node.py`](../skidl/src/skidl/node.py#L380)를 참고한다.
- 넷 병합 후 고유 넷 단위로 검사하고, 미연결·NC 오접속·핀 충돌·구동 능력을 검사하는 흐름은 [`erc.py`](../skidl/src/skidl/erc.py#L19)를 인용·확장한다.
- 넷리스트 생성 전 넷 이름 병합 방식은 [`circuit.py`](../skidl/src/skidl/circuit.py#L681)와 [`circuit.py`](../skidl/src/skidl/circuit.py#L752)를 참고한다.

추가 ERC 규칙:

- 존재하지 않는 핀과 중복 참조번호
- 입력 부동, 출력 간 충돌, 전원 입력 미공급
- 전원·접지 역연결
- I²C 풀업 누락
- MCU별 전원·리셋·부트·프로그래밍 핀 처리
- 전원 핀별 디커플링 누락
- 통신 전압 도메인 불일치
- 풋프린트 미지정 또는 핀 수 불일치
- 의도하지 않은 단일 핀 넷과 명시되지 않은 NC

### 직접 구현할 회로도 EDA 엔진

SKIDL 자동 배치는 force-directed 방식([`place.py`](../skidl/src/skidl/schematics/place.py#L38)), 배선은 maze/global routing과 greedy switchbox 방식([`route.py`](../skidl/src/skidl/schematics/route.py#L27))이다. 이 부분은 참고만 하고 다음과 같은 가독성 중심 엔진을 직접 구현한다.

- 기능 블록별 계층 시트 또는 명확한 영역 생성
- 입력·커넥터는 좌측, 처리부는 중앙, 출력은 우측
- 전원 레일은 위, GND는 아래
- 디커플링 부품은 대상 전원 핀 근처에 배치
- 버스와 고 fan-out 신호는 라벨을 사용하고 짧은 국부 연결은 와이어 사용
- 2.54 mm 그리드 기반 직교 A* 배선
- 심볼·텍스트·와이어 장애물 회피와 wire-through-symbol 금지
- 교차, 역방향 신호 흐름, 총 배선 길이, 라벨 충돌을 점수화해 제한적으로 재배치
- UUID는 프로젝트·계층 경로·객체 ID를 바탕으로 결정론적으로 생성

별도의 KiCad 10 S-expression serializer를 구현해 필요한 심볼 정의를 `.kicad_sch`에 포함한다. KiCad 소스 복사는 피하고 파일 형식과 동작을 기준으로 구현한다. KiCad API의 회로도 쓰기 기능은 제한적이므로 파일 생성 후 Windows `kicad-cli.exe`를 검증 오라클로 사용한다.

KiCad 10 CLI는 ERC JSON과 SVG 렌더를 생성한다. 원본에도 ERC 작업·JSON 보고서·위반 종료 코드 경로가 존재한다([`eeschema_jobs_handler.cpp`](../kicad-source-mirror-10.0.5/eeschema/eeschema_jobs_handler.cpp#L1295)). CLI 입력 경로는 `wslpath -w`로 Windows 경로로 변환한다.

### 에이전트 수정 루프

- 요구사항 승인 전에는 회로를 생성하지 않는다.
- 자체 ERC → KiCad 구문 검사 → KiCad ERC → SVG 기하 검사 순으로 실행한다.
- 오류 수정은 전체 회로 재생성이 아니라 Circuit IR JSON Patch로 수행한다.
- 동일 오류가 반복되거나 3회 내 해결되지 않으면 자동 수정을 중단하고 사용자에게 원인과 선택지를 제시한다.
- ERC 오류를 자동 제외하거나 숨기지 않는다.
- 최종 승인된 리비전은 이후 자동 수정하지 않는다.

## 4. 개발 단계

1. 기반 구축: 프로젝트 구조, FastAPI/HTMX UI, Windows llama.cpp·KiCad CLI 어댑터, 설정·로그·리비전 저장 구현
2. 부품 계층: KiCad 10 심볼 파서, 다중 라이브러리 인덱스, 검색·핀 조회 API, 라이선스와 provenance 기록
3. 회로 계층: RequirementSpec, Circuit IR, 계층·Bus·Net 연결, SKIDL 기반 ERC와 MCU·센서 규칙 구현
4. 생성 에이전트: 요구사항 승인, 도구 호출, JSON Schema 출력, 부품 선택과 IR 패치 기반 자동 수정 구현
5. EDA 계층: 가독성 배치, A* 배선, KiCad 10 serializer, BOM·SVG·리포트 생성
6. 통합 검증: KiCad CLI ERC, IR↔KiCad 넷 동등성 검사, 오류 회귀 테스트와 최종 승인 UI 완성

각 단계는 독립적으로 실행 가능한 fixture와 산출물을 갖게 하며, 모델 연결 전에도 결정론적 테스트 회로를 생성할 수 있어야 한다.

## 5. 테스트와 완료 조건

대표 골든 회로는 다음 다섯 종류로 고정한다.

- LED·전류 제한 저항·버튼 입력
- MCU 최소회로, 디커플링, 리셋, SWD 헤더
- MCU + I²C 센서 및 풀업
- MCU + SPI 메모리
- MCU + UART·전원 커넥터·상태 LED

필수 테스트:

- 한글·영문 및 단위가 섞인 요구사항 정규화
- 정확한 라이브러리 ID와 핀 번호만 사용하는지 검증
- Bus slicing, 다중 유닛 심볼, 전원 심볼, NC, 계층 포트
- 고의로 주입한 미연결·출력 충돌·풀업 누락·핀 오배선 검출
- Circuit IR 직렬화·역직렬화와 넷 연결 보존
- `.kicad_sch` 파싱, SVG export, KiCad ERC JSON 생성
- Circuit IR과 KiCad가 추출한 넷리스트의 동등성
- 심볼·필드 중첩 0건, 심볼을 통과하는 와이어 0건, 잘못된 접합점 0건
- 모델 timeout·비정상 JSON·서버 중단·GPU OOM 시 복구와 사용자 오류 표시
- 승인 전후 리비전 불변성과 동일 입력의 결정론적 재현

MVP 완료 기준:

- 골든 회로 5종이 KiCad 10에서 정상 열림
- 모든 골든 회로에서 자체 ERC 오류와 KiCad ERC 오류가 0건
- 존재하지 않는 부품·핀을 포함한 산출물이 생성되지 않음
- 요구사항 승인과 최종 승인이 모두 감사 로그에 남음
- 인터넷 연결 없이 전체 워크플로가 동작
- 30 VDC 초과, AC 상용전원, 절연·의료·안전필수 회로 요청은 생성하지 않고 제한 사유를 설명

## 6. 명시적 가정과 후속 확장

- MVP 대상은 최대 24 VDC, 3 A 이하의 MCU·센서·기초 디지털/아날로그 회로다.
- 부품 가용성·가격·제조 상태는 완전 오프라인 조건상 보장하지 않는다.
- 전공서적은 내부 검색과 설계 근거 확인에만 사용하며 학습 데이터로 재배포하지 않는다.
- SKIDL에서 옮겨 사용하는 코드나 알고리즘에는 원저작자와 MIT 라이선스를 보존한다. KiCad GPL 소스는 동작 참고와 검증에 사용하고 직접 복사할 경우 별도 라이선스 검토를 거친다.
- 초기 조사 보고서의 구조화 그래프·도구 검증 우선 원칙([`deep-research-report.md`](deep-research-report.md#L34))을 유지한다.
- 후속 버전은 공식 데이터시트 검색, SPICE 검증, PCB 배치·배선·DRC, 부품 수급 정보, 모델 파인튜닝 순으로 확장한다.
