# Kelly 로컬 운영과 복구

Windows에서 프로젝트 가상환경을 활성화한 뒤 실행한다. 모든 명령은 지정한 `config.json`의 별도 Kelly 데이터만 다룬다. 기존 Birkin 데이터는 건드리지 않으며, 설치나 서버 실행만으로 시작 프로그램을 등록하지 않는다.

## 선택한 경우에만 로그인 시 시작

먼저 `kelly serve --config .\config.json`으로 설정을 만들고 일반 실행을 확인한다. 다음 명령을 직접 실행한 경우에만 현재 Windows 사용자의 작업 스케줄러에 등록한다.

```powershell
kelly startup enable --config .\config.json
kelly startup status --config .\config.json
kelly stop --config .\config.json
kelly startup disable --config .\config.json
```

`enable`은 **다음 로그인부터** 현재 가상환경의 `pythonw.exe`로 Kelly를 창 없이 시작한다. 현재 사용자의 로그인 트리거와 일반 권한을 사용하며, 관리자 권한·암호 저장·즉시 실행은 요청하지 않는다. 작업 이름은 `KellyOfficeAgent-<home 해시>`이고 명령 결과에 표시된다. 같은 home의 중복 서버는 기존 OS 잠금이 막는다. 등록할 이름에 다른 소유자나 실행 내용의 작업이 있으면 덮어쓰지 않는다. [Microsoft 로그인 트리거](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger?view=windowsserver2025-ps), [작업 실행 계정과 권한](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps)

숨김 실행 로그는 데이터 home의 `logs/server.stdout.YYYY-MM-DD.log`, `logs/server.stderr.YYYY-MM-DD.log`에 UTF-8로 추가된다. 날짜는 UTC이며, 쓰기 시점과 분당 유지보수에서 날짜가 바뀌면 새 파일로 회전하므로 서버를 계속 켜 두어도 적용된다. 접속 코드 자체는 로그에 남기지 않는다. 브라우저 연결이 필요하면 별도 터미널에서 `kelly serve --config .\config.json --access-code`를 실행하고 표시된 코드를 로컬 화면에 입력한다.

일별 파일은 해당 UTC 날짜가 끝난 뒤 30일이 지나고 마지막 수정 시각도 30일보다 오래됐을 때 정리한다. 예전 `server.stdout.log`·`server.stderr.log`는 마지막 수정 시각만으로 같은 30일 기준을 적용한다. 현재 기록 중이거나 다른 프로그램이 열어 둔 파일, 링크·junction·hardlink, 다른 이름·폴더는 삭제하지 않는다. 읽기 전용이거나 잠긴 파일은 다음 분에 다시 확인한다. 시계가 이전 관측 시각보다 뒤로 움직이면 회전·삭제를 보류하고 기존 파일에 계속 기록한다. 시계가 회복된 뒤 다시 정리한다. 별도 보관하려면 서버를 정상 종료한 뒤 본 앱 로그 규칙과 다른 이름이나 폴더에 복사한다.

`stop`은 로컬 접속 코드·세션·CSRF를 거쳐 서버에 종료를 요청하고 OS 잠금 반환을 확인한다. PID를 추정하거나 강제 종료하지 않는다. 종료가 35초 안에 확인되지 않으면 `unknown / shutdown_timeout`을 반환하므로 로그를 확인한다. `disable`은 다음 로그인 실행만 해제하며 이미 실행 중인 서버는 종료하지 않는다. 가상환경이나 설정 파일을 이동·제거하기 **전에** 기존 경로로 `disable`을 실행하고 필요하면 새 경로에서 다시 등록한다.

시작 등록 명령은 Task Scheduler 도구 경계 대체 검사로 검증했다. 실제 `pythonw` 숨김 실행·인증 종료·로그 기록·잠금 반환은 현재 Windows에서 검증했지만, 실제 사용자 로그인 작업 등록과 재로그인은 별도 확인이 필요하다.

## 무료 로컬 점검과 진단 내보내기

```powershell
kelly doctor --config .\config.json
kelly export-log --config .\config.json --output .\diagnostic.json
```

`doctor`는 SQLite 무결성·외래키·스키마, 남은 디스크 공간, 서울 시간대, 로컬 문서 모듈, 실제 UI 설정을 반영한 모델 개수와 Telegram 활성 상태를 확인한다. 키는 환경변수 또는 보호 파일의 **존재 여부만** 표시한다. 키를 복호화하거나 유효성을 검사하지 않으며 네트워크·모델 호출은 0회다. 아직 서버를 시작하지 않았다면 DB는 `not_initialized`로 표시된다.

`export-log`는 테이블별 개수, 업무·호출·전달 상태별 개수, 등록된 안전 오류 코드별 개수와 unknown 호출 수만 JSON으로 쓴다. 알려지지 않은 오류는 `OTHER`로 합친다. 업무 요청, 자료 내용, 프롬프트, 파일명·개인 경로, 채팅/요청 ID, 원격 오류 원문, 키는 내보내지 않는다. 출력 파일이 이미 있으면 덮어쓰지 않는다.

## 서버 정지 후 백업

`kelly stop --config .\config.json`이 `stopped`를 반환하는지 확인한다. 터미널의 Ctrl+C 또는 Ctrl+Break도 가능하며, 로그에서 `Application shutdown complete.`를 확인한다. OS 파일 잠금이 남아 있으면 백업은 `ALREADY_RUNNING`으로 거부한다. PID 파일을 지우거나 실행 중인 SQLite 파일을 복사해서 우회하지 않는다.

