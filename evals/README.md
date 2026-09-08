# 고정 합성 평가셋과 평가 준비 도구

**현재 상태: 실제 모델 품질 평가는 미완료.** C 오프라인 Office 실행기, 자동화 20개 사건 실행기, 합성 A/B/C 비교, 별도 승인을 요구하는 실제 모델 비교 실행기를 제공한다. 모의 HTTP와 실제 공급자 증거를 구분한다. validate 성공은 gold 구조 검사이며 모델의 정답률이 아니다. 사람 채택 판정과 실제 평가가 없으므로 모델 품질·토큰 절감률은 미확정이다. 실제 Telegram 전송과 Office 앱 화면 검사는 별도다.

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

현재 adapter는 실제 집계 숫자·단위·원장 locator, TODO kind/상태, 권한 effect, 파일 재열기·원본 불변을 관측한다. gold의 의미상 요구와 제품 requirement ID의 대응은 자동 확정하지 않으므로 facts/coverage 및 관측 TODO의 gold requirement_ids는 비워 두고 실제 연결은 원장에 보존한다. 따라서 이 파일을 scorer에 넣은 coverage·불필요 TODO 지표는 **미주석 관측**으로 해석해야 하며 제품 품질 판정에 사용하면 안 된다. 다음 단계는 원본 결과에 대한 독립 의미 주석 또는 검증 가능한 숫자 요구 대응이다. 캐시는 제품이 완료한 조회와 실제 재사용 횟수를 run별로 기록한다. 조회를 생략한 검토 경로는 조회수에 넣지 않으며, 조회가 없으면 hit rate는 null이다.

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
- 입력 보완이 정답인 사례는 `decision`에서 따로 평가한다. `expected_deferral_tasks`, `correct_deferral_tasks`, `correct_deferral_rate`, `unexpected_deferral_tasks`와 `completed_workloads`를 구분한다. 실제 `awaiting_input`, 검증된 원본·허용 영향, 발행 영수증 0개, 숫자·답변 출력 없음이 확인돼야 올바른 보류다. `published_output_count=null`인 이전 관측은 출력 0개로 추정하지 않는다. 보류를 완료·사람 채택으로 올리지 않으며 기존 품질률은 전체 관측 중 채택된 완료 산출물의 비율이다.
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
5. 실제 calls·steps·receipt·출력 hash와 캐시 계측을 연결했다. 실제 모델 결과의 의미상 facts·coverage·TODO 요구 연결은 독립 주석을 받아야 Observation으로 발행한다.
6. 명시적으로 승인된 모델·횟수·합성 범위에서 live 평가를 실행하고 사람이 blinded packet을 채택한다. 현재 승인 범위를 120건 평가나 A/B/C 전체 실행 허가로 확대하지 않는다.

run.py는 scorer이며 실제 호출은 별도 live.py에서만 명시적으로 실행한다. 기존 관측과의 호환을 위해 고정 PROTOCOL의 최초 실행기 상태 값은 유지한다. 현재 실행기 사용 가능 여부는 이 문서와 실제 CLI를 따른다. P12 전체 완료, 모델 품질, 토큰 절감, 실제 Telegram 전달 또는 Office 앱 열기 성공은 코드와 모의 시험만으로 주장할 수 없다.

### A/B 선택 경계

`engine.complete_model_todo`의 선택, `context.build_model_request`의 요청, `usage.begin_call`의 원장 입장에 같은 `EvaluationRoute` 계약을 전달한다. 평가 run의 actor/project/arm/privacy/catalog snapshot과 TODO·binding digest를 검증한다. 의미상 kind와 추천 역할은 보존하고 실제 호출 역할을 별도로 기록한다. 기본 실행은 기존 kind/role 일치 검사를 유지한다.

관측 adapter는 calls의 route_json에서 실제 역할과 reasoning을 읽고, steps.spec_json에서는 의미상 kind와 requirement 연결을 읽는다. A/B를 실행하기 위해 저장된 kind를 바꾸거나 결과/검증기를 교체하면 안 된다. B의 후속 시도 역시 현재 동일 TODO 논리 호출 상한과 필수 H review 한도를 넘을 수 없으므로, M/H 전체 cascade가 항상 가능하다고 가정하지 않는다.

