# 고정 합성 평가셋과 평가 준비 도구

**현재 상태: P12 일부 구현.** 100개 Office 사례를 선택 실행하는 C 오프라인 실행기, 20개 자동화 사건 실행기, 합성 응답으로 A/B/C 실행 경계를 비교하는 도구를 연결했다. 자동화 사건은 v1.1에서 20/20 일치했고 Office는 대표 사례만 실행했다. 전체 corpus의 실제 모델 평가와 사람 채택 판정은 미완료다. validate 성공은 gold 구조 검사이며 모델의 정답률이 아니다. 모델 품질과 토큰 절감률은 null이다. 네트워크·실제 Telegram 전송·Office 앱 실행은 이 평가 도구에 포함되지 않는다.

새 의존성은 없다. 기존 Pydantic 엄격 모델을 재사용하고 JSON·Decimal·통계 집계·CLI는 표준 라이브러리로 구현했다. 평가 파일은 배포 wheel의 실행 기능이 아니다.

## 고정 자료

cases.json은 다음 6범주 각 20건, 총 120건이다. 숫자나 이름만 바꾼 반복 생성기를 사용하지 않는다.

| 범주 | 주요 변형 |
|---|---|
| table | 환불 음수, Decimal 소수, 그룹/복수 열, 빈칸/혼합값, 혼합 통화, CSV 따옴표, 범위/시트, 숨김 행, 미검증 수식, 0분모, 비율, 반올림 방식, 날짜 원문 |
| extraction | 명시/미정 담당자와 기한, 발언자와 담당자 구별, 제안/결정, 취소/완료, 상대 날짜, 선행 조건, 공동 담당자, 개인정보 추측 금지 |
| summary | 환불 예외, 휴무, 미확정 시험, 의무/권고, 단위, 시행일, 표본 범위, 미서명 계약, 장애 영향, 미지원 기능, 자료 속 악성 지시문 |
| multi_document | 2~3문서의 실적·재고·채용·교육·지원 정책·예산·QA·예약 상태, 수치 합계와 서술 구분 |
| conflict_missing | 수치/날짜 충돌, 원인 가설, 결합 조건, 자료·환율 부족, 표본 차이, 다른 프로젝트 권한, 승인 없는 전송, 완료 단계 유지 |
| automation | once 중복, 중지, 놓친 일정, DST gap/fold, UTC 간격, 요일, 파일 기준선/안정화/동일 stat 변조, junction, 최신 사건, 일일 한도, stale 승인, 전송 unknown, 토큰 epoch, poison update, 권한 회수 |

각 사례에는 한국어 요청, 출력 형식, 원문 텍스트, s1:L1 같은 **평가용 locator와 정확한 인용**, 입력 조건, 도구/검증기 범위, gold가 있다. 숫자는 Decimal 문자열이며 단위와 근거 locator가 필수다. required_assertions는 요구 coverage, allowed_facts/forbidden_facts는 허용·금지 사실, expected_kind는 기대 난도, expected_missing_input은 부족자료 판정이다.

sources[].text가 TXT/CSV의 고정 UTF-8 내용이다. XLSX 사례의 fixture.sheets는 시트별 셀 배열이고 hidden_rows는 1부터 시작하는 숨김 행이다. 수식 사례는 캐시 미검증 상태를 유지해야 한다. 자료 미첨부 사례에는 sources=[]가 있다. scenario에는 자동화 초기 상태, 순서가 있는 사건, 정확한 기대 상태가 담겨 있다.

inputs는 확인해야 하는 업무 조건이며 모든 항목을 그대로 TaskSpec.inputs에 복사하는 계약이 아니다. 미래 실행기는 case별 recipe/typed args에 맞춰 이 조건을 연결해야 한다. 예를 들어 다문서 보고서는 필요한 동적 계획을 허용해야 하고, intent를 도구 인수로 넘기면 안 된다. 평가 locator도 실제 Evidence.locator로 변환한 뒤 실제 바이트로 재검증해야 한다.

## 실행

저장소 루트에서 설치된 환경을 사용한다.

~~~powershell
.venv/Scripts/python.exe evals/run.py validate
.venv/Scripts/python.exe evals/run.py manifest --output .birkin-lite/evidence/p12-manifest.json
.venv/Scripts/python.exe evals/run.py smoke
.venv/Scripts/python.exe evals/run.py score
.venv/Scripts/python.exe -m pytest tests/test_evals.py -q
~~~

