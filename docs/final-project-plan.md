# 프롬프트 기반 KiCad 회로도 AI 에이전트 — 최종 통합 계획

> `docs/project-plan.md`(Claude 초안)와 `docs/codex-project-plan.md`(Codex 초안)를 통합한 최종 계획서. 원본 두 문서는 참고용으로 그대로 남겨둔다. `docs/deep-research-report.md`(대규모 파인튜닝 데이터셋 설계 보고서)는 사용자가 불필요 판단하여 삭제했으므로 이 문서에서도 더 이상 참조하지 않는다.

## 1. 목표

자연어 프롬프트를 입력받아 요구사항을 정규화하고, 로컬 부품 라이브러리와 로컬 LLM(Qwen2.5-Coder-7B-Instruct, llama.cpp)만으로 검증 가능한 KiCad 10 회로도(`.kicad_sch`)를 생성하는 완전 오프라인 에이전트를 만든다. 클라우드 LLM, 벤더 부품 API, 인터넷 연결에 의존하지 않는다.

## 2. 범위(Scope)

**MVP 범위는 Codex 초안이 정의한 5종 골든 회로 전체를 기준으로 한다(§10)** — 수동소자+전원뿐 아니라 MCU 최소회로, MCU+I²C, MCU+SPI, MCU+UART까지 포함한다. (Claude 초안 작성 중 AskUserQuestion으로 "수동소자+전원만"으로 v1 범위를 좁혔던 결정은 사용자의 이후 지시로 대체되었다.)

다만 **구현 순서는 여전히 "가장 단순한 회로로 파이프라인 전 구간을 먼저 관통시킨다"는 원칙을 따른다**: Phase 1에서 수동소자+전원 회로(§10 골든 회로 1번)로 프롬프트→검증된 `.kicad_sch` 전 구간을 완성한 뒤, Phase 2부터 다핀 IC/MCU 지원을 쌓아 올린다(§9 로드맵). 이는 범위를 좁히는 것이 아니라 리스크가 낮은 것부터 순서대로 구현하는 것 — MVP 완료 기준(§12)은 처음부터 5종 골든 회로 전체다.

## 3. 비목표 (Non-goals)

- PCB 배치·배선·DRC, Gerber/IPC-2581 등 제조 산출물 — 스키매틱 이후 단계, 별도 트랙.
- SPICE 시뮬레이션(아날로그 동작 검증) — ERC(연결 규칙)까지만.
- 온라인 부품 검색, 데이터시트 자동 다운로드, 부품 가격/재고 조회 — 완전 오프라인 전제와 상충.
- 대규모 데이터셋 수집 및 모델 파인튜닝 — 완전히 별개의 장기 리서치 프로젝트(§9 Phase 6). §6에서 확인했듯 `이론지식/`의 전공서적 PDF는 이 파인튜닝 트랙에도 적합하지 않다(넷리스트·S-expression·구조화된 회로 예시가 없음) — 필요하다면 처음부터 별도로 설계해야 한다.
- 30 VDC 초과, AC 상용전원, 절연·의료·안전필수 회로 — 에이전트가 생성을 거부하고 사유를 설명. MVP 대상은 최대 24 VDC·3A 이하의 MCU·센서·기초 디지털/아날로그 회로로 한정.

## 4. 확정된 환경/인프라 사실

- **GPU**: RTX 4060 8GB. 유휴 시 ~1.3GB 점유. Qwen2.5-Coder-7B-Q5_K_M(~5.1GB) 로드 후 KV캐시/연산버퍼 여유가 ~1.6GB로 빠듯함 → `--ctx-size`는 8192 안팎으로 제한. 이 제약이 §8.4의 컨텍스트 예산 정책을 강제한다.
- **모델**: 기본 모델은 `Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf`로 고정. `Qwen3.5-9B`는 thinking 모드 폭주로 응답 생성 실패 이력 5/5 — MVP 경로에서 완전히 비활성화(원인이 VRAM 부족이든 thinking 모드 자체든 대응은 동일).
- **PyTorch/LibTorch(CUDA)는 MVP에 사용하지 않는다** — 추론이 이미 Windows `llama.cpp`에서 이루어지므로 GPU 메모리를 중복 점유할 이유가 없음. cu132 wheel·설치 경로는 조사되어 있으므로 향후 임베딩·재랭커·파인튜닝 단계에서 필요시 도입.
- **실행 환경 분리**: WSL2(FastAPI 에이전트, 부품 인덱스, Circuit IR, EDA 생성·검증 엔진) / Windows(`llama-server.exe`, KiCad 10 CLI). `.wslconfig`의 `networkingMode=mirrored` 덕분에 WSL2에서 `http://localhost:PORT`로 Windows의 `llama-server.exe`를 직접 호출 가능 — Phase 0에서 실측 확인 필요.
  - `llama-server.exe`는 WSL2 bash에서 직접 기동 시 종료 코드 53으로 즉시 실패(CUDA/DLL 탐색 경로 문제로 추정) → 에이전트가 서버 프로세스를 직접 관리하지 않고, 사용자가 Windows에서 기동한 뒤 에이전트는 순수 HTTP 클라이언트로만 통신. `127.0.0.1:8080`, GPU layer 99, 동시 요청 1로 시작하고 OOM 시 GPU layer만 낮춘다.