`compare_fixture(case, directory, scripts=..., catalog=..., privacy=..., cache="cold")`는 A/B/C 각각의 유한한 `ModelResult` 목록을 받는다. `provider="fixture"`, `metering_mode="local"`만 허용하며 fixture 사용량은 `estimated` 또는 `unknown`이어야 한다. 실제 공급자 사용량을 가장한 fixture와 외부 transport callable은 거부한다. 각 arm은 고립된 SQLite/home을 사용하고 입력 파일 바이트는 한 번만 고정한다. warm은 같은 arm의 DB·원본·환경을 재사용하며 cold 원장을 남긴다.

B는 내용 생성 L에서 실제 검증 실패 후 M으로 이동할 수 있다. HTTP 429 재시도는 같은 L 논리 호출이고 승격이 아니다. 필수 H 검토도 TODO의 두 번 한도에 포함되므로 세 번째 생성/검토는 `CALL_LIMIT`으로 중단한다. 고정된 arm을 중간에 바꾸거나 평가 snapshot 없이 역할을 낮추는 호출은 거부한다. ledger는 실제 run/steps/calls/재열기 결과를 기록하되 `offline_fixture`이며, scorer용 품질 `Observation`으로 자동 변환하지 않는다.


## 최초 모델 후보 시험과 자격 자료

`evals.qualification`은 일반 업무 라우터에 미검증 모델을 등록하지 않고 격리된 후보 시험을 실행한다. 전용 설정은 `actor_id: "eval-owner"`, `project_ids: ["eval"]`과 해당 프로젝트의 xAI cloud 허용 정책을 사용한다. 후보와 기준 H 모델은 실제 계정 ID·기능·역할·context/output 한도·reasoning 옵션을 명시하고 `enabled: true`, `allowed_projects: ["eval"]`, `metering_mode: "api"`로 설정한다. 최초 시험에는 quality evidence ID가 필요하지 않다. 실행 키는 일반 앱과 같은 환경변수 또는 보호 저장소에서 읽는다.

~~~powershell
python -m evals.qualification prepare --config eval-config.json --model candidate-model-id --role M --task summarize --case summary-01 --workspace .birkin-lite/evals/candidate-summary
~~~

준비는 키·외부 클라이언트·출력 디렉터리를 만들지 않고 `{manifest, manifest_sha256}`을 stdout에 출력한다. 그 전체 JSON을 BOM 없는 UTF-8 `candidate-manifest.json`으로 저장하고 내용을 검토한다. `--case`는 반복할 수 있으며 선택 사례마다 생성 1회가 상한이다. 모델 목록 조회 1회는 생성과 별개다. **아래 실행에는 그 시험 범위의 별도 승인이 필요하다.**

~~~powershell
python -m evals.qualification execute --manifest candidate-manifest.json --manifest-sha256 REVIEWED_SHA256 --allow-live
~~~

자동 재시도·기존 디렉터리 재실행은 거부한다. 선언한 옵션은 `--reasoning-effort`로 별도 시험한다. H의 `semantic_review`는 `--review-inputs` 파일에 `{case_id: 검토할 내용 출력}`을 지정한다. 후보와 독립 H 기준은 실제 지시문·원문·출력 스키마·검토 대상이 같아야 한다. 코드 버전이 다른 시험을 섞을 수 없다.

각 시험의 `blind.json`은 모델·역할을 숨기고 실제 과제·원래 요청·자료·결과를 보여 준다. 독립 검토자가 기존 HumanLabel 형식으로 채택 여부를 기록한 뒤 발행한다.

~~~powershell
python -m evals.qualification write --candidate .birkin-lite/evals/candidate-summary --reference .birkin-lite/evals/reference-summary --labels human-labels.json --output .birkin-lite/qualification --evidence-id candidate-v1
~~~

위 1건 예시는 자격 발행에 부족하다. 선언 역할마다 서로 다른 원본 fixture 20개 이상, 업무·reasoning 옵션 coverage, 필수 검사, 실제 사용량, H 대비 채택 기준을 충족해야 한다. `--candidate`와 `--reference`는 반복할 수 있고 동일 입력의 독립 H 기준이 하나여야 한다. 모의 HTTP·미상 사용량·누락 또는 재사용한 기준·오래된 라벨·변조된 원장/파일은 거부한다.

발행은 `evaluations/<id>.json`, `<id>.provenance.json`, `<id>.profile.json`을 생성하며 일반 설정을 자동 수정하지 않는다. 6개 내용 업무는 고정 텍스트/CSV corpus에 명시적인 역할 시험 지시문을 적용한다. 이 시험을 원래 120건 Office A/B/C 점수로 사용하지 않는다. XLSX 후보 시험은 지원하지 않으며, XLSX의 실제 업무 비교는 다음 실행기를 사용한다.