manifest는 120×3경로×cold/warm = 720개 **예정 조합**을 출력한다. 계획을 실행하거나 성공 수로 계산하지 않는다. 관측 파일이 없는 score는 빈 그룹과 미측정 비교를 출력한다.

실제 오프라인 실행기는 다음과 같이 선택한 고정 사례만 실행한다. workspace는 앱의 실제 DB/home과 분리된 새 평가 디렉터리여야 한다. 같은 cold 사례를 재실행하면 덮어쓰거나 중복 실행하지 않고 거부한다.

~~~powershell
.venv/Scripts/python.exe -m evals.execute --case table-01 --case summary-01 --case conflict_missing-05 --workspace .birkin-lite/evidence/my-offline-eval
.venv/Scripts/python.exe -m evals.execute --case table-01 --cache warm --workspace .birkin-lite/evidence/my-offline-eval
~~~

전체 Office 100개를 순차 실행하려면 --case all-office를 사용한다. 자동화 20개와 --arm A/B는 파일 생성 전에 명시적으로 거부한다. 모델·키·외부 클라이언트를 구성하지 않으며 모델 목록은 빈 값, 정책은 local_only이다. 복잡한 원문이 계획 모델을 요구하면 실제 NO_CAPABLE_MODEL 상태와 빈 출력을 남긴다. 요청을 단순화하거나 gold 정답을 생성 입력에 넣지 않는다.

각 사례 디렉터리에는 원본 파일, 관리 사본, state.db, cold/warm-ledger.json, cold/warm-observation.json이 남는다. 실제 prepare_run/execute_next·도구·검증기를 사용하고, 결과는 발행된 artifact와 일치하는 Receipt가 있을 때만 실제 파일을 재열어 읽는다. 원본과 관리 사본의 SHA-256도 다시 확인한다. warm은 동일 source ID·원본 바이트·코드·환경을 재사용한다. 원장을 보존한 채 새 run을 만든다.

현재 adapter는 실제 집계 숫자·단위·원장 locator, TODO kind/상태, 권한 effect, 파일 재열기·원본 불변을 관측한다. gold의 의미상 요구와 제품 requirement ID의 대응은 자동 확정하지 않으므로 facts/coverage 및 관측 TODO의 gold requirement_ids는 비워 두고 실제 연결은 원장에 보존한다. 따라서 이 파일을 scorer에 넣은 coverage·불필요 TODO 지표는 **미주석 관측**으로 해석해야 하며 제품 품질 판정에 사용하면 안 된다. 다음 단계는 원본 결과에 대한 독립 의미 주석 또는 검증 가능한 숫자 요구 대응이다. 캐시 lookup 계측도 미연결이므로 0건 관측을 기록하고 hit rate는 null이다.

회귀 검사는 원문 table-01의 실제 CSV→D→XLSX 재열기, summary-01/conflict_missing-05의 실제 모델 부족 중단, 별도로 명명한 짧은 자료 미첨부 fixture의 awaiting_input을 구분한다. 짧은 fixture를 고정 사례의 성공으로 바꿔 보고하지 않는다. XLSX를 A/B까지 확장할 때는 각 경로에서 ZIP 바이트를 재생성하지 않고 한 번 고정한 원본 바이트를 재사용해야 한다.

~~~powershell
.venv/Scripts/python.exe evals/run.py blind --observations observations.json --output blind.json
.venv/Scripts/python.exe evals/run.py score --observations observations.json --labels human-labels.json --output report.json
~~~

입력은 JSON 배열이다. 정확한 구조는 run.py의 Observation.model_json_schema()와 HumanLabel.model_json_schema()로 조회할 수 있다. 모르는 필드, 중복 사례/arm/cache, 중복 관측 ID, 변조된 output hash, case fingerprint 불일치, 숫자 단위·locator 누락, 잘못된 토큰 부분집합을 거부한다.

## 동일 조건 비교 계약

