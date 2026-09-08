# 로컬 HTTP 연결 계약 (P11)

현재 Python 실행기를 HTTP와 작은 화면에 연결한다. 별도 프런트엔드 라이브러리를 사용하지 않는다.

## 공통

- 서버는 `127.0.0.1`, 지정 port 한 개, HTTP worker 한 개다. 모든 DB 연결은 사용하는 스레드 안에서 생성·종료한다.
- API origin은 정확한 `http://127.0.0.1:<port>`다. Host/Origin/Fetch-Metadata를 검사하며 CORS를 허용하지 않는다.
- `POST /api/bootstrap`의 JSON `{code}`로 일회용 로컬 코드를 소비한다. 성공은 HttpOnly/SameSite=Strict cookie와 `{csrf_token}`. 이후 변경 요청은 `X-CSRF-Token` 필수. 코드는 URL에 넣지 않는다.
- `GET /api/session`: `{csrf_token, actor:{id,project_ids}}`. 세션이 없으면 401. `GET /api/health`는 인증 없이 `{ok:true}`만 반환한다.
- JSON 본문은 64 KiB, 파일은 25 MiB. unknown field/형식 오류는 400, 권한 없음은 404, stale revision은 409, 만료 승인은 410.
- 오류: `{error:{code,message_ko,recoverable,run_id}}`. 원시 예외·키·파일 절대경로를 오류로 반환하지 않는다.
- 화면은 API 객체를 textContent와 DOM node로 렌더링한다. 모델·자료 HTML을 innerHTML로 넣지 않는다.

## 업무와 파일

| 요청 | body 또는 query | 응답 |
|---|---|---|
| POST /api/artifacts | raw binary, query `project_id`, `filename`; Content-Type은 파일 MIME | 201 `{artifact}` |
| GET /api/artifacts | `project_id` | `{artifacts}`; 선택 가능한 source 목록 |
| GET /api/artifacts/{id}/download | 없음 | 실제 권한/hash 재확인한 bytes, attachment |
| POST /api/artifacts/{id}/pin | `{pinned:true|false}` | 결과의 보관 고정 상태 |
| POST /api/runs | RunRequest; `Idempotency-Key` header 필수 | 202 `{run}` |
| GET /api/runs | `project_id` | `{runs}` 최근 100개 |
| GET /api/runs/{id} | 없음 | `{run,steps,task_spec,artifacts,approvals,deliveries,usage,calls,automation_observation}` |
| POST /api/runs/{id}/input | `{expected_revision,task_spec}` | 202 `{run}` |
| POST /api/runs/{id}/cancel | `{}` | 202 `{run}` |

RunRequest는 `{request,project_id,artifact_ids,quality_mode,output_format}`. 품질은 balanced/high, 형식은 text/md/csv/xlsx/docx. high는 생성 난도를 바꾸지 않고 자연어 결과의 정밀 의미 검토를 요청한다. 계산에는 모델 검토를 추가하지 않는다.

TaskSpec은 `{intent,source_ids,inputs,acceptance,required_capabilities,ambiguity}`. 화면은 기존 task_spec을 기본값으로 사용하고 사용자가 선택한 자료·시트·범위·값 열·그룹 열·완료 조건을 보완한다. `inputs`는 이 실제 계약에서 허용하는 이름을 사용하며 누락 값은 임의 추측하지 않는다. `supply_inputs`의 revision 변경과 재준비 marker는 같은 DB transaction에 저장한다.

숫자 집계의 `inputs.policy.thousands_separator`는 기본 null이며 쉼표 숫자의 뜻을 추측하지 않는다. 명시한 `","`는 정확한 세 자리 천 단위 그룹만 허용한다. CSV의 명시적 천 단위 요청에서만 같은 정책을 도출하며 명시한 null은 덮어쓰지 않는다. 필수 숫자가 비었거나 숫자로 해석되지 않고 해당 정책이 error이면 결과를 발행하지 않고 입력 보완을 기다린다.

run/steps는 기존 DB의 JSON 필드를 해석한 객체다. step.spec_json.todo에서 난도·배정, original/final classification과 correction_reason에서 변경을 읽는다. run.result_json.outputs의 artifact/receipt와 artifacts 목록으로 결과를 보여준다. 형식·수치·출처·의미·시각 증거를 섞지 않는다. 원본/자료·결과가 없다는 상태도 정확히 보여준다.

## 자동화와 승인

`calls`는 id/step_id/provider/model_id/purpose/state/role/reasoning_effort, 토큰 관측값, `logical_call_no`, `transport_attempt_no`, `actual_micro_usd`, `price_stale`을 담는다. prompt·비공개 route·키·원시 응답은 보내지 않는다. step_id=null은 계획 호출이다. UI는 실제 호출 모델을 TODO 아래에 표시하며 호출 전에는 모델명을 추정하지 않는다. 같은 논리 호출의 첫 HTTP 시도와 재시도 비용을 구분한다. 관측되지 않은 비용은 null이며 전송 전 released 시도는 합계에서 제외한다.

