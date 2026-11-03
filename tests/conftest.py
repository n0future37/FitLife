import pytest


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", help="仅测试库真实MySQL/MCP集中回归")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="添加--integration启用测试库验收")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_database():
    from fitlife.database import DatabaseSettings
    from fitlife.manage import migrate

    settings = DatabaseSettings(environment="test")
    engine = settings.engine()
    migrate(engine, "test")
    yield settings, engine
    engine.dispose()
