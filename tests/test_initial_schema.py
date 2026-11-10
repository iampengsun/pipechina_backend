"""The initial revision must build a database without relying on create_all."""

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command


def test_initial_schema_upgrade_downgrade_and_repeat(settings, monkeypatch):
    monkeypatch.setattr("app.bootstrap.config.get_settings", lambda: settings)
    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0003_station_locations"]
    assert len(list(scripts.walk_revisions())) == 3
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)