- **`kicad-cli.exe` 호출 — 반드시 지켜야 할 규칙 (실측 검증됨)**: WSL2에서 `kicad-cli.exe`에 리눅스 경로(`/home/...`)나 `/mnt/c/...` 문자열을 그대로 넘기면 **"회로도 로드 실패", exit 3**으로 실패한다 — Windows 프로세스가 해당 문자열을 경로로 해석하지 못함. 반드시 **`wslpath -w <path>`로 변환한 경로**를 넘겨야 하며, 이 경우 `/mnt/c` 스테이징 없이 WSL2 네이티브 경로(`\\wsl.localhost\Ubuntu-24.04\...`)에서 바로 동작한다 (실제 KiCad 데모 스키매틱으로 실측: `sch erc --format json` → exit 0, 구조화된 JSON 위반 리포트 정상 수신). 이 wslpath 래퍼가 모든 `kicad-cli` 호출의 필수 전처리다. 자동 게이트에는 `--exit-code-violations` 사용.
- **Python**: WSL2 기본 `python3`는 3.14.4. SKIDL은 `python_requires=">=3.6"`이며 classifier에 3.14도 명시되어 문제 없음. 다만 PyTorch cu132의 3.14 wheel 지원은 미확인이나 MVP에서 PyTorch 자체를 쓰지 않으므로 현재는 무관.
- **PDF 처리**: `pdftoppm`(poppler-utils)이 환경에 설치되어 있지 않음 — Read 도구의 PDF `pages` 파라미터가 이에 의존해 작동하지 않는다. **PyMuPDF(`fitz`)는 이미 설치되어 있고 정상 동작 확인됨** — §6의 지식 추출 파이프라인은 PyMuPDF 기반으로 설계한다(poppler 설치 여부와 무관하게).
- **리소스**: 839GB 여유 디스크, 16 코어, 15GB RAM — 제약 없음. 프로젝트 디렉터리는 아직 git 저장소 아님(하위 skidl/, kicad-source-mirror-10.0.5/, ESP/OLIMEX/SparkFun/digikey는 각각 독립 저장소·참고자료, 수정 대상 아님).

## 5. 참고 자료 정리

### 5.1 SKIDL — 인용 범위와 근거

**"미숙하다" 판단의 근거**: SKIDL은 부품 검색(`part_query.py`, 로컬 SQLite 정규식 검색, 벤더 API 없음)과 회로도 자동 배치·라우팅(`schematics/place.py`+`route.py`, force-directed 배치 + switchbox 라우팅)을 이미 갖고 있지만, KiCad 10 지원(v2.3.0)이 2026-07-28 출시로 이 계획 시점 기준 약 1주일 전이며, 자체 테스트에 출력 품질(배치·라우팅 결과) assert가 전무하고, 현실적 계층 회로 하나는 `xfail`(예상된 실패)로 커밋되어 있다. 사용자의 "부품 검색·회로도 그리기는 미숙하니 직접 구현" 지시를 이 코드 근거에 따라 그대로 채택한다 — 이 두 기능은 **직접 구현**, 나머지는 **인용/포팅**한다.

**인용 대상과 근거 위치**:

| 대상 | SKIDL 소스 | 재사용 방식 |
|---|---|---|
| `Circuit`/`Part`/`Net`/`Bus`/`Pin` 역할 분리 | `circuit.py:52`, `part.py:137`, `net.py:120`, `bus.py:52`, `pin.py:184` | 필요 범위로 축소 포팅 (IR 데이터 모델 기준) |
| 계층 블록(`Node`/`SubCircuit`) | `node.py:380` | "Node 트리 + 계층적 UUID 경로" 개념만 포팅 — LLM IR은 중첩 JSON으로 계층 표현 |
| 핀 타입 충돌 매트릭스 (ERC 핵심 규칙) | `pin.py:1023-1085` (`conflict_matrix`) | 값 그대로 인용 — OUTPUT×OUTPUT=ERROR, PWROUT×PWROUT=ERROR, PWROUT×OUTPUT=ERROR, NOCONNECT 규칙, TRISTATE×OUTPUT=WARNING 등 |
| 넷 병합 후 단위 ERC 체크 | `erc.py:19`, `circuit.py:681`, `circuit.py:752` | 로직 패턴 인용·확장 — 미연결 핀, 0/1핀 네트, 드라이브 강도 부족 + §8.2의 MCU 특화 규칙으로 확장 |
| 넷리스트 S-expression 구조 | `tools/kicad10/gen_netlist.py` | 구조 그대로 인용 |
| 결정론적 UUID 스킴 | `uuid.uuid5`, namespace `7026fcc6-e1a0-409e-aaf4-6a17ea82654f` | 동일 패턴 채택 — 향후 PCB 확장 시 상호 참조 호환 |
| `.kicad_sym` 파서 구조 | `tools/kicad10/lib.py` | 파싱 로직 참고, 실제 구현은 `simp_sexp`로 직접 |
| **S-expression 저수준 파서** | 의존 패키지 `simp_sexp` | **유일하게 실제 pip 의존성으로 채택** — 좁고 이미 해결된 문제 재발명 방지 |
| 배치/라우팅 알고리즘 아이디어 | `place.py:38`(force-directed), `route.py:27`(전역 미로 라우팅 + greedy switchbox, doi.org/10.1016/0167-9260(85)90029-X 인용) | 알고리즘 패턴만 참고, §7.5 자체 엔진으로 직접 구현 |

SKIDL을 `pip install skidl` 런타임 의존성으로 넣지 않는 이유: (1) LLM IR은 구조화 JSON이라 SKIDL의 연산자 오버로딩(`+=`,`&`,`|`)이 불필요, (2) 알려진 설계 허점 — `erc_list`가 클래스 속성이라 `add_erc_function()`이 해당 클래스의 모든 인스턴스에 전역으로 적용되는 부작용, 최상위 `generate_netlist()` 등이 import 시점에 `default_circuit`에 바인딩되어 `with Circuit() as ckt:` 안에서도 엉뚱한 회로를 대상으로 동작할 수 있는 함정 — 이를 상속하지 않기 위함, (3) 자체 배치/라우팅 엔진이 읽을 데이터 모델을 "geometry in, wires out"에 맞게 처음부터 깔끔하게 설계 가능 (SKIDL 엔진은 라이브 객체에 `part.tx`/`pin.pt` 등을 직접 주입하는 방식으로 결합되어 분리가 어려움).

**개발 중 1회성 활용**: `pip install skidl`로 임시 설치 후 예제 회로를 `generate_schematic()`으로 생성해 **구조 참고용 골든 레퍼런스**로 보관(`lib_symbols` 블록, UUID 스킴, Y-down 좌표계 확인용). **단, 헤더/버전 스탬프의 기준으로 삼지 말 것** — SKIDL의 `tools/kicad10/backend.py`는 실제로 `20230409`(KiCad 8/9 시절 값)를 하드코딩하고 있음이 확인됨. "kicad10" 폴더명과 달리 실제 KiCad 10 포맷 스탬프로 갱신되지 않은 상태. 헤더/버전의 정답은 §9 Phase 1의 손으로 작성한 파일이 담당한다. MIT 라이선스 고지를 유지한다.

