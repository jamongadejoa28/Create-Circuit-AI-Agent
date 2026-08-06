# KiCad 회로도 생성 AI 에이전트 — 프로젝트 계획

## Context

이 프로젝트 디렉터리(`/home/hajun/dev_ws/create_circuit`)에는 이미 상당한 사전 준비가 되어 있다: SKIDL 소스, KiCad 10 전체 소스 미러, 4개의 실전 부품 라이브러리(ESP/OLIMEX/SparkFun/Digikey), 전공서적 PDF 8권, 그리고 대규모 학습 데이터셋 설계에 관한 사전 리서치 보고서. 목표는 이 자료들을 바탕으로, 자연어 프롬프트를 입력받아 **유효한 KiCad 회로도(`.kicad_sch`) 파일을 생성**하는 로컬 AI 에이전트를 만드는 것 — 로컬 GPU(RTX 4060 8GB)에서 llama.cpp로 구동하는 Qwen 모델을 사용하고, 클라우드 LLM이나 벤더 부품 API에는 의존하지 않는다.

**v1 목표 범위** (사용자 확정): 단일 시트, 2핀 수동소자(R/C/LED/Diode)와 전원 심볼(GND/+5V/+3V3 등)만으로 구성된 회로. 다핀 IC(멀티유닛/`extends` 상속)와 계층 시트(hierarchical sheet)는 Phase 3로 명시적으로 미룸 (아래 로드맵 참조).

**SKIDL 활용 범위에 대한 해석** (사용자가 "SKIDL의 넷리스트 생성/ERC/계층설계/Part·Net·Bus·Pin API는 인용하되, 부품 검색과 회로도 그리기 EDA 부분은 미숙하므로 직접 구현하라"고 지시한 것을 아래와 같이 해석하고 계획을 세움): 조사 결과 SKIDL은 실제로 부품 검색(`part_query.py`)과 회로도 자동 배치·라우팅(`schematics/place.py`+`route.py`) 기능을 갖고 있으나, 이 스키매틱 생성 기능은 KiCad 10 지원이 이 대화 시점 기준 **약 1주일 전**(2.3.0, 2026-07-28)에 막 출시되었고, 자체 테스트에 출력 품질 검증이 전무하며, 현실적 계층 회로 하나는 실패가 예상된다고 `xfail`로 커밋되어 있는 등 미성숙함이 코드로 확인됨 — "미숙하다"는 SKIDL의 해당 기능을 가리키는 것으로 읽었고, 이 계획은 그 해석을 그대로 채택한다 (다르게 의도했다면 정정 요청 바람). 따라서 넷리스트/ERC/계층설계/API는 SKIDL의 설계를 적극 인용·포팅하고, 부품 검색과 배치·드로잉은 처음부터 직접 구현한다 (상세 근거와 구체적 인용 범위는 아래 "SKIDL 내부 구조 조사 결과" 절).

## 확인된 환경 정보 (직접 검증 완료)

- **GPU/CUDA**: RTX 4060 8GB, CUDA 13.3, WSL2에서도 `nvidia-smi` 정상 동작 (GPU 패스스루 확인). 유휴 시 VRAM 약 1.1~1.3GB 사용 중 — 8GB 중 여유는 넉넉하지 않음.
- **KiCad 10.0.5**: Windows에 설치됨 (`C:\Program Files\KiCad\10.0`). `kicad-cli.exe`를 WSL2 bash에서 **직접 호출 가능** 확인 (`kicad-cli.exe --version` → `10.0.5` 정상 응답).
  - `kicad-cli sch erc` 서브커맨드로 ERC 실행 가능, `--format json` 옵션으로 **기계 판독 가능한 검증 결과** 획득 가능 — 자동 검증 루프의 핵심 도구.
  - `kicad-cli sch export` (svg/pdf/netlist/bom 등)도 존재 — 생성된 스키매틱 렌더링/검증에 활용 가능.
  - **실측 검증 완료 (중요, 구현 시 필수 반영)**: WSL2 bash에서 `kicad-cli.exe`에 리눅스 스타일 경로(`/home/...`)나 단순 `/mnt/c/...` 문자열을 그대로 넘기면 **"회로도 로드 실패" (exit 3)로 실패**한다 — Windows 프로세스가 인자 문자열을 해석 못 함. 반드시 **`wslpath -w <path>`로 변환한 경로**를 넘겨야 함. 변환하면 WSL2 네이티브 경로(`/home/hajun/...`)도 `\\wsl.localhost\Ubuntu-24.04\...` UNC 경로로 변환되어 **파일을 `/mnt/c`로 복사할 필요 없이 바로 동작** — 실제 데모 스키매틱(`kicad-source-mirror-10.0.5/demos/interf_u/interf_u.kicad_sch`)에 대해 ERC 실행 → exit 0, 구조화된 JSON 위반 리포트(심각도/설명/좌표/UUID 포함) 정상 수신 확인. 즉, 검증 파이프라인은 WSL2 파일시스템에 그대로 두고 얇은 `wslpath -w` 래퍼 함수 하나로 `kicad-cli.exe` 호출을 감싸면 됨. 자동 게이트에는 `--exit-code-violations` 플래그로 위반 존재 시 nonzero exit 활용.
  - KiCad 자체 번들 심볼 라이브러리 확인: `share/kicad/symbols/`에 224개 `.kicad_sym` 파일 (R/C/L, Amplifier, CPU, Connector 등 표준 부품 총망라), `share/kicad/footprints/`에 155개 `.pretty` 풋프린트 디렉터리. 이것이 부품 검색의 **1차 기준 라이브러리**이며, 프로젝트에 이미 받아둔 ESP/OLIMEX/SparkFun/Digikey 라이브러리는 특정 벤더 보드/모듈 보강용 2차 자료.
