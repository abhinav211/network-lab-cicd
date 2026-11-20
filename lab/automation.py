import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import AggregatedResult, Result, Task
from nornir_napalm.plugins.tasks import napalm_configure, napalm_get

from lab.model import Host, load_hosts, read_yaml

LOG = logging.getLogger(__name__)


def inventory(root: Path, live: bool = False) -> Nornir:
    nr = InitNornir(
        inventory={
            "plugin": "SimpleInventory",
            "options": {
                "host_file": str(root / "inventory/hosts.yaml"),
                "group_file": str(root / "inventory/groups.yaml"),
                "defaults_file": str(root / "inventory/defaults.yaml"),
            },
        },
        runner={"plugin": "threaded", "options": {"num_workers": 4}},
        logging={"enabled": False},
    )
    if live:
        for key in ("LAB_USERNAME", "LAB_PASSWORD"):
            if not os.environ.get(key):
                raise ValueError(f"{key} must be provided in the environment")
        for host in nr.inventory.hosts.values():
            host.username = os.environ["LAB_USERNAME"]
            host.password = os.environ["LAB_PASSWORD"]
    return nr


def require_success(results: AggregatedResult) -> None:
    if results.failed:
        for name, result in results.items():
            if result.failed:
                for item in result:
                    if item.failed:
                        LOG.error("%s: %s: %s", name, item.name, item.exception or item.result)
        raise RuntimeError(f"{results.name}: failed on {', '.join(results.failed_hosts)}")


def build(root: Path, output: Path) -> None:
    hosts = load_hosts(root)
    output.mkdir(parents=True, exist_ok=True)
    environment = Environment(
        loader=FileSystemLoader(root / "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("eos.j2")

    def render(task: Task) -> Result:
        data = hosts[task.host.name].data
        config = template.render(**data.model_dump(mode="json"), router_id=str(data.loopback.ip))
        (output / f"{task.host.name}.cfg").write_text(config)
        return Result(host=task.host, changed=False)

    with inventory(root) as nr:
        require_success(nr.run(task=render))
    lines = [
        f"{hashlib.sha256((output / f'{name}.cfg').read_bytes()).hexdigest()}  {name}.cfg\n"
        for name in sorted(hosts)
    ]
    (output / "SHA256SUMS").write_text("".join(lines))


def check_artifacts(hosts: dict[str, Host], output: Path) -> None:
    expected = "".join(
        f"{hashlib.sha256((output / f'{name}.cfg').read_bytes()).hexdigest()}  {name}.cfg\n"
        for name in sorted(hosts)
    )
    if (output / "SHA256SUMS").read_text() != expected:
        raise ValueError("configuration artifact checksum mismatch; rebuild before deploying")


def prepare(root: Path, output: Path, name: str) -> None:
    if not re.fullmatch(r"netci-[0-9]+-[0-9]+", name):
        raise ValueError("lab name must be netci-<run-id>-<attempt>")
    hosts = load_hosts(root)
    check_artifacts(hosts, output)
    topology = read_yaml(root / "topology.clab.yml")
    topology["name"] = name
    topology["mgmt"]["network"] = name
    (output / "runtime.clab.yml").write_text(yaml.safe_dump(topology, sort_keys=False))


def retry(nr: Nornir, task: Callable[[Task], Result], timeout: int, interval: int = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        # Nornir otherwise skips hosts marked failed by the preceding attempt.
        nr.data.reset_failed_hosts()
        results = nr.run(task=task, name="readiness/convergence check")
        if not results.failed:
            return
        for name, result in results.items():
            if result.failed:
                LOG.warning("%s: %s not ready: %s", name, results.name, result[-1].exception)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            require_success(results)
        nr.close_connections(on_failed=True)
        time.sleep(min(interval, remaining))


def check_bgp(host: Host, observed: dict) -> None:
    peers = observed["global"]["peers"]
    expected = {str(peer.address): peer.remote_as for peer in host.data.bgp.neighbors}
    if set(peers) != set(expected):
        raise ValueError(f"BGP peer set differs: expected {set(expected)}, got {set(peers)}")
    for address, remote_as in expected.items():
        peer = peers[address]
        if not peer["is_up"] or not peer["is_enabled"]:
            raise ValueError(f"BGP peer {address} is not established/enabled")
        if peer["remote_as"] != remote_as or peer["local_as"] != host.data.bgp.asn:
            raise ValueError(f"BGP peer {address} has incorrect AS numbers")


def check_ping(reply: dict, destination: str) -> None:
    if "success" not in reply:
        raise ValueError(f"ping {destination} failed: {reply}")
    result = reply["success"]
    if result["probes_sent"] != 3 or result["packet_loss"] != 0:
        raise ValueError(f"ping {destination}: expected 3 probes with zero loss, got {result}")


def verify_host(task: Task, hosts: dict[str, Host]) -> Result:
    host = hosts[task.host.name]
    device = task.host.get_connection("napalm", task.nornir.config)
    check_bgp(host, device.get_bgp_neighbors())
    for name, remote in hosts.items():
        if name == task.host.name:
            continue
        prefix = str(remote.data.loopback.network)
        routes = device.get_route_to(destination=prefix, protocol="bgp")
        if not any(route["current_active"] for route in routes.get(prefix, [])):
            raise ValueError(f"{task.host.name}: missing active BGP route {prefix}")
        destination = str(remote.data.loopback.ip)
        check_ping(
            device.ping(destination, source=str(host.data.loopback.ip), count=3, timeout=2),
            destination,
        )
    LOG.info("%s: BGP peers, remote loopback routes and pings passed", task.host.name)
    return Result(host=task.host)


def live(root: Path, output: Path, command: str, timeout: int) -> None:
    hosts = load_hosts(root)
    if command == "configure":
        # Check every file before changing any device.
        check_artifacts(hosts, output)
    with inventory(root, live=True) as nr:
        if command == "wait":
            retry(nr, partial(napalm_get, getters=["facts"]), timeout)
        elif command == "configure":

            def configure(task: Task) -> Result:
                task.run(
                    task=napalm_configure,
                    filename=str(output / f"{task.host.name}.cfg"),
                    replace=False,
                    dry_run=False,
                )
                return Result(host=task.host)

            require_success(nr.run(task=configure))
        elif command == "verify":
            retry(nr, partial(verify_host, hosts=hosts), timeout)
        else:
            raise ValueError(f"unknown live command: {command}")