### 5.2 KiCad 파일 포맷 — 자체 EDA 엔진이 반드시 지켜야 할 규칙

- **`.kicad_sch`에는 `(net ...)` 객체가 없다.** 연결은 순수 좌표 일치(wire 끝점 = 핀 절대좌표 or 다른 wire 끝점/junction)로만 결정되거나, 텍스트가 같은 label(`label`/`global_label`/`hierarchical_label`)로 결정된다. T자 교차점을 관통하는 하나의 긴 wire는 연결로 인식되지 않음 — 반드시 `junction`에서 두 segment로 분리해야 한다.
- **자기완결적 파일**: 사용하는 모든 심볼의 전체 정의가 `(lib_symbols ...)`에 캐시되어 내장되므로 `sym-lib-table` 없이도 렌더링된다. 생성기는 참조 심볼을 통째로 `lib_symbols`에 복사해 넣으면 된다.
- **버전 번호는 날짜 스탬프**(semver 아님). 현재 저장소(KiCad 10.0.5) 기준 스키매틱 `20260306`, 심볼 라이브러리 `20251024` (`eeschema/sch_file_versions.h`). SKIDL의 `kicad10` 백엔드가 구버전 스탬프를 하드코딩하고 있으므로(§5.1) 신뢰하지 말 것.
- **심볼 구조**: `(symbol "Name_유닛_바디스타일" ...)` (유닛 0=공통 그래픽, 바디스타일>1=De Morgan 대체). `(pin <전기타입> <그래픽스타일> (at x y angle) (length L) (name "N")(number "N"))` — 전기타입은 SKIDL `pin_types`와 거의 1:1 매핑. 일부 심볼은 `extends`(상속) 사용(OLIMEX `Used-In-KiCad_v7`에서 5건 확인) — 파서가 처리해야 함.
- **포맷 그라운드 트루스**: `eeschema/schematic.keywords`(토큰 목록 ~180개), `eeschema/sch_file_versions.h`(버전 changelog), `eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp`+`sch_io_kicad_sexpr.cpp`+`sch_io_kicad_sexpr_lib_cache.cpp`(실제 리더/라이터), ERC 잡 처리는 `eeschema/eeschema_jobs_handler.cpp:1295` 부근(JSON 리포트·위반 종료 코드 경로).
- **새 IPC API(`api/`)는 스키매틱 쪽이 미완성**: `schematic_commands.proto`는 명령이 **0개**, `eeschema/api/api_handler_sch.cpp`에 `// TODO` 스텁 다수. PCB 쪽(`board_commands.proto` 37개 명령)과 대조적. → **`kicad-cli.exe`가 유일한 실전 자동화 경로** (`pcbnew` Python SWIG 바인딩도 PCB 전용, eeschema 대응품 없음). `kicad-cli sym upgrade`로 레거시 `.lib`/`.dcm`→`.kicad_sym` 변환 가능.
- **`power:PWR_FLAG` 함정**: `power:GND`/`power:+5V` 등 전원 심볼의 핀은 `power_in`이므로, 이를 구동하는 `power_out` 핀이 없으면 KiCad ERC가 "Input power pin not driven by any Output Power pins" 에러를 낸다 — 토폴로지가 완벽해도 발생. 표준 해법은 전원 net마다 `power:PWR_FLAG`(핀이 `power_out`) 하나씩 추가. 넷리스트/이미터가 전원 net마다 자동으로 붙이도록 구현.

### 5.3 부품 라이브러리 실측 규모 및 우선순위 (Codex 초안의 우선순위 오류 수정)

> Codex 초안은 "KiCad 공식 → DigiKey/ESP/SparkFun → OLIMEX 최신 → legacy 제외" 순서를 제시했으나, 실측 결과 **DigiKey 라이브러리는 100% 레거시 포맷**(`.kicad_sym` 0개, `.lib` 1223개+`.dcm` 150개)이라 "2순위"와 "legacy 제외"가 서로 모순된다. 아래가 실측 기반 수정된 우선순위다.

| 라이브러리 | 포맷 | 심볼 수 | 우선순위 |
|---|---|---|---|
| KiCad 번들(`share/kicad/symbols`) | 최신 | 224개 파일 (R/C/L, 74xx 시리즈 등 표준 부품 총망라) | **1순위** — 기준 라이브러리 |
| ESP-kicad-libraries | 최신(`20251024`) | 49 | 2순위 |
| SparkFun-KiCad-Libraries | 최신(동일 버전) | 761(31개 파일, `SparkFun-MicroMod.kicad_sym`은 빈 파일 — 혼동 주의) | 2순위 |
| OLIMEX `Used-In-KiCad_v7/` | v7(구버전이나 KiCad10이 로드 시 자동 업그레이드) | 638 | 3순위 — v8+ 폴더 없음, v7 사용 |
| digikey-kicad-library | **레거시**(`.kicad_sym` 0개) | 1073 | **`kicad-cli sym upgrade` 1회 변환 후에만 편입** — 즉시 사용 불가 |

동일 이름은 덮어쓰지 않고 라이브러리 네임스페이스로 분리한다. 인덱스에는 심볼 ID·설명·키워드·참조 접두어, 핀 번호·이름·전기타입·유닛, 기본/허용 풋프린트, 전원·숨김·NC 핀, 원본 경로·라이선스·파일 체크섬을 저장한다. Floyd 교재(§6)에서 확인된 74xx 부품번호는 KiCad 번들 `74xx.kicad_sym`과 직접 매핑되므로, §6의 지식 추출과 이 부품 인덱스는 서로 검증 가능한 관계다.

## 6. 이론지식(전공서적) PDF 활용 전략

사용자 지시: PDF를 무작정 페이지 단위로 텍스트 추출해 지식화하지 말고, 이 문서들이 실제로 (1) 회로 생성 에이전트의 지식 기반(검색 그라운딩)이 될 수 있는지, (2) 학습 데이터가 될 수 있는지, (3) 될 수 있다면 어떻게 추출해야 하는지를 먼저 검토할 것. 아래는 `이론지식/`의 8개 PDF 전체를 실제로 샘플링(각 권당 목차/서문/여러 장 20~80페이지, 총 8개 병렬 조사)한 결과다.

### 6.1 책별 평가

