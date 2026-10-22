import pytest
import yaml

import config
from conftest import TOKEN, ADMIN, GROUP


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: None)
    for key in ("BOT_TOKEN", "IT_GROUP_ID", "ADMIN_IDS", "IT_IDS", "DATABASE_URL", "DB_PASSWORD", "DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("IT_GROUP_ID", str(GROUP))
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN))
    monkeypatch.setenv("DB_PASSWORD", "synthetic:@/$#password")
    return monkeypatch


def test_password_not_embedded_in_url_and_repr(env):
    settings = config.Settings.from_env()
    assert settings.database["password"] == "synthetic:@/$#password"
    assert "dsn" not in settings.database
    assert settings.it == settings.admins
    assert "synthetic" not in repr(settings)
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize("key,value", [("BOT_TOKEN", "bad-secret"), ("IT_GROUP_ID", "1"),
    ("ADMIN_IDS", ""), ("ADMIN_IDS", "-4"), ("IT_IDS", "name"), ("DB_PORT", "0"),
    ("DB_PASSWORD", ""), ("DATABASE_URL", "mysql://secret")])
def test_bad_configuration(env, key, value):
    env.setenv(key, value)
    with pytest.raises(ValueError) as error:
        config.Settings.from_env()
    assert "bad-secret" not in str(error.value)
    assert "mysql://secret" not in str(error.value)


def test_compose_security_structure():
    data = yaml.safe_load((config.BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8"))
    db, bot = data["services"]["db"], data["services"]["bot"]
    assert "ports" not in db
    assert bot["depends_on"]["db"]["condition"] == "service_healthy"
    assert "DATABASE_URL" not in bot["environment"]
    assert "${DB_PASSWORD:?" in db["environment"]["POSTGRES_PASSWORD"]
    assert bot["read_only"]
    assert db["healthcheck"]["timeout"] == "3s"


async def test_startup_failure_closes_pool(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import bot

    @asynccontextmanager
    async def broken_acquire():
        raise RuntimeError("synthetic SQL secret")
        yield

    pool = SimpleNamespace(acquire=broken_acquire, close=AsyncMock())
    monkeypatch.setattr(bot.Settings, "from_env", lambda: SimpleNamespace(group_id=GROUP,
                        admins={ADMIN}, it={ADMIN}, database={}))
    monkeypatch.setattr(bot.asyncpg, "create_pool", AsyncMock(return_value=pool))
    with pytest.raises(RuntimeError):
        await bot.main()
    pool.close.assert_awaited_once()
