# Claude 작업 인수인계 문서 (2026-08-08 ~ 08-09 세션)

> 대상: Codex. 커밋 범위 `951adeb`..`b854e33` (19 커밋, +7,044 / −128줄).
> 테스트 141 → **162개 전체 통과**, 워크트리 클린. 모든 주장에는 커밋/측정 근거가 있다.

---

## 0. 한 줄 요약

사용자 지시(dvk-mx8m-bsb 스타일: 파트별 시트 완결 + 실제 와이어)에서 출발해
**계층 방출 → 안전 라우팅 → 체인 정렬 → 트리 라우터 통합 → CircuitPattern 엔진 →
에이전트 패턴 고속 경로 → 레이어드 배치**까지 코어 엔진을 교체했고,
bench_general을 v1→v8로 8회 돌리며 **결정론적 실패 클래스를 전부 제거**했다.
현재 릴리스 점수 4/8이며, **통과 4건 = 패턴 커버 4건**(led/opamp/regulator/relay).
남은 실패는 전부 비패턴 케이스의 7B 콘텐츠 룰렛이다.

---

## 1. 새로 만든 모듈 / 주요 변경

### 1.1 계층 방출 — `src/circuitgen/hier_emit.py` (951adeb)
- `emit_hierarchical(ir, symbols, partition, out_dir, name, parts_index)` → 루트(시트 박스) + 기능별 자식 `.kicad_sch`.
- 크로스시트 넷 = **global_label** (sheet-pin 페어링 대신; `single_global_label`은 ERC ignore-by-default).
- 레일: 시트별 전원 심볼 인스턴스 + **PWR_FLAG는 레일당 프로젝트 전체 1회** (PWROUT×PWROUT 충돌 방지).
- 자식 instance path = `/<root-uuid>/<sheet-box-uuid>`, 페이지 번호는 실제 방출된 자식만 카운트.
- 에이전트 라우팅: `Agent._generate` — 그룹 ≥2 && 부품 ≥12 → 계층, 아니면 평면.
- 오라클: 3-시트 합성 보드 = 루트 ERC 0 + 전체 계층 넷리스트 라운드트립 일치 (`tests/test_hier_emit.py`).

### 1.2 안전 와이어 라우팅 — `src/circuitgen/emit.py` (951adeb, 64e18c0)
너의 1차 와이어 실험이 ERC 36→64로 악화된 근본 원인 = **와이어가 임의 핀 좌표를 스치면 KiCad는 연결로 판정**.
이를 막는 안전 사슬:
- `_collect_obstacles`: 외부 핀 점 + 회전 인지 몸체 박스(핀 돌출 제외).
- `_seg_clear(a,b,...)`: 세그먼트가 외부 핀 ±0.01 / 외부 몸체 내부를 건드리면 거부.
- `_try_direct_wire` / `_try_l_wire`(2노드) → **`_try_tree_wire`(2~8노드, 너의 router.py 통합)** → 실패 시 스텁+라벨 폴백. **"틀린 와이어는 절대 그리지 않는다"가 불변식.**
- 트리 라우터 통합 시 필수였던 것들(리뷰에서 네 코드의 미비점으로 확인):
  - grid=1.27(핀 좌표 정합); 오프그리드 핀은 라우팅 거부(스냅하면 무음 미연결).
  - 핀별 1셀 **탈출 세그먼트**(핀 방향으로 깨끗하게 이탈).
  - `blocked_points` API를 router.py에 추가: 외부 핀 점 + **잠재 스텁 통로**(폴백 넷의 스텁과 동일선 겹침 방지) + 기라우팅 셀(v1은 직교 교차도 금지).
  - **`_split_at_endpoints`: 분기점에서 세그먼트 분할 + junction 방출.** 네 router의 junction 검출은 압축 세그먼트 중간 부착을 놓친다(분할 없는 분기 = ERC 통과하는 무음 단선). 통합부에서 전면 재계산.
- `EmitPlan.net_routes`(net → direct|l|tree|stubs) + `route_metrics()` — 네가 요구한 **넷 단위 지표** (와이어 객체 수 금지).
- 오라클: 3노드 I2C 버스 = 정션 트리, ERC 0, 라운드트립 일치 (`tests/test_tree_route.py`).

