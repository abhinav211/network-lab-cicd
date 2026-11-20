import copy
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from jinja2 import UndefinedError
from nornir.core.task import Result
from pydantic import ValidationError

from lab.automation import (
    build,
    check_artifacts,
    check_bgp,
    check_ping,
    inventory,
    live,
    prepare,
    require_success,
    retry,
    verify_host,
)
from lab.model import load_hosts, read_yaml, validate_topology

ROOT = Path(__file__).resolve().parents[1]


def test_build_real_inventory_and_prepare(tmp_path):
    hosts = load_hosts(ROOT)
    build(ROOT, tmp_path)
    check_artifacts(hosts, tmp_path)
    assert {path.stem for path in tmp_path.glob("*.cfg")} == set(hosts)
    r1 = (tmp_path / "r1.cfg").read_text()
    assert "neighbor 10.10.13.1 remote-as 65003" in r1
    assert "network 10.255.0.1/32" in r1
    assert "router-id 10.255.0.1" in r1
    assert (
        "ip prefix-list R1-TO-R3 seq 10 permit 10.255.0.1/32\n"
        "ip prefix-list R1-TO-R3 seq 20 permit 10.100.1.0/24\n"
    ) in r1
    assert r1.index("ip prefix-list") < r1.index("router bgp")
    assert " neighbor 10.10.13.1 prefix-list R1-TO-R3 out\n" in r1
    assert "network 10.100.1.0/24" not in r1
    for name in ("r2", "r3", "r4"):
        assert "prefix-list" not in (tmp_path / f"{name}.cfg").read_text()
    loopback_stanza = r1.split("interface Loopback0")[1].split("!")[0]
    assert "no switchport" not in loopback_stanza
    first_build = (tmp_path / "SHA256SUMS").read_text()
    build(ROOT, tmp_path)
    assert (tmp_path / "SHA256SUMS").read_text() == first_build
    prepare(ROOT, tmp_path, "netci-123-1")
    topology = read_yaml(tmp_path / "runtime.clab.yml")
    assert topology["name"] == topology["mgmt"]["network"] == "netci-123-1"


@pytest.fixture
def policy_project(tmp_path):
    for directory in ("inventory", "templates"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    shutil.copyfile(ROOT / "topology.clab.yml", tmp_path / "topology.clab.yml")
    return tmp_path


def test_prefix_list_deny_and_sequence_order(policy_project):
    path = policy_project / "inventory/hosts.yaml"
    hosts = read_yaml(path)
    entries = hosts["r1"]["data"]["prefix_lists"][0]["entries"]
    entries[1]["action"] = "deny"
    entries.reverse()
    path.write_text(yaml.safe_dump(hosts))
    build(policy_project, policy_project / "build")
    config = (policy_project / "build/r1.cfg").read_text()
    assert (
        "ip prefix-list R1-TO-R3 seq 10 permit 10.255.0.1/32\n"
        "ip prefix-list R1-TO-R3 seq 20 deny 10.100.1.0/24\n"
    ) in config


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("duplicate-name", "duplicate prefix-list name"),
        ("duplicate-sequence", "duplicate prefix-list sequence"),
        ("missing-list", "undefined outbound prefix-list"),
        ("host-bits", "IPv4 network"),
        ("invalid-action", "permit"),
        ("unsafe-name", "pattern"),
        ("invalid-sequence", "greater than 0"),
    ],
)
def test_invalid_prefix_lists_fail_before_rendering(policy_project, failure, message):
    path = policy_project / "inventory/hosts.yaml"
    hosts = read_yaml(path)
    data = hosts["r1"]["data"]
    policy = data["prefix_lists"][0]
    if failure == "duplicate-name":
        data["prefix_lists"].append(copy.deepcopy(policy))
    elif failure == "duplicate-sequence":
        policy["entries"][1]["sequence"] = policy["entries"][0]["sequence"]
    elif failure == "missing-list":
        data["bgp"]["neighbors"][0]["prefix_list_out"] = "UNDEFINED"
    elif failure == "host-bits":
        policy["entries"][1]["prefix"] = "10.100.1.1/24"
    elif failure == "invalid-action":
        policy["entries"][1]["action"] = "allow"
    elif failure == "unsafe-name":
        policy["name"] = "R1-TO-R3\nip routing"
    else:
        policy["entries"][1]["sequence"] = 0
    path.write_text(yaml.safe_dump(hosts))
    output = policy_project / "build"
    with pytest.raises(ValidationError, match=message):
        build(policy_project, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "failure", ["asn", "duplicate-ip", "missing-link", "management", "endpoint"]
)
def test_invalid_inventory_topology_fails(failure):
    hosts = load_hosts(ROOT)
    topology = read_yaml(ROOT / "topology.clab.yml")
    if failure == "asn":
        hosts["r1"].data.bgp.neighbors[0].remote_as = 12345
    elif failure == "duplicate-ip":
        hosts["r1"].data.loopback = hosts["r2"].data.loopback
    elif failure == "missing-link":
        topology["topology"]["links"].pop()
    elif failure == "management":
        topology["topology"]["nodes"]["r1"]["mgmt-ipv4"] = "172.31.250.99"
    else:
        topology["topology"]["links"][0]["endpoints"][0] = "r1:eth9"
    with pytest.raises(ValueError):
        validate_topology(hosts, topology)


def test_artifact_corruption_fails_before_connecting(tmp_path, monkeypatch):
    build(ROOT, tmp_path)
    (tmp_path / "r4.cfg").write_text("corrupted")
    connection = Mock()
    monkeypatch.setattr("lab.automation.inventory", connection)
    with pytest.raises(ValueError, match="checksum"):
        live(ROOT, tmp_path, "configure", 1)
    connection.assert_not_called()


