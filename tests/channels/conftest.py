from contextlib import closing

import pytest

from kelly import db

from .approval_support import Scenario, pair, pending
from .fake import FakeAdapter


@pytest.fixture
def notice(tmp_path):
    adapter = FakeAdapter()
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        pair(conn, adapter)
        yield Scenario(conn, adapter, tmp_path, pending(conn))


@pytest.fixture
def web(tmp_path, monkeypatch):
    from datetime import datetime

    from fastapi.testclient import TestClient

    from kelly import api, config

    from .approval_support import NOW

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    # P1 is on a separate branch. Replace only config/secret platform boundaries;
    # HTTP authentication, SQLite, the engine and the channel router remain real.
    settings = config.AppConfig(home=str(tmp_path), project_ids=["sales"])
    secrets = {}
    monkeypatch.setattr(config, "load_config", lambda _: settings)
    monkeypatch.setattr(config, "store_secret", lambda _home, name, value: secrets.update({name: value}))
    monkeypatch.setattr(config, "stored_secret", lambda _home, name: secrets.get(name))
    monkeypatch.setattr(api, "datetime", FixedClock)
    app = api.create_app(tmp_path / "config.json", start_background=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/bootstrap", json={"code": app.state.bootstrap_secret},
                               headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 200
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": response.json()["csrf_token"],
                   "Idempotency-Key": "synthetic-web-request"}
        yield client, headers
