"""Fail closed before deploying a workflow_run artifact to persistent routers."""

import json
import logging
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def check_source(run: dict, repository: str, latest_sha: str) -> None:
    if (
        run.get("event") != "push"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != "main"
        or (run.get("head_repository") or {}).get("full_name") != repository
    ):
        raise ValueError("Deployment requires a successful main-branch push build from this repo")
    if not latest_sha or run.get("head_sha") != latest_sha:
        raise ValueError("Build is no longer the main branch tip; approve the newer tested build")


def check_reviewers(environment: dict) -> None:
    rules = environment.get("protection_rules", [])
    if not any(
        rule.get("type") == "required_reviewers" and rule.get("reviewers") for rule in rules
    ):
        raise ValueError(
            "Configure Required reviewers on the existing-lab GitHub environment before deploying"
        )


def github_get(path: str, token: str) -> dict:
    request = Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(
            f"GitHub API HTTP {error.code} for {path}; check repository access and "
            "existing-lab environment setup"
        ) from error
    if not isinstance(result, dict):
        raise ValueError(f"Unexpected GitHub API response for {path}")
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    if not token:
        raise ValueError("GITHUB_TOKEN must be provided")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    base = f"/repos/{quote(repository, safe='/')}"
    latest = github_get(f"{base}/commits/main", token)
    check_source(event["workflow_run"], repository, latest["sha"])
    environment = github_get(f"{base}/environments/existing-lab", token)
    check_reviewers(environment)
    logging.info("Current main build and required-reviewer configuration confirmed")


if __name__ == "__main__":
    main()
