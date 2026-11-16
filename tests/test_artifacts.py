import io
import sqlite3
from pathlib import Path

import pytest

from kelly.artifacts import create_draft, import_source, open_artifact, publish_draft
from kelly.contracts import Actor, AppError, RunRequest
from kelly.db import create_run, init_db

OWNER = Actor(id="owner", project_ids=["sales", "private"])


def test_import_identity_access_tampering_and_original(tmp_path):
    conn = init_db(tmp_path / "state.db")
    home = tmp_path / "files"
    original = "회의 결과: 담당자 미정".encode()
    source = import_source(
        home, conn, io.BytesIO(original), {"filename": "회의록.txt"}, OWNER, "sales"
    )
    assert source["kind"] == "source"
    with open_artifact(home, conn, source["id"], OWNER, "sales") as handle:
        assert handle.read() == original
    with pytest.raises(AppError), open_artifact(home, conn, source["id"], OWNER, "private"):
        pass
    with (
        pytest.raises(AppError),
        open_artifact(home, conn, source["id"], Actor(id="other", project_ids=["sales"]), "sales"),
    ):
        pass
    Path(home / source["relative_path"]).write_bytes(b"changed")
    with pytest.raises(AppError) as error, open_artifact(home, conn, source["id"], OWNER, "sales"):
        pass
    assert error.value.code == "SOURCE_CHANGED"
    conn.close()


@pytest.mark.parametrize(
    "filename",
    ["../escape.txt", "C:\\escape.txt", "test.txt:stream", "test.txt.", "a\x00.txt", "slides.pptx"],
)
def test_bad_input_names_and_unsupported_format(tmp_path, filename):
    conn = init_db(tmp_path / "state.db")
    with pytest.raises(AppError):
        import_source(
            tmp_path / "files", conn, io.BytesIO(b"hello"), {"filename": filename}, OWNER, "sales"
        )
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    conn.close()


def test_limit_and_disguised_document(tmp_path):
    conn = init_db(tmp_path / "state.db")
    for data, name in [(b"x" * (25 * 1024 * 1024 + 1), "large.txt"), (b"not a zip", "fake.docx")]:
        with pytest.raises(AppError):
            import_source(
                tmp_path / "files", conn, io.BytesIO(data), {"filename": name}, OWNER, "sales"
            )
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert not list((tmp_path / "files").rglob("*.tmp"))
    conn.close()


def test_publication_is_idempotent_and_never_overwrites(tmp_path):
    conn = init_db(tmp_path / "state.db")
    home = tmp_path / "files"
    run = create_run(conn, OWNER, "ui", "one", RunRequest(request="초안", project_id="sales"))
    draft = create_draft(
        home,
        conn,
        io.BytesIO("보고서 본문".encode()),
        "result.txt",
        OWNER,
        "sales",
        run["id"],
        provenance={"source_ids": []},
    )
    result = publish_draft(home, conn, draft["id"], OWNER, "sales", run_id=run["id"], step_id="t1")
    again = publish_draft(home, conn, draft["id"], OWNER, "sales", run_id=run["id"], step_id="t1")
    assert result["artifact_id"] == again["artifact_id"]
    with open_artifact(home, conn, draft["id"], OWNER, "sales") as handle:
        assert handle.read().decode() == "보고서 본문"
    other = create_draft(
        home, conn, io.BytesIO(b"different"), "other.txt", OWNER, "sales", run["id"], provenance={}
    )
    with pytest.raises(AppError):
        publish_draft(home, conn, other["id"], OWNER, "sales", run_id=run["id"], step_id="t1")
    assert conn.execute("SELECT count(*) FROM artifacts WHERE kind='result'").fetchone()[0] == 1
    conn.close()


def test_source_change_during_import_is_rejected(tmp_path):
    conn = init_db(tmp_path / "state.db")
    source = tmp_path / "original.txt"
    source.write_bytes(b"before")

    class ChangingFile:
        def __init__(self, handle):
            self.handle = handle
            self.changed = False

        def fileno(self):
            return self.handle.fileno()

        def read(self, size):
            chunk = self.handle.read(size)
            if not self.changed:
                self.changed = True
                with source.open("ab") as writer:
                    writer.write(b" during import")
            return chunk

    with source.open("rb") as handle, pytest.raises(AppError) as error:
        import_source(
            tmp_path / "files",
            conn,
            ChangingFile(handle),
            {"filename": "input.txt"},
            OWNER,
            "sales",
        )
    assert error.value.code == "SOURCE_CHANGED"
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert not list((tmp_path / "files").rglob("*.tmp"))
    conn.close()


def test_publication_recovers_after_real_database_insert_failure(tmp_path):
    conn = init_db(tmp_path / "state.db")
    home = tmp_path / "files"
    run = create_run(conn, OWNER, "ui", "crash", RunRequest(request="초안", project_id="sales"))
    draft = create_draft(
        home,
        conn,
        io.BytesIO(b"published bytes"),
        "report.txt",
        OWNER,
        "sales",
        run["id"],
        provenance={},
    )
    conn.execute(
        "CREATE TRIGGER crash_result BEFORE INSERT ON artifacts WHEN NEW.kind='result' "
        "BEGIN SELECT RAISE(ABORT,'simulated disk transaction failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        publish_draft(home, conn, draft["id"], OWNER, "sales", run_id=run["id"], step_id="t1")
    published = list((home / "exports").iterdir())
    assert len(published) == 1 and published[0].read_bytes() == b"published bytes"
    conn.execute("DROP TRIGGER crash_result")
    receipt = publish_draft(home, conn, draft["id"], OWNER, "sales", run_id=run["id"], step_id="t1")
    assert len(list((home / "exports").iterdir())) == 1
    with open_artifact(home, conn, receipt["artifact_id"], OWNER, "sales") as handle:
        assert handle.read() == b"published bytes"
    conn.close()