| 책 | 콘텐츠 품질/추출 용이성 | 원리·공식 가치 | 현재 스코프 적합도 (MCU+수동소자) | 판정 |
|---|---|---|---|---|
| **Practical Electronics for Inventors** (Scherz/Monk, 4th) | 상 — 본문 Tier A, 표/그림 일부 Tier B~C | 상 — 경험칙·표준부품번호·설계공식이 압도적으로 밀집 | 상 — 전 영역 직결, 회로도 작성 컨벤션(§7.4)까지 포함 | **즉시 추출** — 최우선 단일 소스 |
| **Introductory Circuit Analysis** (Boylestad, 14th) | 중 — 표는 Tier B(get_text로 파손 확인) | 특정 절(Ch.3/4/10/22)만 상, 나머지는 이론 위주 | 상 — 저항/커패시터 표준값, 안전마진, 필터 공식이 직결 | **즉시 추출** (Ch.3, Ch.4 §4.7-4.8, Ch.10 §10.4, Ch.22만) |
| **Digital Fundamentals** (Floyd, 11th) | 상 — 본문 Tier A | 상 — 555 타이머 공식, fan-out, 미사용 입력 처리 | 상 — 디지털 로직 서브셋, 74xx 부품번호가 KiCad 번들과 직접 매핑 | **즉시 추출** (Ch.7 §7.5-7.6, Ch.15 §15.1/15.4, Ch.12만) — TTL 수치는 구식이므로 15-5절 CMOS 비교표 우선 |
| **Fundamentals of Electric Circuits** (Sadiku, 7th) | 상 — 본문 Tier A, 임베디드 TOC 332개(자동 챕터 분할 용이) | 상 (Ch.5 Op-amp), 나머지(특히 Part 3)는 순수 유도 | 중 — Op-amp 이득식·필터 컷오프식만 직결 | **즉시 추출** (Ch.5 전체, Ch.14 §14.5-14.9만) |
| **Microelectronic Circuits** (Sedra/Smith) | 상 — 본문 Tier A, Example N.M+Solution 구조 매우 체계적 | 상, 단 상당수(Ch8-16)가 op-amp/로직게이트 **내부** 트랜지스터 레벨 설계로 스케매틱 배치 대상이 아님 | 하~중 — 현 MCU+수동소자 골든회로엔 대부분 불필요, Ch2 op-amp 응용회로·Ch18 555/발진기만 Sadiku·Floyd와 중첩 | **부분 즉시 추출** (Ch2 op-amp 응용회로, Ch18 555·Wien-bridge 발진기, 부록 J 표준저항값, 각 장 Summary 블록만 — 나머지 IC 내부 설계·반도체물리 유도는 제외) |
| **Power Electronics Converters, Applications, and Design** (Mohan) | 상 — 본문 Tier A | 상 (Part 7 실전설계) | 하 — 정류기/인버터/모터드라이브 도메인, 현 스코프와 불일치 | **범위 확장 시 재평가** (Ch.27 스너버, Ch.29 방열, Ch.30 자성설계) |
| **Principles of Power Electronics** (Kassakian) | 상 — 본문 Tier A | 상 (Part IV 실무 챕터) | 하 — 컨버터 토폴로지/제어이론 중심 | **범위 확장 시 재평가** (Part IV, 22-26장) |
| **Fundamentals of Power Electronics** (Erickson) | **하 — Tier C**: 수식·표·그림이 전부 래스터 이미지, `get_text()`가 무음으로 통째 누락 | 상 (원문 품질은 높으나 텍스트로 추출 불가) | 하 — 컨버터 동역학/자성설계 도메인 | **제외** — 범위가 확장돼도 비전/OCR 파이프라인이 별도로 필요해 비용 대비 우선순위 낮음 |

*(Sedra/Smith 조사 완료 시 해당 행을 채우고, 필요하면 이 표를 갱신한다.)*

### 6.2 추출 방법론 — 3단계 신뢰도 티어

8권 모두 PyMuPDF(`fitz`)로 샘플링했다(§4의 poppler 부재 때문). 이 과정에서 **PDF마다 텍스트 추출 신뢰도가 다르다**는 것이 확인되었고, 이는 구현 시 반드시 사전에 티어를 확인해야 하는 사항이다 — 가장 위험한 실패 모드는 **조용한 실패**: 문장은 끊김 없이 이어지는데 그 사이의 수식·표만 통째로 사라진다.

- **Tier A (본문 그대로 사용 가능)**: 서술형 본문 대부분. `get_text()`로 충분.
- **Tier B (표·수식이 있지만 파손되어 나옴)**: Boylestad Table 3.5, PEFI Table 3.7/5.1/8.1 등에서 실측 확인 — 숫자가 열 구조 없이 나열되거나 수식이 토큰 단위로 흩어짐. `page.find_tables()`(PyMuPDF 내장) 또는 좌표 기반 클러스터링(`get_text("dict")`/`"blocks"`)으로 셀 구조를 복원해야 한다. **복원 없이 그대로 쓰면 안 됨** — 열이 뒤섞인 "그럴듯한 숫자"가 나와 잘못된 상수를 심을 위험이, 아예 추출하지 않는 것보다 크다.
- **Tier C (수식·표·그림이 전부 래스터 이미지)**: Erickson 한 권에서 확인 — `get_text()`가 표 제목만 남기고 수치는 전부 무음 삭제. 페이지를 이미지로 렌더링해 비전 모델(멀티모달 LLM)이나 OCR로 재해석하지 않는 한 사실상 추출 불가능.

**환경 참고**: PEFI(108MB)는 Read 도구의 100MB 상한을 넘으므로, 페이지 구간별 사전 추출(PyMuPDF로 청크 단위 텍스트화) 스크립트가 필요하다 — 이 컬렉션에서 가장 가치 높은 단일 소스이므로 사소한 문제가 아니다.

Codex 초안이 제시했던 **"페이지 단위 텍스트 추출 → SQLite FTS5 인덱스"** 방식은 위 실측 결과로 반박된다(단순 개선이 아니라 폐기) — 가장 가치 있는 콘텐츠(표준값 표, 설계 공식)가 바로 naive 텍스트 추출이 파손시키는 대상이기 때문이다. 대신 **~10개 내외의 지정된 절을 반자동으로 정밀 추출**해 표는 필드 단위로 분리된 구조화 JSON/YAML로, 본문 규칙은 "조건 → 값/공식" 형태의 단문으로 큐레이션하고, 표는 사람이 한 번 검수한다.

