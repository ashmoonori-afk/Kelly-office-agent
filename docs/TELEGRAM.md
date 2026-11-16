# Telegram 설정·복구와 L03 실제 검증

기준: 2026-09-08의 `telegram.py`, `config.py`, `db.py`, `planning.py`, `engine.py`. **현재 본인 테스트 채팅에 대한 실제 호출 승인은 없으며 L03은 미검증이다.** 아래 절차는 별도 승인 후 로컬 운영자가 수행한다. 문서 작성이나 MockTransport 테스트 통과는 실제 발송 승인이 아니다.

## 설정과 권한

- 본인 전용 테스트 봇을 [BotFather](https://core.telegram.org/bots/features#botfather)에서 생성한다. 토큰은 채팅·문서·소스·명령 기록에 붙여넣지 않고 실행 프로세스의 `TELEGRAM_BOT_TOKEN`에만 넣는다. `read_telegram_token()`은 `SecretStr` 또는 `None`을 반환한다.
- 앱 설정은 `telegram: {enabled: true, connection_id: "telegram-l03", project_id: "sales"}`이며 해당 프로젝트가 `project_ids`에 있어야 한다. 이 설정만으로 pairing이나 발송 권한이 생기지 않는다. 현재 문서는 공개 Python API를 직접 호출하는 운영 절차다. P11 화면·CLI 명령이 모두 준비되었다고 가정하지 않는다.
- `register_connection(..., client=client)`로 자격 증명을 먼저 연결한 뒤 `create_pairing()`을 호출한다. 로컬에 표시된 코드를 본인 봇의 개인 대화에 `/pair <code>` 또는 `/start <code>`로 보낸다. 코드는 10분·1회 사용이고, 5회 오입력하면 새 로컬 코드가 필요하다.
- actor·프로젝트·봇 연결·pairing revision·Telegram user/chat이 모두 맞아야 한다. 그룹·봇 발신·전달 메시지·Telegram 첨부파일 입력은 지원하지 않는다. 파일 입력은 로컬에서 명시적으로 공급한다. 문서 속 수신인 지시나 일반 메시지의 “승인”은 권한이 아니다.
- 형식 명령은 `/text`, `/md`, `/csv`, `/xlsx`, `/docx` 뒤에 업무 본문을 붙인다. 접두사는 저장할 형식을 정하고 본문에서 제거된다. 없는 명령이나 빈 본문은 거부한다. 형식 명령이 일반 업무의 모델 필요성을 없애지는 않는다.
- long polling만 사용한다. webhook이 있으면 `check_webhook()`/`poll_once()`가 중단하므로 운영자가 기존 연결을 검토한다. 자동 webhook 삭제나 대기 업데이트 폐기는 하지 않는다. Telegram의 polling과 webhook은 함께 사용할 수 없다. [getUpdates](https://core.telegram.org/bots/api#getupdates)

토큰을 교체하면 새 `TelegramClient`로 다시 등록한다. 자격 증명 변경은 pairing 권한과 수신 epoch를 갱신하고 cursor를 초기화한다. 과거 inbox/run은 남으며 새 epoch의 같은 update ID와 충돌하지 않는다. 기존 pairing·버튼·예약 전달 권한을 재사용하지 말고 새 코드와 명시 승인을 거친다. fingerprint는 토큰 원문이 아니다.

## 전달과 복구

| 상태 | 운영 동작 |
|---|---|
| `sent` | `remote_id`는 Telegram 응답의 message ID다. 단말에서 실제 수신·파일 열기를 별도로 확인한다. |
| `retry_wait` | 유효한 429 `retry_after`까지 기다린다. 시도는 총 3회 이하다. [ResponseParameters](https://core.telegram.org/bots/api#responseparameters) |
| `unknown` | 전달 여부 확인 필요. 자동 재발송하지 않는다. 같은 outbox를 `pending`으로 SQL 수정하지 않는다. |
| `sending` 상태에서 프로세스 종료 | 이전 프로세스 종료·독점 잠금을 확인한 복구에서 `resume_after_restart(..., previous_process_is_dead=True)`가 `unknown`으로 바꾼다. 해당 인자는 확인 없이 넘기지 않는다. |
| `failed` / `cancelled` | 오류 코드나 취소 원인을 확인한다. 문서를 다시 생성하는 트리거로 쓰지 않는다. |

수신은 inbox·run·cursor를 한 트랜잭션으로 저장한다. 전달은 `(run_id, delivery_kind, recipient_ref, content_hash)`로 중복을 막지만, 서버 수신 후 응답이 유실될 수 있으므로 원격 exactly-once를 보장하지 않는다. 실패해도 `runs.state=succeeded`와 완료 파일은 유지된다.

파일은 성공한 동일 run의 현재 완료 outputs/receipt가 가리키는 검증 통과 artifact만 전달하며, 보내기 직전에 hash·권한을 다시 확인한다. 단순히 파서 검사를 통과한 중간 초안을 자동 발송하지 않는다. 결과는 짧은 설명과 파일로 전달하며 휴대폰에서 localhost 링크를 열 수 있다고 안내하지 않는다. 파일 전송 성공 응답은 Message다. [sendDocument](https://core.telegram.org/bots/api#senddocument)

버튼은 짧은 opaque 참조다. 서버가 결정·actor·연결 revision·대상 revision·proposal hash·만료·사용 여부를 재검사한다. `answerCallbackQuery` 응답은 버튼 대기 표시를 끝내는 동작이며 업무 완료 증거가 아니다. [answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)

## L03: 별도 승인 후 본인 채팅에서 수행

승인 범위를 **본인 개인 봇의 pairing, 합성 요청 1건, XLSX 결과 1건, 자동화 활성화 확인 버튼 1건과 callback**으로 기록한다. 모델 호출은 0건이다. 자동화는 미래 once를 활성화한 뒤 즉시 pause하여 실제 예약 업무를 실행하지 않는다. 동일 봇을 읽는 다른 poller와 이 테스트 데이터 폴더의 daemon/scheduler를 먼저 중지한다.

`birkin-lite` 디렉터리에서 `.venv\Scripts\python.exe`로 연 로컬 Python 세션에 아래 블록을 순서대로 입력한다. 토큰 입력과 pairing 코드 표시가 녹화·로그에 남는 터미널은 사용하지 않는다. 아래 `client`/`poll_once`/`deliver_once` 호출은 실제 네트워크 작업이므로 승인 전에 실행하지 않는다.

### 1. 격리된 로컬 저장소와 실제 pairing

```python
import getpass, io, json, os, time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from openpyxl import load_workbook
from kelly import db
from kelly.artifacts import import_source, open_artifact
from kelly.automation import AutomationConfig
from kelly.config import read_telegram_token
from kelly.contracts import Actor, PrivacyPolicy, Receipt, TaskSpec
from kelly.engine import execute_next
from kelly.planning import prepare_run
from kelly.telegram import (
    TelegramClient, register_connection, create_pairing, poll_once,
    create_callback, prepare_delivery, deliver_once,
)
from kelly.tools import registry
from kelly.usage import summarize_usage

if "TELEGRAM_BOT_TOKEN" not in os.environ:
    os.environ["TELEGRAM_BOT_TOKEN"] = getpass.getpass("Telegram token (숨김): ")
token = read_telegram_token()
assert token is not None
clock = lambda: datetime.now(UTC)
home = (Path.cwd() / ".birkin-lite" / "live-telegram-l03").resolve()
assert not (home / "state.db").exists(), "이전 검증과 구분되는 새 폴더를 선택하세요"
conn = db.init_db(home / "state.db")
actor, connection_id = Actor(id="owner", project_ids=["sales"]), "telegram-l03"
client = TelegramClient(token)
register_connection(conn, actor, connection_id, "sales", now=clock(), client=client)
client.check_webhook()

def receive_until(matches):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for event in poll_once(client, conn, actor, connection_id, now=clock, timeout=5):
            if matches(event):
                return event
    raise TimeoutError("60초 안에 해당 업데이트를 받지 못했습니다")

pair = create_pairing(conn, actor, connection_id, now=clock())
print("본인 봇 개인 대화에 전송: /pair " + pair["code"])
```

본인이 휴대폰에서 표시된 pairing 명령을 보낸 뒤 다음을 실행한다. pairing 코드와 토큰을 증거 파일에 넣지 않는다.

```python
paired = receive_until(lambda e: e["payload_json"].get("kind") == "pairing")
assert paired["state"] == "enqueued"
del pair
```

### 2. 실제 수신 run에 합성 CSV를 공급하고 D 업무 완료

본인 채팅에 **`/xlsx 금액 합계 계산해줘`**를 보낸다. 가짜 Telegram update나 새 로컬 run을 만들지 않는다.

```python
incoming = receive_until(lambda e: e["payload_json"].get("kind") == "request")
run_id = incoming["run_id"]
run = db.get_run(conn, run_id, actor)
assert run["source"] == "telegram"
assert run["request_json"]["request"] == "금액 합계 계산해줘"
assert run["request_json"]["output_format"] == "xlsx"
content = "부서,금액\n영업,1\n지원,2\n".encode("utf-8")
source = import_source(home, conn, io.BytesIO(content), {"filename": "l03.csv"}, actor, "sales")
spec = TaskSpec(
    intent="aggregate", source_ids=[source["id"]],
    inputs={"value_columns": ["금액"]},
    acceptance=["금액 열 합계를 검증한 결과표로 저장"],
)
db.supply_inputs(conn, run_id, actor, spec, run["revision"])
tools = registry(home)
prepared = prepare_run(
    conn, home, run_id, actor, tools=tools, capability_catalog=[],
    privacy_policy=PrivacyPolicy(project_id="sales"), complete=None,
)
assert all(t["executor"]["type"] == "tool" for t in prepared["plan_json"]["current"]["todos"])
for _ in range(8):
    finished = execute_next(conn, run_id, actor=actor, tools=tools, complete=None, now=clock(), home=home)
    if finished["state"] == "succeeded":
        break
assert finished["state"] == "succeeded", finished["error_code"]
receipts = [v["receipt"] for v in finished["result_json"]["outputs"].values() if "receipt" in v]
assert len(receipts) == 1
receipt = Receipt.model_validate(receipts[0])
assert receipt.run_id == run_id and receipt.validation.status == "pass"
with open_artifact(home, conn, receipt.artifact_id, actor, "sales") as stream:
    workbook = load_workbook(stream, data_only=True)
    assert list(workbook["Result"].values) == [("그룹", "금액"), ("전체", "3.00")]
    workbook.close()
with open_artifact(home, conn, source["id"], actor, "sales") as stream:
    assert stream.read() == content
usage = summarize_usage(conn, run_id, actor)
assert usage["usage_state"] == "not_used" and usage["total_tokens"] == 0
assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
```

예외나 `awaiting_input` 등에서 멈추면 그 상태를 남긴다. SQL로 형식·완료 상태·receipt를 위조하거나 모델을 추가 호출하여 검증을 통과시키지 않는다.

### 3. 동일 run의 XLSX 전달과 실제 활성화 버튼

```python
outbox = prepare_delivery(conn, actor, connection_id, run_id,
    artifact_id=receipt.artifact_id, text="L03 합성 CSV 합계 3.00", now=clock())
delivered = deliver_once(client, conn, home, actor, now=clock())
assert delivered["id"] == outbox["id"]
assert delivered["state"] == "sent", "현재 전달 상태를 기록하고 아래 진행을 중단하세요"
assert delivered["remote_id"]
```

본인이 실제 채팅에서 파일을 내려받아 `Result` 시트의 `전체 / 3.00`을 확인한다. 다음은 가짜 외부 쓰기 approval step 대신 **현재 구현된 자동화 활성화 승인**을 검증한다. scheduler tick은 호출하지 않는다.

```python
bot = db.create_automation(conn, actor, AutomationConfig(
    name="L03 활성화 확인 후 중지", project_id="sales", recipe="aggregate",
    instruction="금액 합계 계산해줘",
    trigger={"kind": "once", "at": (clock() + timedelta(days=1)).isoformat()},
    source_scope={"artifact_ids": [source["id"]]}, output_format="xlsx",
    delivery={"channel": "telegram", "recipient_ref": "owner"},
    authority={"mode": "read_and_draft", "send_result_to_owner": True},
), now=clock())
proposal_hash = db.automation_proposal_hash(bot)
button = create_callback(conn, actor, connection_id,
    kind="automation_enable", target_id=bot["id"], now=clock())
notice = prepare_delivery(conn, actor, connection_id, run_id,
    text="L03: 합성 CSV의 금액 합계 once 예약을 내일로 활성화합니다. 결과 대상은 본인입니다. 확인 직후 PC에서 중지하며 예약 업무는 실행하지 않습니다.",
    reply_markup={"inline_keyboard": [[{"text": "테스트 예약 활성화", "callback_data": button}]]},
    now=clock())
button_delivery = deliver_once(client, conn, home, actor, now=clock())
assert button_delivery["id"] == notice["id"] and button_delivery["state"] == "sent"
```

본인이 버튼을 누른 직후 아래를 실행한다. 실패해도 `finally`에서 현재 활성화를 해제한다. 세션 자체가 끊기면 같은 DB를 다시 열어 `get_automation`의 현재 revision으로 pause를 먼저 수행한다.

```python
try:
    clicked = receive_until(lambda e: e["payload_json"].get("callback_result", {}).get("id") == bot["id"])
    activated = db.get_automation(conn, bot["id"], actor)
    assert clicked["state"] == "enqueued" and activated["enabled"] == 1
    assert activated["revision"] > bot["revision"]
finally:
    current = db.get_automation(conn, bot["id"], actor)
    paused = db.set_automation_enabled(conn, bot["id"], current["revision"], False, actor, now=clock())
assert paused["enabled"] == 0
assert conn.execute("SELECT count(*) FROM runs WHERE automation_id=?", (bot["id"],)).fetchone()[0] == 0
```

### 4. 증거와 종료

다음 항목만 로컬 증거 JSON에 저장한다. 토큰·전체 HTTP URL·pairing 코드·opaque 버튼 값·채팅 원문·상대방 user/chat ID는 제외한다.

```python
evidence = {
    "mode": "live", "checked_at": clock().isoformat(),
    "ingress_update_id": incoming["payload_json"]["update_id"], "run_id": run_id,
    "source_sha256": source["sha256"], "artifact_id": receipt.artifact_id,
    "artifact_sha256": receipt.artifact_hash, "document_remote_id": delivered["remote_id"],
    "button_remote_id": button_delivery["remote_id"],
    "callback_update_id": clicked["payload_json"]["update_id"],
    "automation_id": bot["id"], "proposal_hash": proposal_hash,
    "activated_revision": activated["revision"], "paused_revision": paused["revision"],
    "model_calls": 0, "total_tokens": usage["total_tokens"], "automation_runs": 0,
    "phone_file_open_confirmed": False,
}
(home / "evidence").mkdir(exist_ok=True)
(home / "evidence" / "l03.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
client.close()
conn.close()
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
```

`phone_file_open_confirmed`는 본인의 실제 확인 후에만 바꾼다. 토큰이 노출된 예외·request 객체·환경 전체를 출력하지 않는다. 실패한 경우 `mode`, 시간, 안전한 오류 코드, 알려진 run/outbox ID와 상태만 별도로 기록하고 성공 JSON을 만들지 않는다. `unknown`은 remote ID가 없을 수 있으며, 채팅 확인 전 자동 재발송 횟수는 0이어야 한다. 중간 실패에도 필요하면 위 pause 절차를 수행하고 `client.close()`, `conn.close()`, 환경 변수 제거를 직접 마친다.

L03 완료는 실제 수신·동일 run의 검증된 파일 전달·단말 확인·실제 활성화 callback·최종 pause·모델 0호출 증거가 모두 있을 때만 표시한다. 이 절차는 Telegram 첨부파일 수신, 외부 업무 시스템 쓰기 승인, 무인 daemon 수명주기 검증을 대신하지 않는다.
