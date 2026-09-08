# xAI 연결 계약

검토일: 2026-09-08. 실제 계정 호출 없이 `httpx.MockTransport`로 검증했다. API 키가 준비된 뒤 계정별 모델 조회와 유료 smoke 검사가 별도로 필요하다.

`XAIClient(api_key, *, http_transport=None)`는 HTTPS 클라이언트 하나를 재사용한다. `with` 또는 `close()`로 닫는다. `.transport(request, binding)`와 `.complete(request, binding)`는 같은 단일 전송이다. 업무 실행은 반드시 기존 `metered_complete(..., transport=client.transport)`를 통해 호출한다. 어댑터에 가격이나 역할 선택을 넣지 않는다.

`list_models(timeout_s=10)`은 `GET /v1/models` 응답의 모델 ID만 반환한다. 가용성은 그 API 키에 관한 관측이며 구조화 출력, 품질, 추론 설정 지원을 증명하지 않는다. 모델 목록에 있는 가격은 여기서 사용하지 않는다. [공식 API 계약](https://api.x.ai/api-docs/openapi.json)

생성 요청은 `POST /v1/chat/completions` 한 번이다. Bearer 인증, TLS 검증, 프록시 환경변수 무시, 리다이렉트 금지, SDK/HTTP 내부 재시도 0회, `stream:false`, `n:1`, 도구 없음이 고정된다. 요청의 timeout을 HTTP 각 단계에 적용하고, 디코딩한 응답이 1MiB를 넘으면 읽기를 중단한다. 연결 오류나 429/5xx 재시도는 계측 wrapper만 담당한다. Read timeout 뒤에는 청구 여부를 추측하지 않는다.

`response_format.json_schema`에 원래 출력 스키마와 `strict:true`를 넣는다. 선택 필드와 순환하지 않는 `$ref/$defs`는 유지한다. `discriminator` 주석만 제거한다. xAI의 `oneOf`는 `anyOf`처럼 동작하므로 필수 키와 닫힌 객체 또는 구별되는 상수로 대안의 배타성을 증명할 수 없는 경우 전송 전에 거부한다. 순환 참조와 문서상 거부되는 형식도 거부한다. 제약을 삭제해 전송을 성공시키지 않는다. 일부 길이·개수 제약은 공급자 보장 범위를 넘을 수 있으므로 기존 Pydantic 계약으로 응답을 다시 검증한다. [구조화 출력 지원 범위](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)

사용량 정규화는 이 엔드포인트에만 적용한다. 입력은 `prompt_tokens`, 캐시 입력은 그 부분집합이다. 출력은 `completion_tokens + completion_tokens_details.reasoning_tokens`이며 추론 토큰을 다시 더하지 않는다. 공식 응답 예의 32 입력·9 가시 출력·94 추론은 총 135, 정규화 출력 103이다. `total_tokens`와 모순되면 출력 합계를 unknown으로 남기고 숫자 사용량 원문과 검증 가능한 입력을 보존한다. 없는 캐시 쓰기나 추론 값은 0으로 만들지 않는다. [Chat Completions 응답 예와 Usage 정의](https://api.x.ai/api-docs/openapi.json)

`max_completion_tokens`는 가시 출력에만 적용되며 추론·함수 호출 토큰을 포함하는 총생성량 상한이 아니다. 추론 노력 값은 선택된 binding의 허용목록과 공급자 지원 범위를 함께 확인한다. [공식 요청 필드 정의](https://api.x.ai/api-docs/openapi.json)

HTTP 오류·거절·잘못된 JSON 내용·출력 잘림·계약 불일치도 이미 읽은 사용량을 `TransportError`에 담아 정산하게 한다. 응답 전체가 잘못된 JSON이거나 크기 제한/타임아웃으로 완전히 읽지 못한 경우 사용량을 추측하여 추출하지 않는다. 프롬프트, 본문, 비밀값은 예외나 호출 원장에 기록하지 않으며 사용량은 알려진 숫자 필드만 남긴다.

## 내용 응답의 참조 계약

`office-task-v2`부터 보고서·요약·답신·검색 답변의 본문은 참조 배열로 반환한다. 예를 들어 `sections.content.paragraphs`는 `[[{"claim_id":"c1"}]]`이고, 문장은 `claims`의 `{"id":"c1","text":"확인한 내용","kind":"fact","evidence_ids":["공급된 근거 ID"]}`에 한 번만 쓴다. 제목·불릿·표·답신 수신인도 같은 규칙을 따른다. 원문 없는 자유 작성은 `kind:"inference"`, 검색에서 근거를 찾지 못한 답은 `uncertainty_id`와 `uncertainties:[{id,text,evidence_ids}]`로 표현한다. 해석 문구는 결과 문서에서 별도로 표시한다.

집계 수치는 `numeric_id`로만 배치한다. `NumericClaim`에는 `id`가 필요하고, 비율에는 `denominator`도 필요하다. 공통 `verification.render_content`가 대상 열·그룹·연산·값·단위를 함께 렌더링한다. 없는 참조·사용하지 않은 선언·임의 본문 문자열을 거부하며, 자료 기반 문장의 숫자는 그 문장이 직접 인용한 근거와 대조한다. 입력 자료 없는 자유 작성에서는 사용자 요청에 명시된 숫자를 사용할 수 있다. 다른 선언이나 집계값을 빌려 임의 문장의 숫자를 허용하지 않는다. 참조 조각 사이에는 공백을 넣어 `10`과 `0`이 새 숫자 `100`으로 붙지 않게 한다. 이는 출력과 선언의 구조적 연결 검사이며, 자연어 주장과 인용의 의미 일치까지 결정적으로 증명하지 않는다.

결정적 도구의 `DocumentContent` 계약과 이미 완료한 파일·원장은 그대로 둔다. 구형 본문을 자동으로 근거 있는 주장으로 변환하지 않으므로 구형 미완료 응답은 이 계약에서 재사용할 수 없다. 변경된 프롬프트·스키마가 모델 응답 캐시 키에 포함되고, 평가의 코드 환경 hash도 바뀐다. 기존 평가 결과를 새 계약의 품질 증거로 재표시하지 않는다. TODO 난도·생성 역할과 보완·의미 검토를 합친 논리 호출 두 번 한도는 유지한다.

## Command Code 키 보관

설정 → API 키와 Telegram pairing → 키 종류에서 **Command Code API 키**를 선택해 저장한다. 현재 Windows 사용자의 DPAPI로 암호화하며 빈 값으로 저장하면 삭제한다. 실행 프로세스의 `COMMAND_CODE_API_KEY`가 있으면 우선 사용하고 UI 저장·삭제를 막는다. 환경변수 이름은 [Command Code 공식 설정 문서](https://commandcode.ai/docs/settings)를 따른다.

현재는 키 입력·보관만 지원한다. 키 저장으로 Command Code 모델 연결, 모델 조회, 생성 요청을 실행하지 않으며 xAI 모델 설정과 품질 자격은 바꾸지 않는다.

## 합성 연결 검사 실행

키는 실행 프로세스의 `XAI_API_KEY` 환경변수를 우선 읽고, 없을 때 설정 화면에서 저장한 현재 Windows 사용자 DPAPI 저장소를 읽는다. 연결 검사도 서버와 같은 `runtime_config`로 파일 설정에 UI의 DB 설정을 반영한다. `state.db`는 기존 보호된 읽기 전용 연결로 읽으며, UI에서 추가·활성화하거나 비활성화한 모델 상태를 그대로 사용한다. UI 설정이 없으면 파일 설정을 사용한다.

설정 화면에서 모델을 준비하거나 `config.example.json`을 별도 설정 파일로 복사한다. 실제 계정의 모델 ID, `enabled:true`, 허용 프로젝트, `M` 역할, `structured_output`·`planning` 기능을 설정한 뒤 다음 명령을 실행한다. 예제의 모델 ID는 실제 가용 모델을 뜻하지 않으며 기본값은 비활성 상태다.

```powershell
.\.venv\Scripts\python.exe -m birkin_lite.live_smoke --config .\config.json --model YOUR_ACCOUNT_MODEL_ID --allow-live
```

`--allow-live`가 없으면 파일·키를 읽거나 HTTP 요청을 보내기 전에 거부한다. 명시된 실행은 계정 모델 목록 조회 1회와 합성 계획 생성 최대 1회를 수행한다. 연결 오류, 429, 5xx, 잘못된 내용에도 생성 재시도·스키마 수정 호출은 하지 않는다. 가시 출력 상한은 설정값과 1,024 중 작은 값이며 추론 토큰의 총량 상한을 의미하지 않는다. 이 검사는 실제 API 사용료가 발생할 수 있다.

입력은 프로그램에 고정한 합성 문장뿐이다. `PlanningResultV1` 검증을 통과하고 TODO가 정확히 하나이며 제목이 `LIVE_MODEL_OK`와 일치할 때만 `accepted:true`로 기록한다. 생성한 계획을 설치하거나 실행하지 않는다. 성공 여부와 별개로 받은 사용량은 `<home>/evidence/provider-probe.db`의 호출 원장에 남기고, 실패 응답에서 사용량을 관측하지 못하면 null과 unknown을 보존한다.

결과 JSON은 `<home>/evidence/provider-live-<run_id>.json`에 새 파일로 저장된다. 테스트에서 `http_transport`를 주입하면 파일 이름은 `provider-mock-<run_id>.json`이며 결과와 증거의 `live`는 항상 false다. 모든 연결 검사 결과의 `qualified_for_office`는 false이며, 연결 성공으로 모델 품질 자격이나 Office 실제 실행 성공을 인정하지 않는다. 현재 증거는 `tests/test_live_smoke.py`의 모의 HTTP 계약 검증이며 실제 계정 호출은 아직 수행하지 않았다.

정상 업무의 품질 자격은 별도 실측 보고서가 필요하다. 선언한 역할마다 서로 다른 fixture 20개 이상, 필수 schema·숫자·출처·권한 검사 통과, 실제 provider 사용량과 기존 H 기준 채택 비교를 유지한다. 역할별 업무 범위는 계획이 M/H, 의미 검토가 H이며, 요약·보고서 서술·회의 행동 추출·답신 초안·검색 답변·분석은 선언한 L/M/H 역할마다 검사한다. L의 계획이나 L/M의 의미 검토를 실측했다는 보고서는 거부한다. 연결 검사 한 건은 이 자격 요건을 충족하지 않는다.