### 6.3 추출 대상 선정 기준 — "관련성"이 아니라 "도달 가능성" 테스트

후보 구절을 넣을지 말지는 주관적 "흥미로움"이 아니라, 파이프라인이 실제로 소비할 수 있는 형태로 표현되는지로 판단한다:

1. **값 산출 공식** — 예: LED 전류제한저항 = (Vsupply − Vf) / If
2. **부품 선정 규칙** — 예: 디커플링 캡 0.01~0.1µF, IC VCC-GND 핀 근처에 배치
3. **§8.2 ERC 판정 규칙로 바로 쓸 수 있는 조건** — 예: I²C 풀업 저항 범위, 미사용 입력 처리 규칙

이 셋 중 하나로 표현되지 않는("일반적으로 흥미로운 배경지식") 내용은 제외한다 — §6.1 표의 "제외"/"범위 확장 시 재평가" 판정 대부분이 이 기준에서 나온다.

### 6.4 커버리지 갭 — MVP 필수 상수의 출처 확인

애초에 "이 컬렉션에 MVP에 필요한 상수(디커플링 값, 풀업 범위 등)의 출처가 아예 없을 수 있다"는 우려가 있었으나, 실측 결과 아래와 같이 출처가 확인되었다:

| 필요 상수 (§8.2 ERC 규칙에서 사용) | 출처 |
|---|---|
| 디커플링 커패시터 값(0.01~0.1µF, IC당 배치 규칙) | Practical Electronics for Inventors, p.1214 |
| 풀업 저항 범위(10kΩ 대표값, Vin=5V−R·IIH 공식) | Practical Electronics for Inventors, p.1247 |
| 풀다운 저항 범위(100Ω~1kΩ) | Practical Electronics for Inventors, p.1247-1248 |
| 표준 저항 E-계열 값, 색띠/SMD 코드 | Introductory Circuit Analysis, Table 3.5 (Ch.3) |
| Fan-out/미사용 입력 처리(1.0kΩ 풀업)/555 타이머 설계식 | Digital Fundamentals, Ch.7 §7.5-7.6, Ch.15 §15.1/15.4 (TTL 수치는 구식 — 15-5절 CMOS/74HC 비교표 우선 사용) |
| Op-amp 이득식/필터 컷오프식 | Fundamentals of Electric Circuits, Ch.5, Table 5.1 |
| 회로도 작성 컨벤션(입력좌/출력우, +V상단/GND하단, 지정자·값 표기법) | Practical Electronics for Inventors §7.2.1 (p.879-881) — §7.4 레이아웃 컨벤션의 근거이기도 함 |

**남은 공백**: 특정 IC의 정확한 최대 정격, 벤더별 데이터시트 수치 같은 부품 고유값은 이 컬렉션에 없다 — 애초에 교과서가 다루는 영역이 아니다. 이런 값은 근거 없이 추측하지 않고, **하드코딩된 보수적 관례값**을 쓰거나 **`request_user_decision`**(§7.3)으로 사용자에게 넘긴다.

### 6.5 "지식 기반"이 될 수 있는가 vs "학습 데이터"가 될 수 있는가

이 둘은 별개의 질문이며 답도 다르다.

- **검색 기반 지식 그라운딩(`search_knowledge` 도구, §7.3)으로는 유용하다.** 문제+완결된 풀이가 함께 제시되는 worked example이 풍부하고(Sadiku 한 권만 약 2,481개), 위 §6.4처럼 인용 가능한 설계 공식·경험칙이 실제로 다수 확인됐다.
- **파인튜닝용 학습 데이터로는 쓸 수 없다.** 이 책들이 가르치는 것은 "주어진 회로를 수식으로 해석하는 법"이지 "자연어 사양에서 `.kicad_sch`/넷리스트를 생성하는 법"이 아니다. 넷리스트, S-expression, 구조화된 토폴로지 예시가 이 컬렉션에 전혀 없다 — 회로도 자체는 도표 이미지로만 존재하며 텍스트 추출 시 소실된다(PEFI에서만 2,000장 이상 확인). §6.2의 Tier C 문제까지 더하면 일부 책은 수식조차 신뢰성 있게 뽑히지 않는다.

**결론**: 이 PDF들은 `search_knowledge` 도구의 검색 그라운딩용으로 §6.1~6.4 기준에 따라 큐레이션하고, `docs/deep-research-report.md`(삭제됨)가 다뤘던 것과 같은 대규모 파인튜닝 데이터셋 구축에는 사용하지 않는다 — 그 문서를 불필요 판단해 삭제한 것과도 방향이 일치한다.

## 7. 시스템 아키텍처

### 7.1 애플리케이션 형태

- WSL2: FastAPI 기반 에이전트, 부품 인덱스, Circuit IR, EDA 생성·검증 엔진
- Windows: `llama-server.exe`, KiCad 10 CLI
- UI: FastAPI + Jinja2 + HTMX + SVG, 진행 상황은 SSE로 전달
- 저장소: 프로젝트별 입력·승인본·생성 리비전·검증 로그·산출물을 별도 디렉터리에 보존 (승인 전후 리비전 불변성 보장)

### 7.2 처리 흐름

```
프롬프트
→ 요구사항(RequirementSpec) JSON 생성 · 사용자 승인    ← 승인 전에는 회로 생성 안 함
→ 기능 블록 구성
→ 부품 검색 · 핀 검증 (SQLite FTS5, §5.3 우선순위)
→ Circuit IR 합성 (LLM 도구 호출 루프, §7.3)
→ 자체 ERC (§5.1 SKIDL 인용 + §8.2 확장 규칙)
→ 회로도 배치 · 배선 (§7.5)
→ .kicad_sch 생성 (자체 S-expression 이미터, §5.2 규칙 준수)
→ KiCad 10 CLI ERC(JSON) · SVG 렌더 (wslpath -w 래퍼 필수, §4)
→ 오류 자동 수정 (IR JSON Patch, 최대 3회, §8.4)
→ 사용자 최종 승인
```

