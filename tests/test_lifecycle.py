import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("failure", "returncode", "destroyed"),
    [
        ("", 0, True),
        ("deploy", 1, True),
        ("configure", 1, True),
        ("verify", 1, True),
        ("destroy", 1, True),
        ("collision", 1, False),
    ],
)
def test_lifecycle_cleanup_and_failure_status(tmp_path, failure, returncode, destroyed):
    """Exercise real lifecycle scripts without root, Docker or network devices."""
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    tools = tmp_path / "bin"
    tools.mkdir()
    commands = {
        "python": """
if [[ "$*" == *prepare* ]]; then
  mkdir -p build
  printf 'name: %s\\n' "$LAB_NAME" > build/runtime.clab.yml
fi
if [[ "$*" == *"$FAILURE"* && -n "$FAILURE" ]]; then
  exit 1
fi
""",
        "sudo": """
[[ "$1" == "-A" ]] || exit 2
shift
exec "$@"
""",
        "docker": """
if [[ "$1 ${2:-}" == "network inspect" ]]; then
  [[ -f build/resource || "$FAILURE" == "collision" ]]
elif [[ "$1" == "ps" ]]; then
  if [[ -f build/resource ]]; then echo fake-container; fi
else
  exit 0
fi
""",
        "containerlab": """
echo "$1" >> events
if [[ "$1" == "deploy" ]]; then
  touch build/resource
elif [[ "$1" == "destroy" ]]; then
  if [[ "$FAILURE" == "destroy" ]]; then exit 1; fi
  rm -f build/resource
fi
if [[ "$1" == "$FAILURE" ]]; then exit 1; fi
""",
    }
    for name, body in commands.items():
        executable = tools / name
        executable.write_text("#!/bin/bash\nset -eu\n" + body + "\nexit 0\n")
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "LAB_NAME": "netci-123-1",
        "LAB_USERNAME": "test-user",
        "LAB_PASSWORD": "test-value",
        "FAILURE": failure,
    }
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/run-lab.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == returncode, result.stdout + result.stderr
    events = (tmp_path / "events").read_text().splitlines()
    assert ("destroy" in events) is destroyed
    if failure != "destroy":
        assert not (tmp_path / "build/resource").exists()
        assert not (tmp_path / "build/lab-owned").exists()
    else:
        assert (tmp_path / "build/lab-owned").exists()
        assert "Cleanup failed" in result.stderr
