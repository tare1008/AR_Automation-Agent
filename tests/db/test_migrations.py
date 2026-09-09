import subprocess


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True, text=True,
    )


def test_migrations_upgrade_and_downgrade(_embedded_pg):
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    again = _alembic("upgrade", "head")
    assert again.returncode == 0, again.stderr
