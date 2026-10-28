"""Tests for `./run check-fixtures`.

Planted values are assembled from fragments so this file does not trip the
check it tests.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SCRIPT = Path(__file__).parent.parent / "run"
DENYLISTED = "zebra-" + "quokka-7"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="check-fixtures needs git and bash",
)


def check_fixtures(
    tmp_path: Path, fixture: str, denylist: str | None = None
) -> subprocess.CompletedProcess[str]:
    shutil.copy(RUN_SCRIPT, tmp_path / "run")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.txt").write_text(fixture + "\n")
    env = {k: v for k, v in os.environ.items() if k != "FIXTURE_DENYLIST"}
    if denylist is not None:
        env["FIXTURE_DENYLIST"] = denylist
    return subprocess.run(
        ["bash", "run", "check-fixtures"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "planted",
    [
        "Author" + "ization: Bearer abc123",
        "X-Shopify-" + "Access-Token: abc123",
        '{"x-shopify-' + 'access-token": "abc123"}',
        "token = " + "shp" + "at_0123456789abcdef",
        "token = " + "shp" + "ss_0123456789abcdef",
        "https://acme-shop" + ".myshopify.com/admin",
    ],
)
def test_planted_value_fails_without_being_printed(
    tmp_path: Path, planted: str
) -> None:
    result = check_fixtures(tmp_path, planted)

    assert result.returncode != 0
    assert planted not in result.stdout + result.stderr
    assert "tests/fixture.txt:1" in result.stdout


def test_denylisted_string_fails_without_being_printed(tmp_path: Path) -> None:
    result = check_fixtures(tmp_path, f"name = {DENYLISTED}", denylist=DENYLISTED)

    assert result.returncode != 0
    assert DENYLISTED not in result.stdout + result.stderr
    assert "tests/fixture.txt:1" in result.stdout


@pytest.mark.parametrize("host", ["example.myshopify.com", "dev.example.myshopify.com"])
def test_placeholder_shop_host_passes(tmp_path: Path, host: str) -> None:
    result = check_fixtures(tmp_path, f"https://{host}/admin", denylist=DENYLISTED)

    assert result.returncode == 0, result.stdout + result.stderr