```powershell
New-Item -ItemType Directory -Path .\backups -Force
kelly backup --config .\config.json --output .\backups\snapshot-001
```

대상의 부모 폴더는 먼저 준비한다. `snapshot-001`은 아직 없어야 하며, 데이터 home 내부를 대상으로 지정할 수 없다. 백업은 **압축 없는 전용 디렉터리**다.

- `state.db`는 SQLite `Connection.backup()`으로 만든 일관된 사본이다. 관리 원본·초안·결과·기억과 DB의 기록·사용량·보관 핀을 보존한다.
- `config.json`에는 UI에서 저장한 실제 설정을 반영하고 home을 `.`으로 기록한다. `auth`, DPAPI 키, 접속 코드, 임시·비관리 파일은 복사하지 않는다.
- 모델 설정에 evidence ID와 SHA-256이 함께 고정된 `evaluations/<id>.json`만 보존한다. 고정된 보고서가 없거나 해시가 다르면 백업을 거부한다. 고정 근거가 없는 모델은 그대로 미자격 상태이며, 백업은 자격 보고서를 생성하거나 통과 판정을 바꾸지 않는다.
- 삭제했지만 남아 있는 기억 파일은 보존한다. 이미 지운 기억과 보관 정리된 결과의 tombstone은 DB와 `manifest.json`의 `omitted_files`에 남긴다. 활성 자료 파일이 없으면 오류다.

`manifest.json`은 성공한 마지막 단계에서 쓴다. 모든 실제 파일의 정확한 상대 경로·크기·SHA-256을 담는다. 파일 10,000개, 합계 1 GiB, DB 256 MiB, 문서 25 MiB, 평가 보고서 및 manifest 각각 2 MiB가 상한이다. 백업에는 업무 원문과 결과가 포함되며 별도 암호화하지 않는다. 보관 위치의 접근 권한을 관리한다.

## 새로운 home으로 복구

```powershell
kelly restore --input .\backups\snapshot-001 --home .\restored-home
kelly doctor --config .\restored-home\config.json
kelly serve --config .\restored-home\config.json
```

복구 대상은 **존재하지 않는 새 경로**여야 한다. 기존 빈 폴더도 거부한다. manifest의 파일 집합·크기·해시, DB 무결성과 관리 파일 참조를 먼저 검증한다. DB는 검증된 바이트를 SQLite backup API로 복원한다. 원래 데이터와 백업본을 변경하지 않는다.

절대 경로, `..`, 숨은 추가 파일, junction·symlink·reparse point, 크기 초과를 거부한다. ZIP을 입력받거나 압축을 해제하는 기능이 없어 ZIP 폭탄도 처리하지 않는다. 검증 실패 시 새 home을 만들지 않는다. 파일 쓰기 중 장애가 나면 미완성 폴더가 남을 수 있다. manifest 없는 백업 또는 최종 config 없는 복구본을 사용하지 말고 원인을 확인한 후 새로운 대상 경로로 다시 실행한다.

복구 후에는 다음 재확인이 필요하다.

- 모델 키와 Telegram 토큰은 해당 Windows 계정에서 다시 입력한다. 클라우드 전송 동의는 해제되고 Telegram은 비활성화된다.
- 모든 자동화는 일시정지된다. 폴더 접근 등록은 제거되므로 폴더를 다시 선택하고 작업 범위·검토 해시를 승인한다.
- Telegram pairing과 버튼 권한은 초기화된다. 연결 revision/epoch를 올려 이전 채팅과 콜백을 재사용하지 않는다.
- 대기·승인된 외부 작업 승인은 만료된다. 대기 전달은 취소하고, 보내던 전달은 `unknown`으로 보존한다. 자동 재전송하지 않는다.
- 실행 중 외부 효과와 미정산 모델 호출은 기존 재시작 복구 규칙에 따라 unknown을 보존한다. 나머지 미완료 업무는 `RESTORE_REVIEW_REQUIRED` 사유로 취소한다. 완료 결과·receipt·실제 사용량은 유지하며, 복구 직후 중단된 업무가 자동 실행되지는 않는다.

## 잠긴 의존성과 wheel 설치 검사

```powershell
python .\scripts\check_install.py
```

이 스크립트는 설치된 `uv`를 사용한다. 오프라인 캐시로 wheel을 빌드하고, `uv.lock`에서 프로젝트 자체를 제외한 런타임 의존성을 내보내 SHA-256 확인을 요구하며 새 가상환경에 설치한다. 잠금 파일을 수정하지 않는다. 캐시에 빌드 도구가 없으면 실패하며 도구를 임의로 업데이트하지 않는다. 런타임 의존성 다운로드에는 잠금에 적힌 패키지 저장소를 사용할 수 있다.

별도 환경에서 MIT 메타데이터·SBOM·라이선스 고지·패키지 리소스·`doctor`·의존성 정합성을 확인하고, 숨긴 실제 서버를 loopback 포트에서 시작해 health와 `kelly stop`의 인증된 정상 종료를 검증한다. 테스트 자격증명은 전달하지 않는다. 마지막에 그 가상환경에서 `kelly-office-agent`만 제거하고 데이터 보존과 import 불가를 확인한다. 결과는 `.birkin-lite/install-check-*/result.json`에 남긴다.

이는 **현재 Windows 호스트의 독립 가상환경 검사**다. Windows 새 설치 이미지, 실제 provider 품질·사용량, 실제 Telegram 전달, 보조공학 호환성을 검증했다는 뜻은 아니다. 이 항목들은 별도 실제 환경 검증이 필요하다.