MVP 산출물: `.kicad_sch`, BOM CSV, 정규화된 요구사항+Circuit IR JSON, 자체 ERC·KiCad ERC JSON 보고서, SVG 미리보기, 부품·규칙·이론지식 출처가 포함된 설계 설명서.

### 7.3 에이전트/도구 아키텍처

LLM에는 자유로운 파일·셸 접근을 주지 않고 다음 도구만 노출한다: `search_parts`, `get_part_pins`, `search_knowledge`(§6의 큐레이션된 지식 인덱스 검색), `validate_requirements`, `validate_circuit`, `propose_patch`, `request_user_decision`(§6.4의 공백 상수 확인용). **모든 LLM 응답(도구 호출 인자 포함)은 JSON Schema/GBNF로 강제**하며(llama-server의 `--json-schema`/`--grammar` 공식 지원 확인), 존재하지 않는 부품·핀·풋프린트는 결정론적 검증기에서 거부한다. 이렇게 하면 §5.1에서 채택한 "구조화 IR + 결정론적 파이프라인" 원칙과 Codex의 도구 호출 루프가 하나의 설계로 합쳐진다 — LLM은 매 턴 좁은 스키마의 도구 호출만 하고, 실제 넷리스트/ERC/배치/직렬화는 전부 결정론적 코드가 수행한다.

**정방향 경로에도 §8.4의 컨텍스트 예산 정책을 동일하게 적용한다.** 수정 루프뿐 아니라 요구사항 추출 → 여러 차례의 `search_parts`/`get_part_pins` 호출 → IR 합성으로 이어지는 정방향 경로도 8192 토큰을 금방 소진한다 — 후보 부품 5개의 전체 핀 테이블만으로도 컨텍스트를 다 채울 수 있다. 각 도구 호출 결과는 즉시 IR에 반영한 뒤 대화 이력에서 제거한다: LLM이 매 턴 보는 것은 "현재까지 합성된 IR + 현재 단계에서 필요한 정보"뿐이며, 이전 도구 호출들의 원시 응답이나 전체 대화 이력은 보내지 않는다.

### 7.4 핵심 데이터 타입

- `RequirementSpec`: 전원, MCU, 인터페이스, 센서, 커넥터, 수량, 패키지 선호, 명시적 NC, 제약과 미확정 사항
- `CircuitIR`: `Project → Sheet/Block → Component → Pin ↔ Net/Bus`
- `ComponentRef`: 정확한 `library:symbol`, 값, 풋프린트, 속성, 원본 라이브러리와 라이선스
- `ValidationIssue`: 검사기, 규칙 ID, 심각도, 객체 경로, 설명, 수정 가능 여부
- `LayoutIR`: 심볼 좌표·회전, 필드 위치, 라벨, 와이어, 버스, 접합점, 시트 포트

**레이아웃 컨벤션**: 기능 블록별 계층 시트 또는 명확한 영역 생성. 입력·커넥터는 좌측, 처리부는 중앙, 출력은 우측. 전원 레일은 위, GND는 아래. 디커플링 부품은 대상 전원 핀 근처에 배치. 지정자(R1/C3/Q1/IC4)·값 표기(100k, 0.1µF)·접합점 점 찍기·핀번호는 심볼 바깥에 표기 — 이 컨벤션은 Codex 초안의 제안과 **Practical Electronics for Inventors §7.2.1(§6.4)이 독립적으로 일치**해 근거가 두 배로 확인됨. UUID는 프로젝트·계층 경로·객체 ID 기반 결정론적 생성(§5.1 SKIDL 스킴 준용).

### 7.5 배치 · 배선 엔진 — 통합 연결 정책

SKIDL의 force-directed 배치(`place.py`)와 maze/switchbox 배선(`route.py`)은 알고리즘 아이디어만 참고하고 직접 구현한다(§5.1). **연결 표현은 라벨과 와이어를 처음부터 함께 쓰는 하이브리드 정책 하나로 통일**한다 — 넷의 성격에 따라 처음부터 다른 방식을 쓴다:

- **라벨(label) 연결**: 전원 레일, 버스, N그리드 단위 이상 떨어진 net. `power:PWR_FLAG` 필수(§5.2).
- **직접 와이어**: 인접 핀 간 짧은 국부 연결.
- **A* 그리드 배선(2.54mm)**: 그 외 일반 연결 — 심볼·텍스트·와이어 장애물 회피, wire-through-symbol 금지, 교차·역방향 신호흐름·총 배선길이·라벨충돌을 점수화해 제한적으로 재배치. **MVP 게이트 이후(§9 Phase 6)에 도입** — MVP 완료 기준(§12)은 ERC 0건이지 배선 미관이 아니므로, 그 전까지는 A* 대상 net도 라벨로 대체해 파이프라인을 먼저 완성한다.

**MCU 회로의 조밀한 fan-out에 대한 명시적 대응**: 범위가 Codex 기준(§2)으로 확장되면서 MCU 골든 회로(§10 2~5번)는 수동소자 회로보다 핀 수가 많아 라벨이 조밀해진다. A*를 앞당기는 대신, **Phase 3(§9)에서 A*보다 훨씬 저렴한 "라벨 밀집 완화 휴리스틱"을 함께 도입**한다 — 실제 경로 탐색 없이: 디커플링 캡을 해당 전원 핀 바로 옆에 배치, 주변장치 핀 라벨을 물리적 핀 순서대로 정렬, 같은 net의 라벨을 스택 배치. 이것만으로도 "ERC는 통과하지만 읽기 어려운" 문제를 상당 부분 완화하며, 진짜 그래프 탐색 문제(A*)는 여전히 뒤로 미룬다 — 정확성(ERC)과 가독성(A*)을 같은 단계에서 동시에 새로 만들지 않기 위함.

