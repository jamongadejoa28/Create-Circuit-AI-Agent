# 테스트·평가 경계

이 문서는 `pytest가 통과했다`와 `모델이 좋은 회로도를 만들었다`를 혼동하지 않기 위한
계약이다. 테스트 코드가 회로 설계의 정답 데이터를 제품에 주입해서는 안 되며, 이미지가
생성됐다는 사실만으로 사람이 읽기 좋은 도면이라고 판정해서도 안 된다.

## 결론

`pytest`는 결정론적 backend의 불변조건을 검사한다. 대부분의 테스트는 작은 수제
`CircuitIR` 또는 synthetic `SymbolDef`를 사용한다. 이는 특정 회로를 모델에게 가르치는
템플릿이 아니라, 함수 입력을 최소화한 단위 테스트다. 제품 코드는 `tests`를 import하지
않는다.

반면 실제 설계 능력은 llama-server를 호출하는 benchmark에서만 측정한다. benchmark는
실제 `Prompt → RequirementSpec → CircuitIR → KiCad → SVG/PNG`를 실행한다. 그래도 PNG
가독성은 자동 승인하지 않는다. 결과 행은 `visual_review_status=not_reviewed`와 PNG 경로를
남기며, 사람이 보거나 별도로 정확도를 평가한 vision model이 검토해야 한다.

## 허용하는 고정값

다음 네 종류만 허용한다.

1. 전기·자료구조 불변조건: 한 핀은 한 net, 서로 다른 net의 wire 교차 금지, round-trip
   connectivity 동일성처럼 입력 종류와 무관한 규칙.
2. 출처가 있는 장치 사실: 데이터시트 페이지와 package가 확인된 전원 범위, pin function,
   W25Q control pin처럼 선택한 부품 자체에 고유한 사실. 다른 부품으로 일반화하지 않는다.
3. 형식 oracle: KiCad S-expression, netlist, JSON schema, HTTP path confinement처럼 출력
   형식에 정확한 정답이 있는 경우.
4. 정확 전사 oracle: 사용자가 reference와 pin을 명시한 입력. 이 모드는 설계가 아니라
   복사이므로 expected netlist를 정확히 비교할 수 있다.

다음은 금지한다.

- 과거 모델 출력이나 ERC-clean 도면을 설계 정답/검색 지식으로 재사용
- prompt 부분 문자열 `apply_when`으로 고정 회로를 선택
- 특정 campaign artifact 경로가 로컬에 있을 때만 실행되는 테스트
- 종횡비, wire 길이, ERC 개수 같은 임의 임계값을 제품 성공으로 승격
- 저장된 SVG에 `<svg`가 있는지만 보고 visual regression이라고 부르기
- 지식 DB에 존재하지 않는 ID를 retrieval 정답으로 기재

## 남아 있는 pytest 분류

| 범위 | 파일 | 보장하는 것 | 보장하지 않는 것 |
|---|---|---|---|
| 오케스트레이션 | `test_agent.py`, `test_blocks.py`, `test_block_completeness.py`, `test_contracts.py`, `test_truncation.py` | schema 단계, retry, patch gate, block/contract metadata | 실제 모델의 응답 품질. `MockLLM` payload는 모델 정답이 아님 |
| IR·전기 불변조건 | `test_erc.py`, `test_functional_pins.py`, `test_interfaces.py`, `test_topology.py`, `test_compliance.py`, `test_supply_rail_reach.py`, `test_generic_normalize.py` | 연결·전원·peer contract·전도성의 일반 판정 | 완전한 회로 설계 인증 |
| 출처 기반 장치 사실 | `test_normalize_devices.py`, `test_pinfunctions.py`, `test_devicebindings.py`, `test_mcu_circuit.py`, `test_mcu_query.py` | 기록된 부품/package에 한정된 pin/rail 규칙과 반례 | 이름이 비슷한 미기록 부품으로의 일반화 |
| 카탈로그·물리 바인딩 | `test_indexes.py`, `test_symbols.py`, `test_physical_binding.py`, `test_connector_geometry.py`, `test_multiunit.py` | 실제 KiCad symbol/footprint 구조와 검색 provenance | BOM 구매 적합성 전체 |
| backend geometry | `test_geometry.py`, `test_chain_align.py`, `test_router.py`, `test_tree_route.py`, `test_visual.py`, `test_hierarchy.py`, `test_hier_emit.py` | 좌표, 충돌, wire occupancy, 계층/emit 구조 | PNG의 미학·사람 가독성 |
| KiCad 통합 | `test_pipeline.py`, `test_netlist.py`, `test_audit.py` | 수제 IR이 실제 KiCad CLI를 통과하고 결정론적이며 round-trip 동일 | LLM E2E 정확도 |
| 규칙 컴파일러 | `test_rulegraph.py`, `test_patterns.py` | typed rule의 evidence gate, graph lowering/binding/검증 | prompt 키워드 템플릿 선택(제거됨) |
| 측정·표현 | `test_evalmetrics.py`, `test_sequential_campaign.py`, `test_rationale.py`, `test_webapp.py` | 지표 계산, baseline 차이, 사용자에게 실패가 보이는지 | 지표 하나로 제품 합격 판정 |
| 전사 | `test_transcription.py` 및 `test_agent.py`의 transcription 항목 | 명시된 reference/pin/value의 exact 비교 | 자연어 설계 정답 |
| preview/replay 도구 | `test_schematic_preview.py`, `test_replay_model_runs.py` | SVG→PNG 변환과 저장된 IR 발견/파싱 | 이미지 내용 승인 |