### 1.3 배치 — `src/circuitgen/place.py` (d440197, c833907)
- `align_chains`: 2노드 신호 넷 위의 2핀 수동소자를 IC/커넥터 앵커 핀에서 **핀 마주보기**로 재배치(간격 5.08) → 라우터 발화율 확보. 이동은 1회, 충돌 시 포기(스텁 폴백 유지).
- **강체 클러스터 불변식(중요)**: 체인 정렬된 위성과 앵커는 한 몸이다. 라벨-충돌 제거 루프가 앵커만 +7.62mm 밀어 golden2에서 BOOT0 저항 핀이 STM32 핀 필드 안으로 들어갔다(스택 핀 무음 병합). `align_chains`가 `{ref: cluster_id}`를 반환하고, 제거 루프는 **클러스터 전체를 함께** 이동한다. 이후 배치를 손대는 어떤 패스도 이 계약을 지켜야 한다.
- `layered_tile` + `_flow_columns`: 신호 흐름 레이어드 배치 — 드라이버(OUTPUT류 etype)→싱크 방향 간선으로 최장경로 레이어링(피드백 사이클 캡), 무구동 수동소자는 이웃 평균 레이어(단, 방향 간선이 닿은 수동소자는 제외), barycenter 1스윕. 흐름 판별 불가 그룹은 기존 shelf 유지.

### 1.4 CircuitPattern 엔진 — `src/circuitgen/patterns.py` + `data/patterns/*.json` (4efaa3d)
네 진단("서적 지식이 회로 그래프가 아니다")의 구현체. 패턴 = 역할·핀 capability·핀 대 핀 토폴로지·파라미터/공식·포트·배치 힌트·필수 실선 경로·**검증 인용(필수)**·상태.
- `load_patterns`/`validate_pattern`: 엔드포인트 문법(`ROLE.PIN`|포트) 전수 검사, **무인용 패턴 로드 거부**.
- `match_patterns`: apply_when + **apply_unless** 거부 가드("비반전 증폭"이 "반전 증폭"을 부분문자열로 포함).
- `bind_role_pins`: 정제된 이름 매칭 → 유일 etype 폴백 → **키-번호 폴백**(릴레이 IEC 번호 A1/A2/13/14, 이름 공백). 핀 하나라도 미해석이면 전체 실패.
- `instantiate_pattern`: union-find로 토폴로지→넷, 포트 매핑, 파라미터 값, 패턴 그룹 타일.
- `verify_pattern_instance`: 모든 간선이 IR에서 성립함을 증명.
- **시드 6종** (인용은 전부 기검증 지식 항목에서 복사 — 페이지 날조 금지):
  | id | 인용 |
  |---|---|
  | noninverting_amplifier | Sadiku 5.5, p207 |
  | inverting_amplifier | Sadiku 5.4, p205 |
  | rc_lowpass_filter / rc_highpass_filter | Sadiku 14.7, p661 |
  | ldo_linear_regulator | PEFI 11.1, Fig 11.9, p1135 |
  | relay_driver | PEFI 13.5.3, p1369 |
  | led_switch_indicator | golden1 워크드 디자인 (자체 ERC 검증) |
- 분압기 패턴은 **보류**: 검증된 일반 인용이 지식베이스에 없다. 추출하고 검증한 뒤에 추가할 것.

### 1.5 에이전트 패턴 고속 경로 — `agent.py::_pattern_synthesis` (5ccc189, 76057e8)
- 패턴이 **정확히 하나** 매칭되면: 역할을 카탈로그 심볼에 바인딩(핀수 오름차순 60후보 풀; **가시 non-NC 핀 전부를 패턴이 설명하는 후보만** 채택 — bm25는 5핀 MAX1616을 골라 FB/~SHDN을 방치했다), 인스턴스화, 신호 포트에 Conn_01x02 앵커, 계약 게이트 검증. **LLM 호출은 스펙 추출 1회뿐.**
- `rail_ports`: 전원 패턴의 VIN/VOUT은 스펙 레일 그 자체. **전압 인지 매핑**(3V3/1V8 표기 파싱; highest_supply=레귤레이터 입력/릴레이 코일, lowest_supply=레귤레이터 출력) — 레일 나열 순서 운 제거.
- **모든 실패는 LLM 폴백** — 고속 경로가 런을 중단시키는 일은 없다.
- 패턴 런은 수리 루프의 역할-존재 게이트 면제(후보 없음; 토폴로지+계약 증명이 더 강함).