**라벨 분기의 구현 디테일(필수, 실패 시 조용히 ERC 실패)**: 라벨은 좌표가 정확히 핀이나 wire 위에 있어야 연결로 인식된다 — 허공에 떠 있는 라벨은 "미연결"로 판정된다. 따라서 "배치 후 아무 데나 라벨"이 아니라: `pin_absolute_position(symbol_placement, lib_pin)`로 핀의 절대좌표를 정확히 계산(심볼 `(at x y angle)` 변환 + `lib_symbols` 캐시 핀의 로컬 오프셋/회전 합성) → 그 좌표에서 시작하는 **짧은 스텁 wire**를 뽑음 → 스텁 반대쪽 끝에 라벨 배치. SKIDL의 `auto_stub` 폴백과 동일한 방식이며, 실제 프로덕션 KiCad 회로도에서도 흔히 쓰인다. 이 함수는 골든 레퍼런스 파일 기준 단위 테스트로 반드시 검증한다 — 가장 흔하고 디버깅하기 까다로운 실패 모드다.

별도의 KiCad 10 S-expression 시리얼라이저를 직접 구현하며(§5.2 규칙 준수), KiCad 소스 복사는 피하고 파일 형식·동작만 참고한다. 파일 생성 후 Windows `kicad-cli.exe`를 검증 오라클로 사용한다(§4의 wslpath 규칙 필수).

## 8. ERC와 수정 루프

### 8.1 기본 규칙 (SKIDL 인용, §5.1)

핀 타입 충돌 매트릭스 기반 — OUTPUT×OUTPUT/PWROUT×PWROUT/PWROUT×OUTPUT 에러, NOCONNECT 오접속, TRISTATE×OUTPUT 경고 등. 미연결 핀, 0/1핀 네트, 드라이브 강도 부족 체크.

### 8.2 확장 규칙 (MVP 필수 — §2에서 범위가 Codex 기준으로 확장됨에 따라 더 이상 후순위가 아님)

존재하지 않는 핀과 중복 참조번호, 입력 부동, 출력 간 충돌, 전원 입력 미공급(§5.2 PWR_FLAG로 해결), 전원·접지 역연결, I²C 풀업 누락(§6.4에서 값 출처 확인됨), MCU별 전원·리셋·부트·프로그래밍 핀 처리, 전원 핀별 디커플링 누락(§6.4에서 값 출처 확인됨), 통신 전압 도메인 불일치, 풋프린트 미지정 또는 핀 수 불일치, 의도하지 않은 단일 핀 넷과 명시되지 않은 NC. §9 Phase 3에서 구현하며, 이 규칙들과 함께 필요한 멀티유닛 심볼/`extends` 파싱(§5.2)도 같은 단계에서 도입한다.

### 8.3 검증 파이프라인 (모든 Phase 공통)

1. **파싱**: 생성된 `.kicad_sch`가 자체 S-expression 파서로 다시 읽히는지
2. **자체 ERC**: §8.1(+해당 Phase부터는 §8.2)
3. **KiCad 구문 검사**: `.kicad_sch`가 KiCad에 로드되는지
4. **KiCad ERC**: `kicad-cli.exe sch erc --format json --exit-code-violations`(§4 wslpath 규칙) — 0 위반이 목표
5. **SVG 기하 검사**: `kicad-cli.exe sch export svg` 렌더 성공 + 심볼·필드 중첩 0건, 심볼을 통과하는 와이어 0건, 잘못된 접합점 0건
6. **커넥티비티 라운드트립**: IR 단계에서 의도한 넷 연결과 최종 파일의 연결이 일치하는지(Circuit IR ↔ KiCad 추출 넷리스트 동등성)

### 8.4 수정 루프

- 요구사항 승인 전에는 회로를 생성하지 않는다.
- §8.3의 6단계를 순서대로 실행한다.
- **오류 수정은 전체 회로 재생성이 아니라 Circuit IR JSON Patch로 수행한다** — 이것이 §4의 8k 컨텍스트 예산 제약을 실제로 지키는 메커니즘이다. 매 수정 라운드트립에는 현재 IR 전체와 대화 이력을 다시 보내지 않고, **현재 IR + 수정 대상 위반 사항 + 트리밍된 후보 부품 필드**만 보낸다(전체 라이브러리 덤프·미사용 부품의 전체 핀 테이블 금지).
- 동일 오류가 반복되거나 3회 내 해결되지 않으면 자동 수정을 중단하고 사용자에게 원인과 선택지를 제시한다.
- ERC 오류를 자동 제외하거나 숨기지 않는다.
- 최종 승인된 리비전은 이후 자동 수정하지 않는다.

## 9. 로드맵

> **진행 상황 (2026-08-07)**: Phase 0·1·2 완료. `src/circuitgen/` 패키지 + 54개 테스트. 골든 회로 1번(`golden/golden_led_button.kicad_sch`)과 74LS00 멀티유닛 IC 회로가 KiCad ERC 0건 + 넷리스트 라운드트립 통과. 주요 실측 발견: (1) 핀 변환은 시트축 CW 회전 + 회전 후 미러 — 잘못된 공식도 ERC는 통과하지만 넷이 교차되므로 라운드트립 검증이 필수였음, (2) `extends` 파생 심볼은 KiCad `Flatten()`처럼 평탄화해서 임베드해야 함(그대로 넣으면 핀이 전부 사라짐), (3) llama-server는 아직 미기동(Phase 4 전까지 불필요). 비고: venv는 3.12가 아니라 시스템 3.14.4로 구성(SKIDL·전체 스택 호환 확인됨).

- **Phase 0 — 환경/스모크 테스트 + 기반 구축** ✅: WSL2→Windows `localhost` HTTP 연결 실측, Python venv, `git init`, `pip install skidl` 1회 설치로 골든 레퍼런스 생성. FastAPI/HTMX 스켈레톤, Windows llama.cpp·KiCad CLI 어댑터(wslpath 래퍼 포함), 설정·로그·리비전 저장 구조 구현.
- **Phase 1 — 워킹 스켈레톤 (첫 번째 대상: 수동소자+전원, §10 골든 회로 1번과 동일)** ✅:
  1. **코드 작성 전, 가장 먼저**: R+LED+`SW_Push`(버튼, 2핀)+`power:GND`+`power:+5V`로 구성된 `.kicad_sch`를 손으로 직접 작성(스텁+라벨 방식)하고 `kicad-cli sch erc --format json`이 0 위반으로 통과할 때까지 반복 수정 — 이 회로가 그대로 §10 골든 회로 1번이 된다. `lib_symbols` 인라인 방식, `version 20260306` 스탬프, 전원 심볼의 `#PWR01` 관례, `PWR_FLAG` 필요 여부, `.kicad_pro` 동반 필요 여부와 ERC severity 설정(풋프린트 체크 제외 여부)을 이 단계에서 전부 확정 — 포맷 이해 오류와 생성기 구현 오류를 분리하기 위함.
  2. `pin_absolute_position()` 구현 + 골든 레퍼런스 기준 단위 테스트.
  3. IR 스키마 → 넷리스트 생성기 → 자체 ERC(§8.1) → 단순 그리드 배치 → 스텁+라벨 이미터 → `kicad-cli sch erc` 게이트.
  4. 목표: 골든 레퍼런스와 동등한 회로를 파이프라인이 자동 생성 + ERC 통과. 이 단계는 아직 LLM 없이 결정론적 코드만으로 관통한다.
