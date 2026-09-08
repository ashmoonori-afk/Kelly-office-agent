"""Use the warmed CI build cache without forwarding service credentials."""

from scripts.check_install import environment


def test_isolated_install_uses_configured_cache_and_excludes_service_secrets(monkeypatch, tmp_path):
    cache = str(tmp_path / "ci-build-cache")
    monkeypatch.setenv("UV_CACHE_DIR", cache)
    monkeypatch.setenv("XAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("UV_INDEX_PASSWORD", "synthetic-registry-password")

    child = environment()

    assert child["UV_CACHE_DIR"] == cache
    assert not {"XAI_API_KEY", "TELEGRAM_BOT_TOKEN", "UV_INDEX_PASSWORD"} & child.keys()