### 1.6 결정론 가드 신설 (측정 기반, 각각 실패 재현 후 작성)
| 가드 | 근거 측정 | 커밋 |
|---|---|---|
| `merge_dangling_interface_nets` — SPI_MOSI/MOSI 단일핀 쌍 병합 | BLDC run1 | a4e3b84 |
| 수리 게이트(3): 출력류 핀→레일/GND connect 거부 | run2: 수리 LLM이 엔코더 출력들을 GND에 투기, ERC 21→58 | e59a74e |
| `unify_stacked_pins` — 동일 좌표 스택 핀은 한 넷으로 | run4: LTC1562 V- 4/7/14/16/17, 라운드트립에 유령 핀 | b3cfd75 |
| 충돌 매트릭스 connect 게이트 (SKIDL ERROR급 쌍 거부, PWR_FLAG 제외) | run4: 수리 LLM이 MISO OUTPUT 4개를 한 넷에 버스링 | b3cfd75 |
| `compare_connectivity` 계층 로컬 넷 `/SHEET/NET` basename 해석 (넷 분열은 전체 경로로 노출 유지) | run3 | ec194ad |
| documented-NC 핀 **강제 분리** (NC 마크+살아있는 넷 = no_connect_connected) | bench v2 sensor_i2c: Si7050 숨은 핀 3/4 | f0a6787 |
| 헤더/커넥터 역할 빈 검색 → Connector_Generic 폴백 | v2 debug_uart: "UART header" 0건 → 하드 중단 | f0a6787 |
| `_ensure_conceptual_devices` — 미분류 역할에 Conceptual 박스 결정론 주입(합성/수리 게이트 양쪽) | v2 unknown_module: 7B가 2회 모두 커스텀 모듈 탈락 | e1828c9, 7feab49 |
| 직렬 의도 복원 — 2핀 수동소자의 한 핀에 두 넷+반대 핀 공백 → 이동 | v3 passive_led: LED 링크 절단, ERC 1건 차 | 7feab49 |
| 수리 게이트 자가 치유 — 빈 역할 후보를 인덱스에서 재수집(인덱스=원본, ctx=캐시) | v4 debug_uart: 역할 키 desync로 정상 캡이 "미분류" | 901cfca |
| **비례 게이트** — 탈락 수동소자는 복원(전원 힌트 C는 레일에 배선)/면제, 주요 소자만 fail-closed | v6 unknown_module: 벌크 캡 1개로 런 사망 | 69bd678 |
| `_filter_ops` 같은 패치 add→connect 화이트리스트; Device:/power:/Conceptual: 중복 추가 예외; `_limit_main_device_copies` 수량 인지(**드라이버 4개 중 3개 삭제 버그**) | 리뷰 실증 | 0e68745 |

### 1.7 네 세션(contracts/topology) 리뷰에서 수정한 8건 (0e68745)
전부 프로브로 재현 후 수정. 요지:
1. **다중 유닛 op-amp 유닛별 분석** — LM358이 amplifier_total 0 → 앰프 계약 시 무조건 하드 중단이었다. 완전 미배선 여분 유닛은 계약 대상 제외.
2. 피드백 BFS **레일 경유 금지** — 부하 R→GND→바이어스 R이 피드백으로 오판됐다.
3. 바이패스 = **아무 C 브리지** 판정 — 병렬 bleed R이 진짜 캡을 가렸다(첫 간선 승 버그). VI/VO 핀 이름 인식 추가.
4. `repair_contracts`는 **미배선 부품만** 익명 재활용(값 일치 = 지정 부품은 예외) — 연결된 디커플링 캡을 훔치던 무게이트 변이 차단.
5~8. 위 1.6 표의 `_filter_ops`/`_limit_main_device_copies`/flat 경로 attempt-2 재검증(템플릿 이슈가 계약 재검증으로 덮여 사라지던 것).

---

## 2. 측정 기록

### 2.1 BLDC 계층 실행 (같은 프롬프트 4회, Coder-7B)
| run | KiCad ERC | 실선 와이어 | 비고 |
|---|---|---|---|
| 1 (hier만) | 21 | 2 | 첫 계층 성공, 14시트 |
| 2 (+체인 정렬) | 58 | 39 | 수리 LLM이 출력핀들을 GND 투기(→게이트 신설) |
| 3 | 316 | – | MCU 블록 finish_reason=length 사망 + 드라이버 3계열 12개 |
| 4 (+트리 라우터) | 27 | **135** (+정션 7) | 시트별 센스체인 인라인 실선; 스택 핀/버스 충돌 발견(→가드 2건) |
- 결론: **기계는 4회 모두 안정, 편차는 전부 7B 콘텐츠.** 모터 시트는 레퍼런스 스타일로 렌더링됨.