def test_strict_template_reports_failure(tmp_path, monkeypatch):
    template = Mock()
    template.render.side_effect = UndefinedError("missing variable")
    environment = Mock()
    environment.get_template.return_value = template
    monkeypatch.setattr("lab.automation.Environment", Mock(return_value=environment))
    with pytest.raises(RuntimeError, match="failed on"):
        build(ROOT, tmp_path)
    assert not (tmp_path / "SHA256SUMS").exists()


def observed_bgp():
    return {
        "global": {
            "peers": {
                "10.10.13.1": {
                    "is_up": True,
                    "is_enabled": True,
                    "remote_as": 65003,
                    "local_as": 65001,
                }
            }
        }
    }


@pytest.mark.parametrize("failure", ["down", "missing", "wrong-as", "extra", "disabled"])
def test_bgp_must_match_inventory(failure):
    host = load_hosts(ROOT)["r1"]
    observed = observed_bgp()
    check_bgp(host, observed)
    peers = observed["global"]["peers"]
    if failure == "down":
        peers["10.10.13.1"]["is_up"] = False
    elif failure == "missing":
        peers.clear()
    elif failure == "wrong-as":
        peers["10.10.13.1"]["remote_as"] = 65099
    elif failure == "extra":
        peers["10.10.99.1"] = copy.deepcopy(peers["10.10.13.1"])
    else:
        peers["10.10.13.1"]["is_enabled"] = False
    with pytest.raises(ValueError):
        check_bgp(host, observed)


@pytest.mark.parametrize(
    "reply",
    [
        {"error": "unreachable"},
        {"success": {"probes_sent": 3, "packet_loss": 1}},
        {"success": {"probes_sent": 0, "packet_loss": 0}},
    ],
)
def test_ping_rejects_errors_loss_and_no_probes(reply):
    check_ping({"success": {"probes_sent": 3, "packet_loss": 0}}, "10.255.0.2")
    with pytest.raises(ValueError):
        check_ping(reply, "10.255.0.2")


def test_verification_checks_routes_and_source_loopback():
    hosts = load_hosts(ROOT)
    device = Mock()
    device.get_bgp_neighbors.return_value = observed_bgp()
    device.get_route_to.side_effect = lambda destination, protocol: {
        destination: [{"current_active": True}]
    }
    device.ping.return_value = {"success": {"probes_sent": 3, "packet_loss": 0}}
    task = Mock()
    task.host.name = "r1"
    task.host.get_connection.return_value = device
    verify_host(task, hosts)
    assert device.ping.call_count == 3
    device.ping.assert_any_call("10.255.0.4", source="10.255.0.1", count=3, timeout=2)
    device.get_route_to.return_value = {}
    device.get_route_to.side_effect = None
    with pytest.raises(ValueError, match="missing active BGP route"):
        verify_host(task, hosts)


def test_retry_retries_failed_hosts_and_times_out(monkeypatch):
    monkeypatch.setattr("lab.automation.time.sleep", lambda delay: None)
    attempts = {}

    def transient(task):
        attempts[task.host.name] = attempts.get(task.host.name, 0) + 1
        if attempts[task.host.name] == 1:
            raise ConnectionError("booting")
        return Result(host=task.host)

    with inventory(ROOT) as nr:
        retry(nr, transient, timeout=10)
        assert attempts == {name: 2 for name in nr.inventory.hosts}

    def broken(task):
        raise ConnectionError("unavailable")

    with inventory(ROOT) as nr:
        clock = iter([0, 11])
        monkeypatch.setattr("lab.automation.time.monotonic", lambda: next(clock))
        with pytest.raises(RuntimeError, match="failed on"):
            retry(nr, broken, timeout=10)


def test_configure_errors_fail_the_run(tmp_path, monkeypatch):
    build(ROOT, tmp_path)
    monkeypatch.setenv("LAB_USERNAME", "test-user")
    monkeypatch.setenv("LAB_PASSWORD", "test-value")

    def reject(task, **kwargs):
        assert kwargs["replace"] is False
        assert kwargs["dry_run"] is False
        raise RuntimeError("candidate rejected")

    monkeypatch.setattr("lab.automation.napalm_configure", reject)
    with pytest.raises(RuntimeError, match="failed on"):
        live(ROOT, tmp_path, "configure", 1)


def test_live_requires_credentials(monkeypatch):
    monkeypatch.delenv("LAB_PASSWORD", raising=False)
    monkeypatch.delenv("LAB_USERNAME", raising=False)
    with pytest.raises(ValueError, match="LAB_USERNAME"):
        inventory(ROOT, live=True)


@pytest.mark.parametrize("command", ["wait", "verify"])
def test_live_polling_invokes_all_hosts(command, tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_USERNAME", "test-user")
    monkeypatch.setenv("LAB_PASSWORD", "test-value")
    seen = set()

    def check(task, **kwargs):
        seen.add(task.host.name)
        if command == "wait":
            assert kwargs == {"getters": ["facts"]}
        else:
            assert set(kwargs["hosts"]) == set(load_hosts(ROOT))
        return Result(host=task.host)

    monkeypatch.setattr(
        f"lab.automation.{'napalm_get' if command == 'wait' else 'verify_host'}", check
    )
    live(ROOT, tmp_path, command, 1)
    assert seen == set(load_hosts(ROOT))


def test_task_failures_are_not_success():
    def broken(task):
        raise ValueError("invalid candidate")

    with inventory(ROOT) as nr:
        with pytest.raises(RuntimeError, match="failed on"):
            require_success(nr.run(task=broken))