- **llama.cpp**: Windows에 소스 빌드 완료 (`C:\Users\hajun\llama.cpp\build\bin\Release\`). `llama-server.exe` 존재 (OpenAI 호환 API 서버). **GBNF 문법 강제 디코딩**(`--grammar`, `--grammar-file`)과 **JSON Schema 강제 디코딩**(`--json-schema`, response_format의 `json_schema` 타입) 둘 다 서버에서 공식 지원 확인 (`tools/server/README.md`) — LLM 출력의 구조적 실패를 원천 차단할 수 있는 핵심 레버.
  - WSL2 bash에서 `llama-server.exe`를 직접 실행하면 종료 코드 53으로 즉시 실패 (CUDA/DLL 탐색 경로 문제로 추정). → **운영 방식**: 에이전트 프로세스가 서버를 직접 기동/관리하지 않고, 사용자가 Windows 쪽에서 기동한 뒤 에이전트는 순수 HTTP 클라이언트로만 통신 (아래 Day-1 스모크 테스트로 확정 필요).
  - `convert_hf_to_gguf.py`, `convert_lora_to_gguf.py` 등 변환 스크립트 존재 — 추후 파인튜닝 결과물을 GGUF로 재변환해 배포하는 경로도 이미 갖춰져 있음.
- **로컬 모델**: `Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf` (~5.4GB), `Qwen_Qwen3.5-9B-Q4_K_M.gguf` (~6.2GB). 8GB VRAM 대비 두 모델 다 여유가 크지 않으며, **Qwen3.5-9B는 thinking 모드 폭주로 응답 생성 실패 이력 5/5** — 원인이 VRAM 부족이든 thinking 모드 자체 문제든, 대응은 동일: v1은 Qwen2.5-Coder-7B-Instruct를 기본 모델로 채택 (코드/구조화 출력 생성에도 적합), Qwen3.5-9B는 보조/실험적 위치로 격하.
- **WSL2 네트워킹**: `.wslconfig`에 `networkingMode=mirrored` 설정됨 — WSL2와 Windows가 `localhost`를 공유하므로, WSL2의 Python 에이전트가 `http://localhost:PORT`로 Windows의 `llama-server.exe`를 바로 호출 가능할 것으로 예상 (Day-1에 실측 검증 필요, 아래 참조).
- **Python 환경**: WSL2 기본 `python3`는 3.14.4 (매우 최신). SKIDL의 `setup.py`는 `python_requires=">=3.6"`이며 classifier에 3.14도 명시되어 있어 SKIDL 자체는 문제 없음. 다만 PyTorch/CUDA(cu132) wheel의 3.14 지원 여부는 미확인 — **RAG용 임베딩 모델을 GPU/PyTorch로 돌리는 방안은 VRAM 여유 부족과 겹쳐 v1 범위에서 제외**하고, 부품 검색은 KiCad 심볼의 구조화 메타데이터(키워드/설명/핀 등) 기반 어휘 검색으로 시작 (아래 아키텍처 절 참조). PyTorch/LibTorch(cu132)는 이미 설치 경로가 조사되어 있으므로, 추후 파인튜닝/커스텀 검증 모델이 필요해지면 바로 쓸 수 있는 예비 인프라로 남겨둠.
- **디스크/리소스**: 839GB 여유 디스크, 16 코어, 15GB RAM — 리소스 제약은 없음.
- **프로젝트 디렉터리**: `/home/hajun/dev_ws/create_circuit`는 아직 git 저장소 아님 (하위의 skidl/, kicad-source-mirror-10.0.5/, ESP/OLIMEX/SparkFun/digikey 라이브러리들은 각각 독립된 git 저장소 — 참고자료이며 수정 대상 아님).

## 참고 자료 조사 결과

### `docs/deep-research-report.md` (사전 리서치 보고서)

이 문서는 **"로컬 LLM 학습 데이터셋 설계"** 보고서로, 프롬프트 기반 회로/PCB 생성 에이전트의 *구현 아키텍처*가 아니라 대규모 파인튜닝용 데이터셋(500~50,000개 프로젝트 규모) 수집 전략을 다룸. SKIDL/Qwen/llama.cpp 등 실제 채택 스택은 전혀 언급되지 않음 — 즉, 이 보고서는 "지금 당장 로컬 모델로 에이전트를 만드는" 이 프로젝트의 실행 계획서가 아니라, 병행 가능한 **별도의 장기 리서치 트랙**(대규모 데이터셋 구축 후 파인튜닝)으로 취급해야 함.

다만 재사용 가치가 높은 두 가지:
1. **핵심 아키텍처 원칙**: "단일 LLM보다 LLM/LMM + 검색 시스템 + EDA 생성기 + 검증 도구의 조합" — 이번 계획의 방향(LLM은 구조화된 중간 표현만 생성하고, 결정론적 코드가 넷리스트/스키매틱을 만들고, 외부 도구가 검증)과 정확히 일치.
2. **검증 파이프라인 철학**: 파싱→라운드트립→ERC→커넥티비티(넷리스트 동치성)→(선택적) SPICE 순의 계층적 검증, 자유 텍스트 채점 대신 구조적 채점. 아래 "검증 전략" 절에 반영.

### SKIDL 내부 구조 조사 결과 — "인용할 것" vs "직접 구현할 것"의 경계 확정

**사용자 지시 재확인**: "SKIDL의 넷리스트 생성, ERC알고리즘, 계층 설계, Part/Net/Bus/Pin API관련 코드를 살펴보고 인용할 부분은 인용할 것. 단 부품 검색 및 회로도 그리는 EDA부분은 미숙하므로 직접 구현해야한다." — 여기서 "미숙하다"의 주어가 사용자 자신인지 SKIDL의 해당 기능인지는 원문만으로 100% 확정할 수 없었으나, 조사 결과 SKIDL 쪽이 맞다는 강력한 정황 증거를 확보함:

- SKIDL은 실제로 `부품 검색`(`part_query.py`, 로컬 SQLite 인덱스 기반 정규식 검색 — 벤더 API 연동 없음, 확인됨)과 `회로도 그리기`(`schematics/place.py`+`route.py`+`tools/kicad*/gen_schematic.py`, 실제 `.kicad_sch` 파일을 만드는 force-directed 배치 + switchbox 라우팅 엔진) 두 기능을 **정확히 그대로** 갖고 있음 — 사용자가 언급한 두 항목과 완전히 일치.
- 이 스키매틱 생성 기능은 **정말로 미숙한 시점**임이 코드로 확인됨: KiCad 6-9용 실제 편집 가능 스키매틱 생성은 2.2.2 (2026-04-03) 출시, KiCad 10 지원 및 구조 리팩터링은 2.3.0 (2026-07-28) 출시 — 이 대화 시점(2026-08-07) 기준 **약 1주일 전**. 자체 테스트 스위트에 실제 출력 품질(배치/라우팅 결과)을 검증하는 assert가 **단 하나도 없고**, 현실적인 계층 회로 하나는 실패가 예상된다며 `xfail`로 커밋되어 있으며, 코드 내 `# HACK: ad-hoc` 주석과 "실제 IC 검증 전엔 비활성화 상태로 둘 것" 같은 미검증 최적화 플래그가 존재.
- → **결론**: 원문의 "미숙" 판단은 근거가 확실하며, 계획은 이 해석을 그대로 채택함 (사용자에게 재확인 질문 불필요).

**채택 전략 — "포팅(citing), 런타임 의존 아님"**:

SKIDL을 `pip install skidl`로 런타임 의존성에 넣는 대신, 아래 네 파일에서 확인된 설계·알고리즘·상수를 **직접 인용/포팅하여 이 프로젝트 자체의 경량 모듈로 재구현**한다. 이유: (1) 우리의 IR은 LLM이 생성하는 구조화 JSON이라 SKIDL의 범용 Python DSL이 제공하는 연산자 오버로딩(`+=`, `&`, `|`)이나 다중 KiCad 버전 플러그인 시스템이 불필요하게 큼, (2) SKIDL 자체에 알려진 설계상 허점 존재 — 예: `erc_list`가 클래스 속성이라 `add_erc_function()` 호출 시 해당 클래스의 **모든 인스턴스에 전역으로** 규칙이 추가되는 부작용, 최상위 `generate_netlist()` 등은 import 시점에 `default_circuit`에 바인딩되어 `with Circuit() as ckt:` 블록 안에서도 엉뚱한 회로를 대상으로 동작할 수 있는 함정 — 이런 부작용을 상속하지 않기 위함, (3) 우리가 어차피 자체 배치/라우팅 엔진을 만들 것이므로, 그 엔진이 읽을 Part/Net/Pin 데이터 모델을 처음부터 "geometry in, wires out"에 맞게 깔끔하게 설계할 수 있음 (SKIDL 자체 스키매틱 엔진이 SKIDL의 라이브 객체에 알고리즘이 속성을 직접 주입(`part.tx`, `pin.pt` 등)하는 방식으로 결합되어 있어 별도 라이브러리로 분리하기 어렵다고 조사됨 — 우리는 이 결합을 처음부터 피함).

**"인용"할 구체 자산 (파일 경로 및 재사용 방식)**:

| 자산 | SKIDL 소스 위치 | 재사용 방식 |
|---|---|---|
| Part/Net/Bus/Pin 데이터 모델 설계 | `part.py`, `net.py`, `pin.py`, `bus.py` | 우리 IR에 필요한 범위로 축소 포팅 (핀 타입 enum, part-pin-net 관계 구조) |
| 핀 타입 충돌 매트릭스 (ERC 핵심 규칙) | `pin.py:1023-1085` (`conflict_matrix`) | 값 그대로 인용 — OUTPUT×OUTPUT=ERROR, PWROUT×PWROUT=ERROR, PWROUT×OUTPUT=ERROR, NOCONNECT 규칙, TRISTATE×OUTPUT=WARNING, UNSPEC×any=WARNING 등, 대칭 자동화 로직 포함 |
| 넷/파트 단위 기본 ERC 체크 | `erc.py` (136줄, `dflt_circuit_erc`/`dflt_part_erc`/`dflt_net_erc`) | 로직 패턴 인용 — 미연결 핀 경고, 0/1핀 네트 경고, 드라이브 강도 부족 경고 |
| 계층 설계 (`@subcircuit`) | `node.py` | 개념 인용 — 우리는 LLM IR에서 명시적 중첩 JSON으로 계층을 표현하므로 데코레이터 패턴 자체보다 "Node 트리 + 계층적 UUID 경로" 아이디어만 포팅 |
| 넷리스트 S-expression 구조 | `tools/kicad10/gen_netlist.py` | 구조 그대로 인용: `(export (version D) (design ..) (components (comp (ref..)(value..)(footprint..)(libsource..)(sheetpath..)(tstamps..)))(nets (net (code..)(name..)(node(ref..)(pin..)(pintype..))))))` |
| 결정론적 UUID 스킴 | `gen_netlist.py`/`sexp_schematic.py` (namespace `7026fcc6-e1a0-409e-aaf4-6a17ea82654f`, `uuid.uuid5`) | 동일 패턴 채택 — 향후 PCB 단계로 확장 시 스키매틱↔PCB 상호 참조 호환성 확보 |
| `.kicad_sym` 파서 구조 | `tools/kicad10/lib.py` | 파싱 로직 참고 (S-expression → `/kicad_symbol_lib/symbol` 순회, KiCad6+ `extends` 상속 처리) — 실제 파싱은 `simp_sexp`(아래) 이용해 직접 구현 |
| **S-expression 저수준 파서** | 의존 패키지 `simp_sexp` (SKIDL이 실제 사용) | **이것만은 실제 pip 의존성으로 채택** — KiCad 파일의 S-expression 읽기/쓰기라는 좁고 이미 해결된 문제를 재발명하지 않음 |
| 배치 알고리즘 아이디어 | `schematics/place.py` (force-directed, Fruchterman-Reingold 계열 + 20개 이상 부품은 BFS row-based로 전환) | 알고리즘 패턴만 참고, 코드는 직접 구현 |
| 라우팅 알고리즘 아이디어 | `schematics/route.py` (전역 미로 라우터 + switchbox 단위 greedy 상세 라우팅, 학술 논문 인용: doi.org/10.1016/0167-9260(85)90029-X) | 알고리즘 패턴만 참고, 코드는 직접 구현 |

**개발 중 1회성 활용(런타임 의존 아님)**: 개발 초기에 `pip install skidl`로 임시 설치 후, 예제 회로 하나를 `generate_schematic()`으로 실제로 생성해 **골든 레퍼런스 `.kicad_sch` 파일**로 보관 — 우리가 만들 커스텀 이미터가 만들어내는 `lib_symbols` 블록, UUID 스킴, 좌표계(KiCad는 Y-down) 등의 구조를 이 파일과 비교/검증하는 용도로만 사용하고, 런타임 파이프라인에는 포함하지 않음. **단, 이 파일을 헤더/버전 스탬프의 기준으로 삼지 말 것** — SKIDL의 `kicad10/backend.py`는 `20230409`(KiCad 8/9 시절 값)를 하드코딩하고 있음이 확인되어 있으므로(아래 KiCad 파일 포맷 절 참조), 이 골든 레퍼런스는 어디까지나 **구조 참고용**이고, `version`/포맷 스탬프 등 헤더 필드의 정답은 아래 Phase 1의 **손으로 직접 작성한 파일**이 담당한다 (두 골든 레퍼런스 중 어느 쪽이 무엇의 기준인지 혼동하지 말 것).

**참고— Digikey 라이브러리 포맷 주의**: `digikey-kicad-library/digikey-symbols/`는 최신 `.kicad_sym`이 아니라 **레거시 텍스트 포맷**(`.lib` 1223개 + `.dcm` 150개)임이 확인됨 (ESP/SparkFun/OLIMEX 및 KiCad 번들 라이브러리는 전부 최신 `.kicad_sym` 포맷으로 확인). v1의 부품 검색은 최신 포맷 라이브러리(KiCad 번들 + ESP/SparkFun/OLIMEX)로 시작하고, Digikey는 KiCad 자체 변환 기능으로 `.kicad_sym`으로 **1회 일괄 변환하는 준비 작업**(런타임 로직 아님)을 별도 설정 단계로 넣어 나중에 포함.

### KiCad 파일 포맷 조사 결과 — 커스텀 EDA 생성기가 반드시 알아야 할 것

**가장 중요한 발견 (설계를 바꾸는 수준): `.kicad_sch`에는 `(net ...)` 객체가 아예 없다.** 245줄짜리 실제 데모 파일 전체를 읽고 전수 검색해도 net 엔티티가 존재하지 않음 — **연결 관계는 순수하게 좌표 일치로만 결정**된다:
- `(wire (pts (xy x1 y1)(xy x2 y2)) ...)`는 그냥 선분이며, 선분의 끝점이 핀의 실제 좌표(심볼의 `(at ..)` 배치 변환 + 심볼 정의 내 핀의 로컬 오프셋/회전을 합산한 좌표)나 다른 wire의 끝점/`(junction)`과 **정확히 일치**해야만 전기적으로 연결된 것으로 인식됨.
- T자 교차점을 관통하는 하나의 긴 wire는 교차점에서 연결되지 않음 — 반드시 `(junction)`에서 두 개의 별도 segment로 **분리(split)**해야 함. SKIDL 자체 코드 주석이 이 함정을 명시적으로 경고: "Without this, a junction in the middle of a wire does not create separate connectivity segments."
- **대안: 이름 기반 연결(label)**. `(label)`(시트 내부), `(global_label)`(프로젝트 전역), `(hierarchical_label)`(계층 시트 포트)은 좌표가 달라도 **같은 텍스트면 같은 net으로 취급**됨 — wire를 전혀 그리지 않고도 유효한 연결을 만들 수 있는 정식 매커니즘.

**→ v1 아키텍처 결정: 배치(placement)만 직접 구현하고, 전역 와이어 라우팅(멀리 떨어진 두 핀 사이의 경로 탐색 + 교차점 분리)은 v1 범위에서 제외, 각 핀에서 짧은 스텁(stub) 와이어를 뽑아 그 끝에 label을 붙이는 방식으로 연결을 표현한다.**

**주의 — label만으로는 부족함(수정된 이해)**: label은 좌표가 정확히 핀이나 wire 위에 있어야 그 net에 연결된 것으로 인식된다 — 허공에 떠 있는 label은 KiCad가 "미연결"로 판정한다. 따라서 최소 구현은 "배치 + 아무 데나 label"이 아니라: **핀의 절대 좌표를 정확히 계산**(심볼의 `(at x y angle)` 배치 변환을 `lib_symbols`에 캐시된 핀의 로컬 오프셋/회전에 적용) → 그 좌표에서 시작하는 **짧은 스텁 wire 하나**를 뽑음 → 스텁의 반대쪽 끝에 label을 배치. 이것이 정확히 SKIDL의 `auto_stub` 폴백이 하는 일이다 (label만 놓는 게 아니라 "stub + label").

이렇게 해도 여전히 크게 남는 이득:
- 전역 라우터(미로 탐색), switchbox 그리드, 두 wire 간 교차점 분리, 배선 혼잡 회피 같은 **가장 어려운 부분은 전부 제거됨** — 필요한 것은 "핀 절대좌표 계산 + 그 자리에서 뻗어나가는 정해진 길이의 스텁 하나"뿐, 임의의 두 점을 잇는 경로 탐색이 아님.
- 이 방식은 편법이 아니라 **SKIDL의 실제 라우팅 엔진 자체가 라우팅 실패 시 최종 폴백으로 쓰는 정식 전략**(`auto_stub`)과 동일 — 실제 프로덕션 KiCad 회로도에서도 흔히 쓰이는 정상적인 표현 방식.
- **남는 필수 작업이자 Phase 1 최우선 과제**: `pin_absolute_position(symbol_placement, lib_pin)` 함수 하나 — 심볼 배치 변환과 핀 로컬 좌표를 합성해 절대좌표를 내는 순수 함수. 이것이 틀리면 스텁이 핀에서 살짝 벗어나 ERC가 "미연결 핀"으로 조용히 실패하는, 가장 흔하고 디버깅하기 까다로운 실패 모드가 된다 — 골든 레퍼런스 파일(아래)에 대한 단위 테스트로 반드시 검증.
- 전역 와이어 라우팅(임의 경로 탐색, SKIDL의 switchbox 라우터 수준)은 **v1 이후 스트레치 목표**로 미룸.

**그 외 포맷 핵심 사실**:
- **자기완결적 파일**: 스키매틱이 사용하는 모든 심볼의 전체 정의(그래픽+핀)가 `(lib_symbols ...)` 블록에 **그대로 캐시되어 내장**됨 — `sym-lib-table` 없이도 렌더링 가능. 우리 생성기는 참조하는 심볼을 라이브러리에서 찾아 `lib_symbols`에 통째로 복사해 넣으면 되고, 별도의 라이브러리 테이블 파일을 만들 필요가 없음.
- **버전 번호는 날짜 스탬프**(semver 아님). 이 저장소(KiCad 10.0.5) 기준 최신 값은 `eeschema/sch_file_versions.h`에서 확인: 스키매틱 `20260306`, 심볼 라이브러리 `20251024`. **주의**: SKIDL의 `tools/kicad10/backend.py`는 실제로는 **`20230409`(KiCad 8/9 시절 값)를 하드코딩**하고 있음이 확인됨 — "kicad10" 폴더명과 달리 실제 KiCad 10 포맷 스탬프로 완전히 갱신되지 않은 상태. SKIDL을 그대로 믿지 말고 우리 생성기는 `sch_file_versions.h`에서 확인한 실제 최신 스탬프를 사용해야 함.
- **심볼 구조**: `(symbol "Name_유닛번호_바디스타일번호" ...)` 규칙 (유닛 0 = 모든 유닛 공통 그래픽, 바디스타일 >1 = De Morgan 대체 표현). `(pin <전기타입> <그래픽스타일> (at x y angle) (length L) (name "N")(number "N"))` — 전기타입 값(`input/output/bidirectional/tri_state/passive/free/unspecified/power_in/power_out/open_collector/open_emitter/no_connect`)은 SKIDL의 `pin_types`와 거의 1:1 매핑되어 그대로 인용 가능. 일부 심볼은 `extends`(상속) 사용 (OLIMEX `Used-In-KiCad_v7`에서 5건 확인) — 파서가 처리해야 함.
- **포맷 그라운드 트루스 위치** (실제 파서/작성자 C++ 소스, 필요시 직접 대조): `kicad-source-mirror-10.0.5/eeschema/schematic.keywords`(전체 토큰 목록 ~180개), `eeschema/sch_file_versions.h`(버전별 변경사항 changelog), `eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr_parser.cpp`+`sch_io_kicad_sexpr.cpp`+`sch_io_kicad_sexpr_lib_cache.cpp`(실제 리더/라이터).
- **새 IPC API(`api/`)는 스키매틱 쪽이 미완성** — `schematic_commands.proto`는 명령이 **0개**, C++ 핸들러(`eeschema/api/api_handler_sch.cpp`)에 `// TODO` 스텁 다수 확인 (PCB 쪽 `board_commands.proto`는 37개 명령으로 훨씬 성숙). → **`kicad-cli.exe`가 유일한 실전 자동화 경로**라는 우리의 기존 결론이 소스 레벨에서도 확정됨. `pcbnew` Python(SWIG) 바인딩도 PCB 전용이며 eeschema 대응품 없음 확인.
- **`kicad-cli sym upgrade`** 서브커맨드 존재 확인 — Digikey/OLIMEX 레거시 `.lib`/`.dcm`을 최신 `.kicad_sym`으로 변환하는 준비 스크립트에 바로 사용 가능.

**라이브러리 실측 규모** (모두 `/home/hajun/dev_ws/create_circuit/` 하위):

| 라이브러리 | 포맷 | 심볼 수 | 비고 |
|---|---|---|---|
| KiCad 번들 (`C:\Program Files\KiCad\10.0\share\kicad\symbols`) | 최신 | 224개 파일 (R/C/L 등 표준 부품 총망라) | 1차 기준 라이브러리 |
| ESP-kicad-libraries | 최신 (`20251024`) | 49 | ESP32/8266 전 모듈 |
| SparkFun-KiCad-Libraries | 최신 (동일 버전) | 761 (31개 파일) | `SparkFun-MicroMod.kicad_sym`은 실제로 빈 파일(0개) — 디버깅 시 혼동 주의 |
| OLIMEX (`Used-In-KiCad_v7/`) | v7 (2세대 구버전이나 KiCad10이 로드 시 자동 업그레이드 가능) | 638 | v6/v7 폴더만 존재, v8+ 없음 — v7 사용 권장 |
| digikey-kicad-library | **레거시** (`.lib`+`.dcm`, `.kicad_sym` 0개) | 1073 | `kicad-cli sym upgrade` 1회 변환 필요 |

즉 변환 없이 즉시 쓸 수 있는 최신 포맷 부품만 합쳐도 (KiCad 번들 + ESP + SparkFun + OLIMEX v7) 광범위한 커버리지 확보 — 벤더 API 연동 없이 로컬 라이브러리만으로 v1에 충분.

---

## 최종 시스템 아키텍처

### 비목표 (Non-goals) — 범위를 명확히 고정

사용자가 요청한 것은 **"회로도(스키매틱) 생성"**이다. `docs/deep-research-report.md`가 다루는 PCB 배치/배선/DRC/SI/PI/열설계/Gerber·IPC-2581 생산 출력물 등은 스키매틱 이후 단계이며 **이번 프로젝트 범위에 포함하지 않는다** (별도의 장기 트랙으로 남겨둠). 마찬가지로 SPICE 시뮬레이션(아날로그 동작 검증)도 v1 비목표 — ERC(연결 규칙 검증)까지만 다룬다.

### 파이프라인

```
[자연어 프롬프트]
      ↓
[1. LLM: 프롬프트 → 구조화 회로 IR (JSON)]   ← Qwen2.5-Coder-7B-Instruct, llama-server.exe (Windows)
      ↓                                         grammar/json_schema 강제 디코딩으로 구조 실패 원천 차단
[2. 결정론적 코드: 부품 검색/매칭]            ← KiCad 심볼 메타데이터(키워드/설명) 어휘 검색 (RAG/임베딩 아님, v1)
      ↓
[3. 결정론적 코드: IR → 넷리스트]              ← SKIDL 넷리스트 S-expression 구조 인용
      ↓
[4. 결정론적 코드: ERC]                        ← SKIDL 핀 충돌 매트릭스 인용
      ↓ (실패 시 3으로 피드백)
[5. 커스텀 배치 엔진: 심볼 위치 결정]          ← SKIDL place.py 알고리즘(force-directed) 아이디어만 참고, 직접 구현
      ↓
[6. 커스텀 이미터: .kicad_sch 작성]            ← 스텁 wire + label 기반 연결(v1), lib_symbols 인라인, simp_sexp로 S-expr I/O
      ↓
[7. kicad-cli.exe sch erc --format json]       ← Windows 바이너리, wslpath -w 래퍼로 호출, 실제 KiCad 검증
      ↓ (위반 시 JSON 리포트를 LLM에 피드백 → 반복 수정)
[검증된 .kicad_sch 완성]
```

### 핵심 설계 제약 (VRAM/컨텍스트 예산)

RTX 4060 8GB 중 유휴 사용량(~1.3GB) + Qwen2.5-Coder-7B-Q5_K_M 모델 자체(~5.1GB)를 빼면 KV 캐시/연산 버퍼에 남는 여유가 **~1.6GB 수준으로 빠듯함** → `--ctx-size`는 32k가 아닌 **8k 안팎으로 제한**해서 운용해야 함. 이는 아키텍처에 실질적 제약을 가함: **IR 스키마와 부품 검색 결과(RAG 페이로드)는 항상 컴팩트하게 유지** — 후보 부품은 필요한 필드만 트리밍해서 몇 개만 반환하고, 전체 라이브러리 덤프나 사용하지 않는 부품의 전체 핀 테이블을 프롬프트에 포함하지 않는다. 그렇지 않으면 검증 실패 → LLM 재수정 피드백 루프의 두 번째 턴에서 컨텍스트가 바로 초과됨.

### 검증 전략 (deep-research-report의 계층적 검증 철학 반영)

1. **파싱**: 생성된 `.kicad_sch`가 우리 자체 S-expression 파서로 다시 읽히는지 (기본 문법 무결성)
2. **ERC**: `kicad-cli.exe sch erc --format json --exit-code-violations` — 0 위반이 목표. 위반 발생 시 JSON 리포트(설명/좌표/UUID)를 LLM에게 피드백하여 재수정 (report의 "진단/복구" 태스크 개념과 일치)
3. **실제 KiCad에서 열림 확인**: `kicad-cli.exe sch export svg`로 렌더링 성공 여부 확인 (사람이 육안 검증 가능한 산출물)
4. **커넥티비티 라운드트립**: 우리가 의도한 넷 연결(IR 단계)과 최종 파일의 label 기반 연결이 일치하는지 자체 검증

---

## 로드맵

- **Phase 0 — 환경/스모크 테스트**: WSL2→Windows `localhost` HTTP 연결 실측 (사용자가 Windows에서 `llama-server.exe` 기동 후 WSL2에서 `curl http://localhost:PORT/v1/models`), Python 3.12 venv 준비, `git init`, `pip install skidl` 1회 설치로 골든 레퍼런스 스키매틱 생성.
- **Phase 1 — 최소 파이프라인**:
  1. **가장 먼저, 코드 작성 전에**: R + LED + `power:GND` + `power:+5V`로 구성된 `.kicad_sch`를 **손으로 직접 작성**하고 (스텁 wire + label 방식 사용), `kicad-cli sch erc --format json`이 0 위반으로 통과할 때까지 반복 수정. 이 파일이 이후 모든 코드의 스펙이 됨 — 정확한 `lib_symbols` 인라인 방식, 올바른 `version 20260306` 스탬프, 전원 심볼의 `(property "Reference" "#PWR01" ...)` 관례, `.kicad_pro` 동반 파일 필요 여부(데모 파일 두 곳에서 ERC 위반 개수가 3개/1개로 다르게 나온 원인으로 추정 — 주로 풋프린트 라이브러리 해석 관련 노이즈로 보임, 프로젝트 컨텍스트 유무 차이인지 확인 필요) 등을 이 단계에서 전부 확정한다. "생성기부터 만들고 왜 ERC가 거부하는지 디버깅"하는 순서를 피하기 위함 — 포맷 이해 오류와 생성기 구현 오류를 분리. 이 단계에서 반드시 확정할 두 가지:
     - **`power:PWR_FLAG` 함정 예상**: `power:GND`/`power:+5V` 심볼의 핀은 `power_in`이라 이를 구동하는 `power_out` 핀이 없으면 KiCad ERC가 기본적으로 "Input power pin not driven by any Output Power pins" 에러를 낸다 — 토폴로지가 완벽해도 이 파일은 처음엔 ERC가 실패할 것으로 예상. 표준 해법은 전원 net마다 `power:PWR_FLAG`(핀이 `power_out`) 하나씩 추가하는 것 — 실제로 이 에러가 뜨는지, PWR_FLAG 추가로 해소되는지 확인하고, 이후 넷리스트/이미터가 전원 net마다 PWR_FLAG를 자동으로 붙이도록 설계에 반영.
     - **`.kicad_pro` 필요 여부와 ERC severity 설정**: 두 데모 실행에서 위반 개수가 갈린 원인이 주로 풋프린트 라이브러리 해석(스키매틱 전용 프로젝트에는 노이즈)이라면, ① 생성된 스키매틱마다 풋프린트 체크를 제외한 최소 `.kicad_pro`를 동반 생성하거나, ② LLM 피드백 루프에 JSON 리포트를 넘기기 전에 해당 위반 키를 필터링하거나 — 이 둘 중 하나를 이 단계에서 확정한다 (Phase 2 중간에 발견해서 되돌아가는 상황을 피하기 위함).
  2. `pin_absolute_position()` 함수 구현 + 골든 레퍼런스 파일 기준 단위 테스트.
  3. IR 스키마 정의 → 넷리스트 생성기 → ERC 엔진 → 배치 엔진(단순 그리드/행 배치) → 스텁+label 기반 `.kicad_sch` 이미터 → `kicad-cli sch erc` 게이트.
  4. 목표: 골든 레퍼런스와 동등한 회로를 파이프라인이 처음부터 끝까지 자동 생성 + ERC 통과.
- **Phase 2 — LLM 통합**: llama-server.exe 연동, JSON Schema/GBNF 강제 디코딩 적용, 부품 검색(어휘 기반) 연동, ERC 실패 → LLM 재수정 피드백 루프. (v1 확정 범위: 단일 시트, 2핀 수동소자(R/C/LED/Diode)+전원 심볼만 — 다핀 IC·계층 시트는 Phase 3로 명시적으로 미룸.)
- **Phase 3 — 확장 (두 단계로 분리, 난이도 순)**:
  1. **다핀 IC 지원**: 멀티유닛 심볼(`_유닛_바디스타일` 구조), `extends` 상속 파싱, IC 지원 수동소자(디커플링 캡 등) 배치 규칙.
  2. **계층 설계**: 서브서킷 → 별도 `.kicad_sch` 시트 파일 분리, `hierarchical_label` ↔ 부모 시트의 `(sheet (pin "NAME" input ...))` 매칭. SKIDL 자체도 이 수준 복잡도에서 `xfail`(예상된 실패)이 커밋되어 있을 만큼 난이도가 높은 지점이므로, Phase 1/2에서 다진 배치·검증 루프가 안정된 뒤 착수.
  3. Digikey/OLIMEX 레거시 라이브러리 변환·편입 (`kicad-cli sym upgrade`).
- **Phase 4 (선택, 장기)**: 와이어 기반 라우팅 고도화(전역 라우터, switchbox), `docs/deep-research-report.md`가 제시하는 대규모 데이터셋 수집 및 파인튜닝 트랙(별도 프로젝트로 취급).

---

## 검증 방법 (구현 완료 후)

- Phase 1 완료 시점: 생성된 `.kicad_sch`를 Windows KiCad 10.0.5에서 실제로 열어 육안 확인 + `kicad-cli.exe sch erc --format json`이 0 위반으로 통과하는지 확인.
- 이후 각 phase마다 동일한 ERC 게이트를 회귀 테스트로 유지 (예제 프롬프트 셋 → 생성 → ERC 통과 여부를 CI성 스크립트로 자동화).
- LLM 통합 후: Qwen2.5-Coder-7B-Instruct 기준 성공률(구조적으로 유효한 IR 생성 비율, ERC 통과율) 측정 — Qwen3.5-9B는 실험적 트랙으로만 병행 시도.
