"""Executable clean-architecture boundaries for the backend."""

from pathlib import Path


APP = Path(__file__).parents[1] / "app"


def test_api_modules_remain_bounded() -> None:
    oversized = {
        module.name: len(module.read_text().splitlines())
        for module in (APP / "api").glob("*.py")
        if len(module.read_text().splitlines()) > 550
    }
    assert oversized == {}


def test_domain_records_are_framework_and_storage_agnostic() -> None:
    for module in (
        APP / "conversations" / "models.py",
        APP / "integrations" / "atlassian" / "models.py",
    ):
        source = module.read_text()
        assert "fastapi" not in source
        assert "pymongo" not in source
        assert "httpx" not in source


def test_application_layer_does_not_depend_on_api_routes() -> None:
    for module in (APP / "application").rglob("*.py"):
        assert "app.api" not in module.read_text()
