import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from nornir.core.inventory import Host

from lab.automation import build
from lab.incremental import incremental, make_plan
from lab.model import load_hosts

ROOT = Path(__file__).resolve().parents[1]
DESIRED = """ip routing
interface Loopback0
 description router-id
 ip address 10.255.0.1/32
!
interface Ethernet1
 description -to-r3
 no switchport
 ip address 10.10.13.0/31
 no shutdown
!
router bgp 65001
 router-id 10.255.0.1
 neighbor 10.10.13.1 remote-as 65003
 network 10.255.0.1/32
!
"""


def running(desired):
    # EOS emits different indentation and includes defaults with "show run all".
    return desired.replace("\n ", "\n   ") + "end\n"


def test_description_delta_contains_no_other_configuration():
    current = running(DESIRED.replace("description -to-r3", "description link-to-r3"))
    current += "! irrelevant comment\n"
    plan = make_plan("172.31.250.11", current, DESIRED)
    assert plan.commands == "interface Ethernet1\n description -to-r3\nexit\n"
    assert "description link-to-r3" in plan.current_managed
    assert "description -to-r3" in plan.desired
    assert make_plan("172.31.250.11", running(DESIRED), DESIRED).commands == ""


def test_missing_fields_and_shutdown_are_not_treated_as_equal():
    current = running(
        DESIRED.replace(" no shutdown", " shutdown").replace(" router-id 10.255.0.1\n", "")
    )
    assert make_plan("172.31.250.11", current, DESIRED).commands == (
        "interface Ethernet1\n no shutdown\nexit\nrouter bgp 65001\n router-id 10.255.0.1\nexit\n"
    )


def test_unmanaged_configuration_is_preserved_and_not_in_snapshot():
    extra = """username operator secret NOT-FOR-ARTIFACTS
interface Management0
   ip address 172.31.250.11/24
interface Ethernet9
   description manual-link
ip prefix-list MANUAL seq 10 permit 192.0.2.0/24
"""
    plan = make_plan("172.31.250.11", extra + running(DESIRED), DESIRED)
    assert plan.commands == ""
    assert "NOT-FOR-ARTIFACTS" not in plan.model_dump_json()
    assert "MANUAL" not in plan.model_dump_json()
    assert "Management0" not in plan.model_dump_json()


def test_rule_replacement_removes_only_the_changed_sequence():
    rule = "ip prefix-list TEST seq 10 permit 192.0.2.0/24\n"
    current = running(DESIRED + rule + "ip prefix-list TEST seq 20 permit 198.51.100.0/24\n")
    desired = DESIRED + rule.replace("permit", "deny")
    assert make_plan("172.31.250.11", current, desired).commands == (
        "no ip prefix-list TEST seq 10\nip prefix-list TEST seq 10 deny 192.0.2.0/24\n"
    )


def test_ipv4_neighbor_filter_display_context_is_normalized():
    desired = DESIRED.replace(
        " network 10.255.0.1/32",
        " neighbor 10.10.13.1 prefix-list TEST out\n network 10.255.0.1/32",
    )
    current = running(
        DESIRED.replace(
            " network 10.255.0.1/32",
            " network 10.255.0.1/32\n address-family ipv4\n"
            "    neighbor 10.10.13.1 prefix-list TEST out",
        )
    )
    assert make_plan("172.31.250.11", current, desired).commands == ""
    # An IPv6 attachment must not count as a matching IPv4 attachment.
    assert (
        "neighbor 10.10.13.1 prefix-list TEST out"
        in make_plan(
            "172.31.250.11", current.replace("address-family ipv4", "address-family ipv6"), desired
        ).commands
    )


@pytest.mark.parametrize("current", ["", "% CLI error", "interface Ethernet1\n description test"])
def test_incomplete_running_state_fails_closed(current):
    with pytest.raises(ValueError, match="Incomplete"):
        make_plan("172.31.250.11", current, DESIRED)


def test_bgp_process_changes_require_explicit_migration():
    with pytest.raises(ValueError, match="explicit migration"):
        make_plan("172.31.250.11", running(DESIRED.replace("65001", "65100")), DESIRED)


def test_unsupported_desired_commands_fail_instead_of_sending_full_config():
    with pytest.raises(ValueError, match="Unsupported"):
        make_plan(
            "172.31.250.11",
            running(DESIRED),
            DESIRED + "service routing protocols model multi-agent\n",
        )


