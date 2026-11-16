"""One local server. Credentials are entered through the authenticated settings screen."""

import argparse
import json
import secrets
import sqlite3
from pathlib import Path

import uvicorn
from pydantic import SecretStr

from . import config
from .contracts import AppError
from .engine import DaemonLock
from .platform.paths import default_home


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kelly — 작은 로컬 오피스 에이전트")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="127.0.0.1에서 서버 실행")
    serve.add_argument("--config", type=Path, default=Path("config.json"))
    serve.add_argument(
        "--access-code", action="store_true", help="실행 중인 서버의 연결 코드를 새로 발급"
    )
    for name in ("doctor", "export-log", "backup"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config.json"))
        if name != "doctor":
            command.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--home", type=Path, required=True)
    startup = commands.add_parser("startup", help="현재 사용자 로그인 시 시작 설정")
    startup.add_argument("action", choices=("enable", "disable", "status"))
    startup.add_argument("--config", type=Path, default=Path("config.json"))
    stop = commands.add_parser("stop", help="인증된 로컬 요청으로 정상 종료")
    stop.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args(argv)
    try:
        if args.command != "serve":
            from . import operations

            if args.command in {"startup", "stop"}:
                from . import startup

                result = (
                    startup.manage(args.config, args.action)
                    if args.command == "startup"
                    else startup.stop(args.config)
                )
            elif args.command == "restore":
                result = operations.restore(args.input, args.home)
            elif args.command == "doctor":
                result = operations.doctor(args.config)
            elif args.command == "export-log":
                result = operations.export_log(args.config, args.output)
            else:
                manifest = operations.backup(args.config, args.output)
                result = {"format_version": 1, "files_backed_up": len(manifest["files"])}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("ok", True) and result.get("state") != "unknown" else 1
        path = args.config.absolute()
        if not path.exists() and not args.access_code:
            config.create_config(path, config.AppConfig(home=str(default_home())))
        settings = config.load_config(path)
        home = config.home_path(settings, path)
        if args.access_code:
            try:
                with DaemonLock(home / "daemon.lock"):
                    pass
            except AppError as error:
                if error.code != "ALREADY_RUNNING":
                    raise
            else:
                raise AppError("NOT_FOUND", status=404)
            code = secrets.token_urlsafe(32)
            config.store_secret(home, "bootstrap", SecretStr(code))
            # Only this explicit local command displays the short-lived code, never server logs.
            print(code)
            return 0
        from .api import create_app

        server = None

        def request_stop():
            server.should_exit = True

        app = create_app(path, stop_callback=request_stop)
        print(f"http://127.0.0.1:{settings.port}")
        print("화면 연결 코드: 다른 터미널에서 같은 --config로 kelly serve --access-code 실행")
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=settings.port, workers=1, access_log=False)
        )
        server.run()
        return 0
    except (AppError, OSError, ValueError, sqlite3.Error) as error:
        message = (
            error.message_ko if isinstance(error, AppError) else "로컬 설정 파일을 확인해 주세요."
        )
        print(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