- **Phase 2 — 부품 계층 + 다핀 IC 지원 (MVP 필수)** ✅: KiCad 10 심볼 파서, 멀티유닛 심볼(`_유닛_바디스타일`)·`extends` 상속 파싱(§5.2), §5.3 우선순위 기반 다중 라이브러리 인덱스(SQLite FTS5 — 270개 라이브러리/24,232심볼/830,761핀, 76초 빌드), 검색·핀 조회 API, 라이선스·provenance 기록, §6 기준으로 큐레이션한 지식 인덱스 구축(3단계 티어 추출 파이프라인, §6.2 — 총 34개 엔트리: 1차분 PEFI 10개 + 2차분 Floyd 7개(555 공식·fan-out·미사용입력·로직레벨 호환표)/Sadiku 9개(op-amp 이득식·필터 컷오프)/Sedra 8개(PIV 마진·리플·1/3 바이어스 규칙·Wien-bridge), 전부 PyMuPDF로 원문 페이지 재검증 완료, 표 파손 항목은 Tier B로 정직하게 표기).
- **Phase 3 — 회로 계층 + MCU ERC 확장 (MVP 필수)**: `RequirementSpec`/Circuit IR 계층·Bus·Net 연결, §8.2 확장 ERC 규칙 구현, 디커플링 배치 규칙, §7.5의 라벨 밀집 완화 휴리스틱 도입. 이 시점부터 MCU 회로가 결정론적 파이프라인만으로 ERC를 통과할 수 있어야 한다(아직 LLM 미통합).
- **Phase 4 — 에이전트 통합**: `RequirementSpec` 정규화·승인 UI, §7.3 도구 호출 루프, JSON Schema/GBNF 강제 출력, ERC 실패 → JSON Patch 기반 수정 루프(§8.4). §10의 5종 골든 회로 전체를 프롬프트→생성 경로로 검증.
- **Phase 5 — 통합 검증 및 MVP 완료**: §10의 5종 골든 회로 전체가 자체 ERC·KiCad ERC 0건, §11의 테스트 매트릭스 통과. 최종 승인 UI 완성.
- **Phase 6 (선택, 장기)**: A* 그리드 배선 고도화(§7.5, 라벨 전용에서 하이브리드로), 계층 시트 지원(골든 회로 5종엔 불필요하나 `RequirementSpec`의 Sheet/Block 개념 완성을 위해), DigiKey/OLIMEX 레거시 라이브러리 변환·편입(`kicad-cli sym upgrade`), 공식 데이터시트 검색, SPICE 검증, PCB 배치·배선·DRC, 대규모 데이터셋 수집·파인튜닝(§6.5에서 확인했듯 이 프로젝트의 PDF 컬렉션과는 무관한 별도 트랙).

## 10. 골든 테스트 회로 (Phase 5 완료 기준)

1. LED·전류 제한 저항·버튼 입력 (= §9 Phase 1의 손으로 작성한 목표 회로와 동일 — 가장 먼저 통과해야 함)
2. MCU 최소회로, 디커플링, 리셋, SWD 헤더
3. MCU + I²C 센서 및 풀업
4. MCU + SPI 메모리
5. MCU + UART·전원 커넥터·상태 LED

## 11. 테스트 매트릭스

- 한글·영문 및 단위가 섞인 요구사항 정규화
- 정확한 라이브러리 ID와 핀 번호만 사용하는지 검증(존재하지 않는 부품·핀 거부)
- Bus slicing, 다중 유닛 심볼, 전원 심볼, NC, 계층 포트
- 고의로 주입한 미연결·출력 충돌·풀업 누락·핀 오배선 검출
- Circuit IR 직렬화·역직렬화와 넷 연결 보존
- `.kicad_sch` 파싱, SVG export, KiCad ERC JSON 생성
- Circuit IR과 KiCad가 추출한 넷리스트의 동등성
- 심볼·필드 중첩 0건, 심볼을 통과하는 와이어 0건, 잘못된 접합점 0건
- 모델 timeout·비정상 JSON·서버 중단·GPU OOM 시 복구와 사용자 오류 표시
- 승인 전후 리비전 불변성과 동일 입력의 결정론적 재현

## 12. MVP 완료 기준

- §10 골든 회로 5종이 KiCad 10에서 정상 열림
- 모든 골든 회로에서 자체 ERC·KiCad ERC 오류 0건
- 존재하지 않는 부품·핀을 포함한 산출물이 생성되지 않음
- 요구사항 승인과 최종 승인이 모두 감사 로그에 남음
- 인터넷 연결 없이 전체 워크플로 동작
- §3의 비목표(고전압/안전필수 회로 등) 요청은 생성 거부 + 사유 설명

## 13. 라이선스·안전 관련 명시적 원칙

- SKIDL에서 옮겨 쓰는 코드·알고리즘에는 원저작자와 MIT 라이선스를 보존한다.
- KiCad GPL 소스는 동작 참고·검증에만 사용하고, 직접 복사할 경우 별도 라이선스 검토를 거친다.
- 전공서적은 §6에서 정의한 범위(검색 그라운딩용 큐레이션)로만 사용하며, 원문을 재배포하거나 학습 데이터로 사용하지 않는다.
- 부품 가용성·가격·제조 상태는 완전 오프라인 조건상 보장하지 않는다.
- 생성된 회로는 항상 "엔지니어 검토가 필요한 초안"으로 취급하고, 특히 §3에서 배제한 고위험 도메인에는 사용하지 않는다.