@pytest.fixture
def devices(tmp_path, monkeypatch):
    build(ROOT, tmp_path)
    monkeypatch.setenv("LAB_USERNAME", "test-user")
    monkeypatch.setenv("LAB_PASSWORD", "test-value")
    drivers = {}
    states = {}
    for name in load_hosts(ROOT):
        desired = (tmp_path / f"{name}.cfg").read_text()
        states[name] = running(desired)
        driver = Mock()
        driver.get_config.side_effect = lambda _name=name, **kwargs: {"running": states[_name]}
        driver.compare_config.return_value = "candidate diff"
        driver.commit_config.side_effect = lambda _name=name, _desired=desired: states.update(
            {_name: running(_desired)}
        )
        driver.cli.return_value = {
            "copy running-config startup-config": "Copy completed successfully.\n"
        }
        drivers[name] = driver
    # Force exactly one live difference regardless of the user's current description.
    wanted = load_hosts(ROOT)["r1"].data.interfaces[0].description
    states["r1"] = states["r1"].replace(f"description {wanted}", "description OLD-DESCRIPTION")
    monkeypatch.setattr(Host, "get_connection", lambda host, *args: drivers[host.name])
    return tmp_path, drivers, states


def test_plan_is_read_only_apply_sends_delta_and_save_skips_unchanged(devices):
    output, drivers, states = devices
    incremental(ROOT, output, "plan")
    for driver in drivers.values():
        driver.load_merge_candidate.assert_not_called()
        driver.commit_config.assert_not_called()
        driver.get_config.assert_called_once_with(retrieve="running", full=True, sanitized=True)
    incremental(ROOT, output, "apply-diff")
    wanted = load_hosts(ROOT)["r1"].data.interfaces[0].description
    drivers["r1"].load_merge_candidate.assert_called_once_with(
        filename=None, config=f"interface Ethernet1\n description {wanted}\nexit\n"
    )
    drivers["r1"].commit_config.assert_called_once()
    incremental(ROOT, output, "save-diff")
    drivers["r1"].cli.assert_called_once()
    for name in ("r2", "r3", "r4"):
        drivers[name].load_merge_candidate.assert_not_called()
        drivers[name].commit_config.assert_not_called()
        drivers[name].cli.assert_not_called()
    incremental(ROOT, output, "plan")
    plans = json.loads((output / "incremental-plan.json").read_text())
    assert all(not plan["commands"] for plan in plans.values())


@pytest.mark.parametrize("failure", ["drift", "tampered-plan", "read-error", "changed-artifact"])
def test_preflight_failure_prevents_all_writes(devices, failure):
    output, drivers, states = devices
    incremental(ROOT, output, "plan")
    if failure == "drift":
        states["r4"] += "hostname changed-outside-pipeline\n"
    elif failure == "tampered-plan":
        path = output / "incremental-plan.json"
        plans = json.loads(path.read_text())
        plans["r4"]["commands"] = "hostname malicious\n"
        path.write_text(json.dumps(plans))
    elif failure == "read-error":
        drivers["r4"].get_config.side_effect = ConnectionError("connection lost")
    else:
        (output / "r4.cfg").write_text("changed")
    with pytest.raises((RuntimeError, ValueError)):
        incremental(ROOT, output, "apply-diff")
    for driver in drivers.values():
        driver.load_merge_candidate.assert_not_called()
        driver.commit_config.assert_not_called()


def test_noop_plan_causes_no_configuration_sessions(devices):
    output, drivers, states = devices
    states["r1"] = running((output / "r1.cfg").read_text())
    incremental(ROOT, output, "plan")
    incremental(ROOT, output, "apply-diff")
    incremental(ROOT, output, "save-diff")
    for driver in drivers.values():
        driver.load_merge_candidate.assert_not_called()
        driver.cli.assert_not_called()


@pytest.mark.parametrize("failure", ["candidate-error", "unchanged-after-commit"])
def test_apply_failure_is_not_reported_as_success(devices, failure):
    output, drivers, states = devices
    incremental(ROOT, output, "plan")
    if failure == "candidate-error":
        drivers["r1"].load_merge_candidate.side_effect = RuntimeError("invalid candidate")
    else:
        drivers["r1"].commit_config.side_effect = None
    with pytest.raises(RuntimeError, match="failed on r1"):
        incremental(ROOT, output, "apply-diff")