- A는 내용 생성을 H부터 시작한다. 계산·해시·파일 작업 D는 동일한 도구를 사용한다.
- B는 내용 생성을 L부터 시작하고 **동일 검증 실패**가 있을 때 M/H로 올라간다. 통과해도 무조건 다음 모델을 호출하는 비교는 금지한다.
- C는 TODO 작성 시 난도에 따라 D/L/M/H를 정한다. 현재 제품의 보완·정정 규칙을 유지한다.
- 세 경로의 원문, 요구 coverage, 출력 형식, 개인정보·외부 영향, 도구/검증기, context·출력 한도, 모델 카탈로그·적격 근거, 계획 조건과 호출 상한은 동일해야 한다.
- 내용 난도 라벨은 A에서 H를 쓴다는 이유로 complex로 바꾸면 안 된다. 계획·검토·보완 역할은 실제 route와 각 call에 기록한다.
- condition_hash(case)는 고정 case 전체와 protocol을 묶는다. environment_hash는 실행기가 실제 코드/도구/검증기 버전, parser/template/prompt, 모델 ID·quality evidence·reasoning 설정, 권한 정책과 한도를 canonical JSON으로 묶어 만든다. 같은 case의 A/B/C·cold/warm에 다른 environment hash가 있으면 비교를 거부한다.
- cold는 별도 DB/home의 빈 로컬 캐시로 시작한다. warm은 같은 case/arm을 그대로 재실행한다. 공급자 prompt cache는 강제로 비웠다고 가정하지 않고 실제 cached usage로 보고한다. warm 준비를 위해 이미 수행한 cold 업무의 사용량은 cold 원장에 남긴다. warm 절감으로 준비 비용이 사라진다고 주장하지 않는다.

## 관측과 사람 채택 라벨

Observation은 실행기가 실제 run/steps/calls/receipt를 읽어 작성할 교환 형식이다. evidence_ref로 원본 실행 근거를 보존한다. 각 transport 시도는 call 하나이고 실패/보완/재시도도 삭제하지 않는다. usage는 제품의 실제 Usage 계약을 사용한다. released는 전송 전 해제된 호출만 해당한다.

numbers, units, numeric_locators, facts, coverage는 출력에 대한 구조화된 관측이다. 이 집계기는 자유로운 한국어 문장에서 의미를 자동 추론하는 judge가 아니다. 허용 사실 집합·금지 문구·수치·locator 범위·요구 연결을 확인하고, 실제 내용의 근거 대응은 실행기의 검증 결과와 사람이 확인해야 한다. verified 필드를 단순히 true로 채운 파일은 실행 증거가 아니다. 자동화는 scenario_result가 고정된 scenario.expected와 일치해야 한다.

blind는 모델 ID, arm, cache, 호출 내역, 관측 ID를 제외하고 요청·원문·요구·TODO 제목·출력을 섞인 순서의 opaque ID로 제공한다. 사람은 모델 설정과 원장을 보지 않은 상태에서 아래 라벨을 작성한다.

~~~json
{
  "blind_id": "blind 명령에서 받은 b_... 값",
  "output_sha256": "같은 packet의 64자리 해시",
  "reviewer_id": "reviewer-01",
  "blinded": true,
  "adopted": true,
  "coverage_correct": true,
  "facts_grounded": true,
  "usable": true,
  "todo_decomposition_appropriate": true,
  "rationale": "필수 요구와 근거가 맞고 후속 업무에 사용할 수 있다."
}
~~~

출력 해시가 다르거나 같은 평가자가 중복 제출하면 거부한다. 채택은 rubric 전체 true가 필요하다. 모델 내용 업무는 사람 라벨 없으면 품질률·채택당 비용·채택당 토큰을 null로 둔다. 복수 평가자가 있으면 모두 채택해야 채택으로 집계한다. 결정적 도구·자동화 사례는 실행 검증으로 채택할 수 있지만, 사람이 거절한 라벨이 있으면 채택하지 않는다.

execution=live 문자열이나 evidence_ref가 실측의 진위를 인증해 주지는 않는다. **이 importer는 원본 DB·영수증을 재실행하거나 외부 서비스로 증명하지 않는다.** 보고서의 evidence_layer=imported_observations_not_reexecuted를 유지한다. 테스트의 합성 live 플래그는 집계 코드 검사용이고 실제 모델 평가 자료로 반입하면 안 된다.

