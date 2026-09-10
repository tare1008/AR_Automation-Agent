from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from ar_pipeline import cli
from ar_pipeline.storage import LocalBlobStore

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emails"


@pytest.fixture
def wired(db_session, tmp_path, monkeypatch):
    """Point cli's get_session / get_blob_store at the test session + a tmp blob store."""

    @contextmanager
    def fake_get_session():
        yield db_session

    store = LocalBlobStore(str(tmp_path))
    monkeypatch.setattr("ar_pipeline.db.base.get_session", fake_get_session)
    monkeypatch.setattr("ar_pipeline.storage.get_blob_store", lambda: store)
    return db_session


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_parser_accepts_the_three_commands():
    p = cli.build_parser()
    assert p.parse_args(["ingest-eml", "a.eml", "b.eml"]).paths == ["a.eml", "b.eml"]
    assert p.parse_args(["tick", "--repeat", "3"]).repeat == 3
    assert p.parse_args(["status"]).command == "status"


def test_ingest_eml_command_seeds_emails(wired, capsys):
    rc = cli.main(["ingest-eml", str(_FIXTURES / "02_fwd_body_table.eml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 ingested, 0 already present, 0 errored" in out

    rc = cli.main(["ingest-eml", str(_FIXTURES / "02_fwd_body_table.eml")])
    assert rc == 0
    assert "0 ingested, 1 already present, 0 errored" in capsys.readouterr().out


def test_ingest_eml_missing_file_errors_but_processes_the_rest(wired, capsys):
    rc = cli.main(["ingest-eml", "/no/such/file.eml", str(_FIXTURES / "06_direct_excel.eml")])
    assert rc == 1  # non-zero because one path was bad
    captured = capsys.readouterr()
    assert "not a file" in captured.err
    assert "1 ingested, 0 already present, 1 errored" in captured.out


def test_ingest_eml_isolates_a_raising_file_from_the_batch(wired, capsys, monkeypatch):
    from sqlalchemy import func, select

    from ar_pipeline.db.models import Email
    from ar_pipeline.ingest import eml as eml_mod

    real = eml_mod.ingest_eml_file
    calls = {"n": 0}

    def flaky(session, blob_store, path):
        calls["n"] += 1
        if calls["n"] == 1:
            session.add(
                Email(  # dirty the session, then blow up
                    internet_message_id="<partial@x>",
                    sender_address="a@b.com",
                    sender_domain="b.com",
                    subject="partial",
                    received_at=_now(),
                    status="new",
                )
            )
            session.flush()
            raise RuntimeError("blob write failed")
        return real(session, blob_store, path)

    monkeypatch.setattr(eml_mod, "ingest_eml_file", flaky)
    rc = cli.main(
        [
            "ingest-eml",
            str(_FIXTURES / "02_fwd_body_table.eml"),
            str(_FIXTURES / "06_direct_excel.eml"),
        ]
    )
    assert rc == 1
    assert "1 ingested, 0 already present, 1 errored" in capsys.readouterr().out

    subjects = set(wired.scalars(select(Email.subject)).all())
    assert "partial" not in subjects  # the raising file's half-write rolled back
    assert wired.scalar(select(func.count()).select_from(Email)) == 1


def _now():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


def test_status_command_reports_counts(wired, capsys):
    cli.main(["ingest-eml", str(_FIXTURES / "02_fwd_body_table.eml")])
    capsys.readouterr()
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "emails" in out and "new" in out


def test_tick_command_runs_without_backend(wired, capsys, monkeypatch):
    from ar_pipeline.pipeline.advance import AdvanceStats

    monkeypatch.setattr(
        "ar_pipeline.pipeline.advance.advance_once",
        lambda *a, **k: AdvanceStats(classified=1),
    )
    monkeypatch.setattr("ar_pipeline.extract.vision.get_vision_extractor", lambda: object())
    monkeypatch.setattr("ar_pipeline.normalize.llm_client.get_llm_client", lambda: object())
    monkeypatch.setattr(
        "ar_pipeline.config.get_settings", lambda: type("S", (), {"backend_url": ""})()
    )
    rc = cli.main(["tick", "--repeat", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tick 1/2" in out and "tick 2/2" in out
    assert "deliver: skipped" in out


def test_tick_rejects_bad_repeat(capsys):
    assert cli.main(["tick", "--repeat", "0"]) == 2
    assert "--repeat must be >= 1" in capsys.readouterr().err
