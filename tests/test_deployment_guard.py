import json
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from lab.deployment_guard import check_reviewers, check_source, github_get, main

REPOSITORY = "example/network-lab"
SHA = "a" * 40


@pytest.fixture
def successful_run():
    return {
        "event": "push",
        "conclusion": "success",
        "head_branch": "main",
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": SHA,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("event", "workflow_dispatch"),
        ("conclusion", "failure"),
        ("head_branch", "feature"),
        ("head_repository", {"full_name": "another/repository"}),
    ],
)
def test_rejects_untrusted_or_failed_build(successful_run, field, value):
    successful_run[field] = value
    with pytest.raises(ValueError, match="successful main-branch push"):
        check_source(successful_run, REPOSITORY, SHA)


def test_rejects_build_superseded_while_waiting_for_approval(successful_run):
    with pytest.raises(ValueError, match="no longer the main branch tip"):
        check_source(successful_run, REPOSITORY, "b" * 40)


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"protection_rules": []},
        {"protection_rules": [{"type": "wait_timer", "wait_timer": 5}]},
        {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]},
    ],
)
def test_requires_actual_reviewers_not_just_environment_name(environment):
    with pytest.raises(ValueError, match="Configure Required reviewers"):
        check_reviewers(environment)


@pytest.mark.parametrize("status", [403, 404])
def test_guard_fails_closed_on_github_access_or_setup_errors(monkeypatch, status):
    path = f"/repos/{REPOSITORY}/environments/existing-lab"
    error = HTTPError(f"https://api.github.com{path}", status, "unavailable", {}, None)
    monkeypatch.setattr("lab.deployment_guard.urlopen", Mock(side_effect=error))
    with pytest.raises(RuntimeError, match=f"GitHub API HTTP {status}"):
        github_get(path, "test-token")


def test_guard_reads_exact_repo_main_and_approval_environment(
    tmp_path, monkeypatch, successful_run
):
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"workflow_run": successful_run}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    api = Mock(
        side_effect=[
            {"sha": SHA},
            {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}]},
        ]
    )
    monkeypatch.setattr("lab.deployment_guard.github_get", api)
    main()
    assert [call.args[0] for call in api.call_args_list] == [
        f"/repos/{REPOSITORY}/commits/main",
        f"/repos/{REPOSITORY}/environments/existing-lab",
    ]