`tests/fixtures/circuits.py`의 LED/button 회로는 KiCad 통합 경로를 호출하기 위한 최소
입력이다. byte-for-byte golden schematic은 없으며 제품 지식에도 들어가지 않는다.

## 실제 모델 평가

- `bench_general.py`: 여러 회로군을 실제 모델로 생성한다. 선택 부품, 역할 동작,
  self/KiCad ERC, round-trip, critical stub, route failure, PNG 경로를 서로 분리해 기록한다.
- `bench_transcription.py`: reference/pin이 명시된 입력만 exact oracle로 비교한다.
- `run_sequential_campaign.py`: 같은 고정 입력/seed의 변화량을 항목별로 보여준다. 과거
  baseline이 전기적 진실이라는 뜻은 아니다.
- `eval_knowledge_retrieval.py`: 현재 DB에 실재하는 검토 ID만 scored relevance로 쓴다.
  정답이 아직 없는 질문은 `coverage_probe`로만 기록하며 정확도 분모에 넣지 않는다.
- `replay_model_runs.py`: 저장된 실제 모델 `run.json`의 IR을 현재 deterministic backend로
  다시 그린다. 모델 변동 없이 placement/router/emitter 변경의 PNG를 비교할 때 사용한다.

예시:

```bash
PYTHONPATH=src .venv/bin/python tests/benchmarks/bench_general.py \
  --label review-20260821 --seed 1

PYTHONPATH=src .venv/bin/python tests/benchmarks/replay_model_runs.py \
  tests/artifacts/benchmarks/general/old-run \
  --output tests/artifacts/benchmarks/replay/backend-review
```

두 실행 모두 PNG를 만든다. 리포트의 수치와 `svg_ok`는 이미지 검토를 대체하지 않는다.

## 이번 감사에서 제거한 것

- plan 번호에 묶인 golden circuit Python/회로도와 byte 비교
- 테스트끼리만 사용하던 internal pattern/knowledge fixture
- prompt keyword `apply_when`으로 고정 회로를 삽입하던 legacy pattern 데이터/선택기
- 로컬의 오래된 `run.json` 경로를 직접 읽던 비재현 테스트
- `<svg` 존재만 검사하던 정적 visual fixture와 갱신 도구
- 제품 범위 밖 board A/B benchmark와 실행되지 않던 SchGen/public-dataset 학습 scaffolding
- 현재 DB에 없는 golden/pattern ID를 정답으로 둔 retrieval case
- 출처 검토가 끝나지 않아 제품에서 로드되지 않던 draft rule 파일
- 저장소 내 독립 자료 없이 BLDC/CAN benchmark를 자동 완성하던
  DRV8311/AS5048/AS5045/TJA1051 전용 배선·net-name whitelist
- 참조가 없던 prompt 파일과 사용하지 않는 import/local 변수

## 새 테스트를 추가할 때

테스트 설명에 `왜 이 결과가 정답인지`를 답할 수 있어야 한다. 답은 universal invariant,
구체적 source citation, exact transcription, format oracle 중 하나여야 한다. “이전 실행이
이렇게 나왔다”, “이 값에서 테스트가 통과했다” 또는 “이미지가 생성됐다”는 근거가 아니다.