## 별도 승인 후 실제 A/B/C 실행

`evals.live`는 `eval` 프로젝트에 cloud 사용이 허용되고, 품질 근거가 검증된 xAI 모델만 받는다. 선택한 모델들이 L/M/H 역할을 모두 제공해야 하며 한 모델이 여러 역할을 맡으면 한 번만 지정한다. 준비 명령은 키를 읽거나 외부 호출을 하지 않는다.

~~~powershell
python -m evals.live prepare --config eval-config.json --case summary-01 --model MODEL_L --model MODEL_M --model MODEL_H --output .birkin-lite/live-evaluation --manifest reviewed.json
~~~

기본 범위는 A/B/C × cold/warm이다. `--arm C --cold-only --max-http-attempts 16`으로 줄일 수 있다. manifest의 사례·고정 원본 바이트·모델·설정·코드·개인정보 정책·호출 상한·출력 위치를 검토한다. **합성 연결 검사 1건의 승인은 이 일괄 평가 승인이 아니다.** 별도 승인 후 준비 명령이 출력한 SHA256을 사용한다.

~~~powershell
python -m evals.live run --config eval-config.json --manifest reviewed.json --manifest-sha256 REVIEWED_SHA256 --allow-live
~~~

원장에는 실제 사용량·비용·캐시 관측·재열기 영수증이 남는다. 같은 디렉터리의 재실행·중단 후 재개는 거부한다. 자료나 코드가 바뀌면 warm 실행도 거부한다. HTTP mock을 주입한 실행은 mock으로 남고 품질 증거로 발행할 수 없다.

독립 검토자는 실제 출력의 SHA256에 맞춘 주석을 작성한다. `coverage`는 확인한 assertion ID에서 source locator 목록으로, `todo_requirements`는 실제 TODO ID에서 assertion ID 목록으로 연결한다. 모든 실제 TODO를 포함하고 gold 답을 자동 복사하지 않는다.

~~~json
{
  "reviewer_id": "independent-reviewer",
  "independent": true,
  "output_sha256": "ACTUAL_PUBLISHED_TEXT_SHA256",
  "facts": [],
  "coverage": {},
  "todo_requirements": {"ACTUAL_TODO_ID": []},
  "numbers": {"응답": "40"},
  "units": {"응답": "명"},
  "numeric_locators": {"응답": ["s1:L1"]}
}
~~~

~~~powershell
python -m evals.live annotate --case summary-01 --ledger .birkin-lite/live-evaluation/summary-01/C/cold-ledger.json --annotation annotation.json
~~~

이 명령은 원장 해시와 실제 결과 파일을 다시 확인하고 `cold-ledger-annotation.json`에 주석과 Observation을 함께 저장한다. 채택 여부를 대신 판단하지 않는다. Observation 배열과 별도의 blinded 사람 라벨을 앞의 `evals.run` scorer에 전달한다.

숫자 주석 세 필드는 모델 내용에 원문 숫자를 인용한 경우에만 함께 지정한다. 위 `40명`은 형식 예시이며 실제 출력과 검증된 인용 원문에 같은 수치·단위가 있어야 한다. 위치가 중복되거나 모호하면 거부한다. 결정적 집계의 숫자를 주석으로 덮어쓰거나 원문에 없는 계산값을 추가할 수 없다.

## 2026-09-08 오류 명칭 정정: corpus/protocol v1.1

`automation-12.scenario.expected.error`를 `PATH_DENIED`에서 `POLICY_DENIED`로 정정했다. 근거는 제품의 [오류 계약](../src/birkin_lite/contracts.py)에 정의된 오류 명칭이다. `PATH_DENIED`는 해당 오류 계약에 없다. 권한 차단 요구, 허용 범위 밖 자료 읽기 0회, 업무 생성 0회는 그대로이며 제품 동작이나 다른 정답 기준은 바꾸지 않았다. 관측 결과에 맞춰 품질 기준을 완화한 변경이 아닌 오류 분류와의 정합성 정정이다.

corpus와 protocol 버전은 `birkin-eval-v1.1`이다. 기존 `p12-automation-v2`의 19/20 일치 관측은 원본 그대로 보존한다. 새 버전은 condition fingerprint가 달라지므로 기존 v1 관측을 재사용하거나 새 실행으로 재분류하지 않는다. 새 버전 평가는 별도 작업 폴더에서 다시 실행한다.

