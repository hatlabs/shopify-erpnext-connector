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


@pytest.mark.parametrize(("in_actions", "annotated"), [(True, True), (False, False)])
def test_unset_denylist_is_annotated_only_in_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_actions: bool, annotated: bool
) -> None:
    if in_actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    result = check_fixtures(tmp_path, "name = placeholder")

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("::warning::" in result.stdout) == annotated


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, content: str, message: str = "change") -> str:
    (repo / "file.txt").write_text(content + "\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    shutil.copy(RUN_SCRIPT, tmp_path / "run")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "run")
    commit(tmp_path, "base")
    return tmp_path


def check_history(
    repo: Path, base: str, denylist: str | None = DENYLISTED
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "FIXTURE_DENYLIST"}
    if denylist is not None:
        env["FIXTURE_DENYLIST"] = denylist
    return subprocess.run(
        ["bash", "run", "check-history", base],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_string_added_then_removed_is_found_in_history(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    added = commit(repo, f"name = {DENYLISTED.upper()}")
    commit(repo, "name = placeholder")

    result = check_history(repo, base)

    assert result.returncode != 0
    assert added in result.stdout
    assert DENYLISTED.upper() not in result.stdout + result.stderr


def test_string_in_a_commit_message_is_found(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    sha = commit(repo, "name = placeholder", message=f"use {DENYLISTED}")

    result = check_history(repo, base)

    assert result.returncode != 0
    assert sha in result.stdout
    assert DENYLISTED not in result.stdout + result.stderr


def test_clean_history_passes(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "name = placeholder")

    result = check_history(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("denylist", [None, "", " \n"])
def test_history_check_refuses_to_run_without_a_denylist(
    repo: Path, denylist: str | None
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "name = placeholder")

    result = check_history(repo, base, denylist=denylist)

    assert result.returncode != 0
    assert "FIXTURE_DENYLIST" in result.stdout + result.stderr