`automation_observation`은 해당 소유자·프로젝트·봇·사건과 일치하는 기록만 반환하며, 없으면 null이다. 예약/수신/시작/직전 tick/이번 tick/기록 시각과 `clock_rollback`, `scheduled_daemon_state`, `pc_power_state`를 제공한다. 예약 시각과 실제 표본 시각이 일치할 때만 daemon을 observed로 표시하며, 관측 공백과 시계 역행은 unknown이다. PC 전원은 항상 unknown이다. 세션 ID는 노출하지 않는다.

| 요청 | body 또는 query | 응답 |
|---|---|---|
| GET /api/automations | `project_id` | `{automations}`; 각 항목 proposal_hash 포함 |
| POST /api/automations | AutomationConfig | 201 `{automation,proposal_hash}`; disabled |
| PATCH /api/automations/{id} | `{expected_revision,config}` 또는 `{expected_revision,enabled,confirmed_hash}` | `{automation,proposal_hash}` |
| POST /api/folders | `{project_id,path,explicit_confirmed:true}` | 201 `{folder}`; 경로 없는 ID |
| GET /api/folders | `project_id` | `{folders}`; id/name만 |
| GET /api/approvals | 없음 | `{approvals}` |
| POST /api/approvals/{id}/resolve | `{decision,proposal_hash,expected_revision}` | `{id,state}` |
| POST /api/deliveries/{id}/cancel | `{}` | `{delivery}` |
| POST /api/telegram/pairing | `{}` | `{code,expires_at}` |

AutomationConfig는 automation.py의 strict 계약이다. `name,project_id,recipe,instruction,trigger,source_scope,quality_mode,review_mode,delivery,authority,limits,output_format`. 화면은 once/daily/weekly/interval/file_changed별 입력을 보여주고 기본 timezone=Asia/Seoul, missed_policy=latest_once, 파일 초기실행=false, delivery=local, max_runs_per_day=10을 사용한다. 활성화 전 자료/업무/시간/형식/대상/횟수/권한을 요약한다. 수정은 비활성 새 revision이 된다. Telegram 전달 봇은 현재 pairing revision까지 정확히 승인해야 한다.

## 설정·기억·사용량

- GET `/api/settings`: `{config,revision,model_status,key_present,telegram_key_present,telegram_connection,capabilities}`. config는 일반 설정이며 키를 포함하지 않는다. 현재 모델 가용성/품질 자격 미확인은 그대로 표시한다.
- PATCH `/api/settings`: `{expected_revision,models?,privacy?,telegram?}`. server home/actor/project 범위를 이 API로 변경하지 않는다. 모델 profile·privacy·TelegramConfig는 config.py의 strict 계약이며 설정 revision으로 낙관적 잠금을 적용한다.
- POST `/api/secrets`: `{provider:"xai"|"telegram",value}`. 사용자에게 확인받은 키만 보호된 로컬 저장소에 기록하고 값은 반환하지 않는다. 빈 값으로 명시 삭제할 수 있다. 설정·로그·모델 문맥에는 키를 기록하지 않는다.
- POST `/api/settings/check-models`: `{}`. 계정 모델 목록만 조회한다. 생성/유료 품질 평가를 자동 실행하지 않는다.
- GET `/api/usage?project_id=...`: `{today,month,runs}`. provider actual/estimated/unknown과 캐시/재시도 내역을 구분한다. 가격/잔액은 TODO 모델 선택에 전달하지 않는다.
- GET `/api/memories?project_id=...&query=...`: `{memories}`.
- POST `/api/memories`: `{project_id,args:MemorySaveArgs}`; 명시 저장 버튼만 호출한다.
- PATCH `/api/memories/{id}`: `{project_id,expected_revision,args:MemorySaveArgs}`.
- DELETE `/api/memories/{id}`: `{project_id,expected_revision,purge_original:false|true}`. 기본값은 검색에서만 제외한다. true는 명시적으로 요청한 기억의 모든 저장 버전 파일을 제거하며, 참고한 업무 원본은 유지한다. 진행 중인 참조가 있으면 `purge_pending:true`이며 파일 제거 성공으로 표시하지 않는다.

## 화면과 실행 수명주기

대화/자동화/기록과 설정 dialog. 대화는 프로젝트·파일 선택, 요청, 품질, 형식, 실행으로 시작한다. 기록과 선택 run은 1초 polling으로 갱신하며 버튼 작업 중 중복 요청을 막는다. run 생성 idempotency key는 재시도에도 유지한다. 명시 label·focus·aria-live·색상 외 상태 문구와 360px 화면 재배치를 구현했다. 실제 브라우저 200% 확대와 스크린리더 검증은 남아 있다.

runtime은 시작 시 OS lock 획득 후 복구한다. 업무 실행 동시성 1, scheduler 15초, Telegram long polling과 outbox는 독립 loop로 운영한다. 대기 입력/승인은 실행 슬롯을 잡지 않는다. 정상 종료는 새 claim을 멈추고 진행 중 실제 스레드가 끝나 DB/사용량을 정산한 뒤 client와 lock을 닫는다. async task 취소만으로 외부 호출이 종료됐다고 판정하지 않는다.