## 2026-09-08 숫자 근거 라벨 정정: corpus/protocol v1.2

현재 버전은 `birkin-eval-v1.2`이다. `table-04·06·08·20`의 6개 숫자 key에서 `gold.numeric_locators`만 직접 기여 행의 최소 집합으로 정정했다. 기준은 **선택 범위·그룹·정책을 적용한 뒤 해당 숫자를 만드는 유효한 값 행**이다. 선택 헤더는 열/그룹의 의미를 확인하는 추가 지원 근거로 허용하며, 기존 scorer의 `gold ⊆ actual ⊆ 등록된 원문 위치` 조건은 바꾸지 않았다. 제외된 행을 숫자 기여 행에 포함시키던 라벨과 실제 기여 근거 계약의 불일치를 고친 것이다.

| 사례 / 숫자 key | 고정 원문과 조건에서 도출한 근거 | 정정된 필수 locator |
|---|---|---|
| table-04 / 영업 | `팀=영업`: L2의 300 + L4의 200 = 500원 | `s1:L2`, `s1:L4` |
| table-04 / 운영 | `팀=운영`: L3의 700 = 700원 | `s1:L3` |
| table-06 / total | `blank_policy=skip`: L2의 3000 + L4의 2000 = 5000원; L3은 빈 금액 | `s1:L2`, `s1:L4` |
| table-08 / total | `mixed_policy=skip`: L2의 600 + L4의 900 = 1500원; L3은 `미정` | `s1:L2`, `s1:L4` |
| table-20 / 2026-09-01 | 날짜 문자열을 그대로 구분: L2의 2 + L4의 4 = 6개 | `s1:L2`, `s1:L4` |
| table-20 / 2026/09/01 | 슬래시 날짜 문자열과 일치하는 L3의 3 = 3개 | `s1:L3` |

숫자·단위·요청·원문/fixture·TaskSpec 입력 조건·required_assertions·허용/금지 사실 및 그 밖의 gold는 그대로다. table-06·08의 제외 행 L3은 삭제할 업무 근거가 아니며, 기존 제외 요구와 집계 원장의 skip 정책·blank/invalid count에 남는다. table-20의 날짜 미통합 요구도 유지한다. table-11·12·14는 actual에 헤더만 추가돼 원래 scorer 조건을 만족하므로 변경하지 않았다. 이 세 사례의 exact-locator 진단 차이를 숫자 scorer 실패로 해석하면 안 된다.

v1.1 원본은 Git `b9d8466`에 보존한다. corpus의 SHA256(LF 기준)은 `dbb1073fffa07647ad4522049fa733614d8a07a1b91db8082ad815698529e6c7` → `cb9e7eab24dc23fca3038fcc6678c384ef78552aebe7fd484401c55186ee741e`이다. 정정 시 Windows CRLF 파일의 SHA256은 `3a0665990a1e65bd8bdc31880420c47a0ff24e3f4c8986789e2ebe22f5b557ae` → `0881392220d700c191301363588973f70f4d863bcf128bed95fee4cde7af36a2`이다. `evals/run.py`는 protocol version 한 줄만 바뀌었으며 SHA256은 `7fc83380d668706699013505154f3054b8af9e3168f183f7730f706b79c8ee6b` → `4ab3f57f117e23cba3e0193ea705008cda24a01729f24ae968daacb9cbfd1c19`이다.

프로토콜 버전 변경으로 모든 condition fingerprint가 달라져 기존 v1.1 관측을 v1.2로 재사용할 수 없다. 옛 원장이나 출력에 새 hash/라벨을 덮어써 새 실행처럼 만들지 않는다. 이번 정정은 고정 fixture에 근거한 라벨 정합성 수정이며, 실제 계산 품질 개선이나 새 20건 통과 결과가 아니다. 여기서는 corpus validator와 순수 scorer 계약만 검증하고, v1.2 업무 실행은 별도 작업 폴더에서 수행한다.

후속 검증에서는 위 4개 사례를 코드가 고정된 새 환경에서 C/cold로 실행했다. 실제 결과 파일을 재열어 숫자·단위·숫자 근거 위치·출처·출력 검사를 4/4 통과했고 모델 호출은 0회였다. 의미상 요구 coverage와 사람의 채택 평가는 채우지 않았으므로 전체 품질 통과나 토큰 절감률의 증거로 사용하지 않는다.