execution=offline_fixture는 합성 관측 자체를 검사한 경우다. 향후 실제 파일·DB·도구를 실행하고 모델 생성은 사용하지 않은 경우는 offline_workload로 구분한다. provider 미설정으로 중단한 업무는 workload_complete=false와 실제 중단 outcome을 기록하고, 빈 output/coverage를 gold로 채우지 않는다. 두 offline 범주 모두 실제 모델 품질·절감률을 null로 유지한다.

## 집계 정의

- 분모는 해당 arm/cache/범주 또는 cohort의 전체 관측 업무다. 실패·미채택 업무와 실패 호출의 입력+출력 토큰 및 과금도 분자에 남긴다.
- 총 토큰은 input_tokens_total + output_tokens_total이다. cached input과 reasoning은 각각 입력/출력의 부분집합이므로 한 번 더 더하지 않는다. cached input과 reasoning은 별도 열로 보인다. 공급자별 캐시 요율은 실제 금액 원장에 반영되어야 한다.
- 일부 사용량이나 가격이 미상이면 합계·채택당 수치를 null로 유지한다. known_*_lower_bound는 확인된 부분만의 하한이며 0원 견적이 아니다. stale 가격도 정확한 비용 비교에서 제외한다.
- planning_tokens는 plan/replan 전체 호출, repair_rate는 repair 호출이 있는 업무 비율, escalation_rate는 실제 상향 호출이 있는 업무 비율이다.
- p50/p95는 채택된 업무의 첫 시도부터 채택까지 전체 latency를 정렬한 후 (n−1)q 선형 보간한다. 실패한 업무는 채택 latency에 넣지 않지만 품질 분모와 사용량 분자에 남긴다.
- 채택률·요구 coverage·분류 정확도에 95% Wilson 구간을 함께 표기한다. 구현은 z=1.959963984540054인 이항 비율 구간이다. [NIST의 비율 신뢰구간 설명](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)을 따랐다. 요구/TODO는 같은 업무 안에서 상관될 수 있으므로 coverage·분류 구간은 기술 통계이며 독립 표본의 출시 검정으로 사용하지 않는다.
- D/L/M/H 분류 과소·과대, 미실행 optional의 요구 대체 여부, 실행한 불필요 TODO, 완료 단계 재실행, 보완 범위, 도구 비율, 캐시 hit를 보고한다. 도구 보조 TODO는 purpose=support로 기록하여 내용 TODO의 기대 난도와 혼동하지 않는다.
- A/B/C 비교는 같은 사례 집합과 같은 환경으로 측정한 live 기록, 필요한 사람 라벨, provider 사용량이 모두 있을 때만 기술적 절감률을 계산한다. unknown·estimated 사용량 또는 누락 라벨이 있으면 절감률은 null이다.
- release_target_proven은 계속 null이다. 이 도구는 작은 표본의 절감률이나 Wilson 구간만으로 “H 대비 2%p 비열등·25% 절감” 출시 목표 달성을 자동 선언하지 않는다. 대응 표본의 불확실성, 실제 비용 개선, 전체 필수 조건과 별도 live gate를 검토해야 한다.

## 현재 연결과 후속 구현

1. 구현됨: Office source fixture를 실제 파일로 만들고 artifacts.import_source를 거쳐 관리 사본과 원본 해시를 확보한다.
2. 구현됨: C 오프라인 planning.prepare_run → engine.execute_next와 현재 tools.registry/verification.registry를 연결한다. 모델이 필요한 업무는 중단하고 실제 상태를 남긴다. 전체 100개 일괄 검증은 아직 수행하지 않았고 대표 사례를 검증했다.
3. `comparison.py`는 같은 파일·DB·도구·검증기를 A/B/C로 실행하는 오프라인 비교 경계를 제공한다. 외부 모델 대신 명시적인 합성 응답만 받으며, 품질·공급자 토큰·절감률은 null이다. 제품의 기본 경로는 그대로 C이고 평가용 입력은 HTTP/config에 노출하지 않는다. 전체 corpus 실측 연결은 별도다.
4. `automation.py`는 20개 봇 사건을 실제 DB·주입 시계·허용된 파일·명시적인 HTTP mock에 연결한다. `python -m evals.automation --case all --workspace <새 폴더>`로 실행한다. C/cold 상태 평가이며 Office 내용 생성·실제 Telegram 전달을 확인하는 실행은 아니다.
5. 부분 구현: 실제 calls·steps·receipt·출력 hash에서 Observation을 만든다. 원장과 원본 불변, 실제 결과 재열기는 연결했고, gold 의미 요구와의 독립 대응·분류 주석 및 캐시 계측은 남아 있다.
6. 명시적으로 승인된 모델·횟수·합성 범위에서 live 평가를 실행하고 사람이 blinded packet을 채택한다. 현재 승인 범위를 120건 평가나 A/B/C 전체 실행 허가로 확대하지 않는다.