### 2.2 bench_general v1→v8 (릴리스 점수)
1/8 → 4/8 → 3/8 → 3/8 → 3/8 → 4/8 → 4/8 → **4/8 (v8: 통과 4건 = 패턴 4건)**.
- 패턴 케이스는 발화 이후 전승: opamp/regulator 7연속, relay 3연속, led 1/1.
- **하드 중단 클래스는 v7부터 0건** — 전 케이스가 통과 또는 드래프트(잔여 ERC 4~28).
- 잔여 실패 = sensor_i2c(플립), communication_can(ERC ~4), debug_uart(ERC ~5, **68HC12 선택**), unknown_module(68000 선택). 전부 부품 선택/자유 배선 품질 = 7B 한계.
- 데이터: `out/bench_general/pattern-fastpath-v*.jsonl` + 각 `<case>-r1/run.json`.

---

## 3. 지켜야 할 불변식 (위반 시 무음 파손)

1. **틀린 와이어 금지**: 실선은 `_seg_clear` 증명 가능할 때만. 폴백은 항상 스텁+라벨.
2. **분기 = 분할 + 정션**: 세그먼트 중간에 와이어 끝이 닿는 것은 연결이 아니다.
3. **체인 강체 클러스터**: `align_chains` 반환 맵을 무시하고 단일 심볼만 옮기지 말 것.
4. **스택 핀 = 한 노드**: 같은 (unit,x,y) 핀 집합은 항상 같은 넷.
5. **패턴 인용 필수**: source.book/section 없는 패턴은 로드조차 거부된다. **페이지를 지어내지 말 것** — 기검증 지식 항목에서 복사하거나 PyMuPDF로 재검증.
6. **패턴 바인딩 전체 커버리지**: 가시 non-NC 핀을 전부 설명 못 하는 후보는 거부(잔여 핀 dangle 방지). MCU 패턴을 만들려면 이 규칙에 `allow_unbound` 같은 명시적 예외 설계가 선행되어야 한다.
7. **게이트 비례성**: fail-closed는 주요 소자(MCU/드라이버/모듈)만. 수동소자는 복원/면제.
8. **인덱스가 원본, ctx는 캐시**: 게이트 판정 전 빈 역할은 재수집.
9. 넷리스트 라운드트립이 최종 오라클 — ERC만으로는 무음 크로스넷을 못 잡는다(핀 변환 교훈).

---

## 4. 다음 작업 제안 (우선순위)

1. **패턴 확대**: sensor_i2c(I2C 센서+풀업+디커플링), communication_can(TJA1051 고정 핀 기능 — sanitize의 화이트리스트 재사용 가능). 추가 후보: 555, H-브리지, 분압기(인용 추출 선행).
2. **MCU 최소회로 패턴**: `allow_unbound_pins` 역할 옵션 설계 필요(허브 부품 부분 바인딩; 잔여 핀은 wire_mcu_interfaces/NC로). golden2/5와 debug_uart를 동시에 잡는다.
3. **MCU 선택 가이드**: generic "microcontroller" 검색이 68HC12/68000을 고른다. selection_guidance 지식 항목 또는 현대 계열 캐퍼빌리티 필터(SWD 존재 등).
4. 라우터 고도화: 직교 교차 허용(현재 v1은 전면 금지 — 밀집 시트에서 라우팅율 제한), rip-up/reroute, 다중 소스 BFS 휴리스틱(성능).
5. 골든 5종 라이브 재측정(fail-closed + 패턴 경로 반영 후 아직 안 돌림).
6. 시각 QA(렌더 이미지 판독) — 라벨 텍스트 겹침이 남아 있다(전기적으론 무해).

## 5. 참고 위치
- 패턴: `data/patterns/*.json`, 엔진 `src/circuitgen/patterns.py`, 테스트 `tests/test_patterns.py`
- 계층: `hier_emit.py`+`tests/test_hier_emit.py` / 라우팅: `emit.py`(+`router.py`)+`tests/test_tree_route.py`
- 배치: `place.py`+`tests/test_chain_align.py` / 가드: `normalize.py`·`agent.py`+`tests/test_normalize_devices.py`·`tests/test_agent.py`
- 벤치: `scripts/bench_general.py`, 결과 `out/bench_general/`; BLDC 산출물 `out/agent/bldc_hier*`
