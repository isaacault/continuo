"""Every migrations directory ships in the package: an image built with `pip install .` would
otherwise start without a part's tables (the video part's 1001+ were left out)."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_migrations_directory_is_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
    for d in sorted((ROOT / "continuo").glob("**/migrations")):
        if not any(d.glob("*.sql")):
            continue
        package = ".".join(d.parent.relative_to(ROOT).parts)
        assert "migrations/*.sql" in data.get(package, []), f"{package}: migrations not shipped"