현재 run.py는 네트워크 API 클라이언트나 live 실행 명령을 제공하지 않는다. P12 전체 완료, 모델 품질, 토큰 절감, 실제 Telegram 전달 또는 Office 앱 열기 성공은 이 파일들만으로 주장할 수 없다.

### A/B 선택 경계

`engine.complete_model_todo`의 선택, `context.build_model_request`의 요청, `usage.begin_call`의 원장 입장에 같은 `EvaluationRoute` 계약을 전달한다. 평가 run의 actor/project/arm/privacy/catalog snapshot과 TODO·binding digest를 검증한다. 의미상 kind와 추천 역할은 보존하고 실제 호출 역할을 별도로 기록한다. 기본 실행은 기존 kind/role 일치 검사를 유지한다.

관측 adapter는 calls의 route_json에서 실제 역할과 reasoning을 읽고, steps.spec_json에서는 의미상 kind와 requirement 연결을 읽는다. A/B를 실행하기 위해 저장된 kind를 바꾸거나 결과/검증기를 교체하면 안 된다. B의 후속 시도 역시 현재 동일 TODO 논리 호출 상한과 필수 H review 한도를 넘을 수 없으므로, M/H 전체 cascade가 항상 가능하다고 가정하지 않는다.

`compare_fixture(case, directory, scripts=..., catalog=..., privacy=..., cache="cold")`는 A/B/C 각각의 유한한 `ModelResult` 목록을 받는다. `provider="fixture"`, `metering_mode="local"`만 허용하며 fixture 사용량은 `estimated` 또는 `unknown`이어야 한다. 실제 공급자 사용량을 가장한 fixture와 외부 transport callable은 거부한다. 별도 live CLI는 없다. 각 arm은 고립된 SQLite/home을 사용하고 입력 파일 바이트는 한 번만 고정한다. warm은 같은 arm의 DB·원본·환경을 재사용하며 cold 원장을 남긴다.

B는 내용 생성 L에서 실제 검증 실패 후 M으로 이동할 수 있다. HTTP 429 재시도는 같은 L 논리 호출이고 승격이 아니다. 필수 H 검토도 TODO의 두 번 한도에 포함되므로 세 번째 생성/검토는 `CALL_LIMIT`으로 중단한다. 고정된 arm을 중간에 바꾸거나 평가 snapshot 없이 역할을 낮추는 호출은 거부한다. ledger는 실제 run/steps/calls/재열기 결과를 기록하되 `offline_fixture`이며, scorer용 품질 `Observation`으로 자동 변환하지 않는다.


## 2026-09-08 오류 명칭 정정: corpus/protocol v1.1

`automation-12.scenario.expected.error`를 `PATH_DENIED`에서 `POLICY_DENIED`로 정정했다. 근거는 제품의 [오류 계약](../src/birkin_lite/contracts.py)에 정의된 오류 명칭이다. `PATH_DENIED`는 해당 오류 계약에 없다. 권한 차단 요구, 허용 범위 밖 자료 읽기 0회, 업무 생성 0회는 그대로이며 제품 동작이나 다른 정답 기준은 바꾸지 않았다. 관측 결과에 맞춰 품질 기준을 완화한 변경이 아닌 오류 분류와의 정합성 정정이다.

corpus와 protocol 버전은 `birkin-eval-v1.1`이다. 기존 `p12-automation-v2`의 19/20 일치 관측은 원본 그대로 보존한다. 새 버전은 condition fingerprint가 달라지므로 기존 v1 관측을 재사용하거나 새 실행으로 재분류하지 않는다. 새 버전 평가는 별도 작업 폴더에서 다시 실행한다.
