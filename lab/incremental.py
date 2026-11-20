"""Plan and apply only differences in template-managed EOS configuration."""

import hashlib
import json
import logging
import re
from pathlib import Path

from netutils.config.parser import EOSConfigParser
from nornir.core.task import Result, Task
from nornir_napalm.plugins.tasks import napalm_configure
from pydantic import BaseModel, ConfigDict, TypeAdapter

from lab.automation import check_artifacts, inventory, require_success, save_host
from lab.model import load_hosts

LOG = logging.getLogger(__name__)
FieldKey = tuple[tuple[str, ...], str]


class DevicePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str
    running_sha256: str
    desired_sha256: str
    current_managed: str
    desired: str
    commands: str


def field_key(parents: tuple[str, ...], command: str) -> FieldKey | None:
    if not parents:
        if command in ("ip routing", "no ip routing"):
            return parents, "ip routing"
        prefix = re.fullmatch(r"(ip prefix-list \S+ seq \d+) (permit|deny) \S+", command)
        if prefix:
            return parents, prefix[1]
    elif len(parents) == 1:
        if re.fullmatch(r"interface (Ethernet[1-9]\d*|Loopback0)", parents[0]):
            if command.startswith("description "):
                return parents, "description"
            if re.fullmatch(r"ip address \d+\.\d+\.\d+\.\d+/\d+", command):
                return parents, "ip address"
            if command in ("switchport", "no switchport"):
                return parents, "switchport"
            if command in ("shutdown", "no shutdown"):
                return parents, "shutdown"
        elif re.fullmatch(r"router bgp \d+", parents[0]):
            if command.startswith("router-id "):
                return parents, "router-id"
            neighbor = re.fullmatch(r"(neighbor \S+) (remote-as \d+|prefix-list \S+ out)", command)
            if neighbor:
                attribute = "remote-as" if " remote-as " in command else "prefix-list out"
                return parents, f"{neighbor[1]} {attribute}"
            if re.fullmatch(r"network \d+\.\d+\.\d+\.\d+/\d+", command):
                return parents, command
    return None


def parse(config: str) -> list[tuple[tuple[str, ...], str]]:
    return [
        (tuple(parent.strip() for parent in line.parents), line.config_line.strip())
        for line in EOSConfigParser(config).config_lines
        if line.config_line.strip()
    ]


def commands_text(lines: list[tuple[tuple[str, ...], str]]) -> str:
    result: list[str] = []
    context: tuple[str, ...] = ()
    for parents, command in lines:
        if parents != context:
            result.extend("exit" for _ in context)
            result.extend(parents)
            context = parents
        result.append(f"{' ' if parents else ''}{command}")
    result.extend("exit" for _ in context)
    return "\n".join(result) + ("\n" if result else "")


def make_plan(hostname: str, running: str, desired: str) -> DevicePlan:
    current_lines = parse(running)
    if not current_lines or not any(command == "end" for _, command in current_lines):
        raise ValueError("Incomplete running configuration; refusing to plan from missing state")
    desired_lines = parse(desired)
    scopes = {parents for parents, _ in desired_lines if parents}
    current_bgp = {
        command
        for parents, command in current_lines
        if not parents and command.startswith("router bgp ")
    }
    desired_bgp = {
        command
        for parents, command in desired_lines
        if not parents and command.startswith("router bgp ")
    }
    if current_bgp and current_bgp != desired_bgp:
        raise ValueError("Changing the existing BGP process requires an explicit migration")

    current: dict[FieldKey, str] = {}
    for parents, command in current_lines:
        # EOS may display a global IPv4 neighbor filter in address-family ipv4.
        field_parents = parents
        if (
            len(parents) == 2
            and parents[1] == "address-family ipv4"
            and re.fullmatch(r"neighbor \S+ prefix-list \S+ out", command)
        ):
            field_parents = parents[:1]
        key = field_key(field_parents, command)
        if key is not None:
            if key in current:
                raise ValueError(f"Ambiguous running state for {key}; refusing to guess")
            current[key] = command

    before: list[tuple[tuple[str, ...], str]] = []
    delta: list[tuple[tuple[str, ...], str]] = []
    seen: set[FieldKey] = set()
    for parents, command in desired_lines:
        if (*parents, command) in scopes:
            continue
        key = field_key(parents, command)
        if key is None:
            raise ValueError(f"Unsupported desired command for incremental deployment: {command}")
        if key in seen:
            raise ValueError(f"Duplicate desired field: {key}")
        seen.add(key)
        previous = current.get(key)
        if previous is not None:
            before.append((parents, previous))
        if previous != command:
            # EOS prefix-list sequence changes need the old rule removed first.
            if previous is not None and key[1].startswith("ip prefix-list "):
                delta.append((parents, f"no {key[1]}"))
            delta.append((parents, command))
    if not seen:
        raise ValueError("Desired configuration is empty")
    return DevicePlan(
        hostname=hostname,
        running_sha256=hashlib.sha256(json.dumps(current_lines).encode()).hexdigest(),
        desired_sha256=hashlib.sha256(desired.encode()).hexdigest(),
        current_managed=commands_text(before),
        desired=desired,
        commands=commands_text(delta),
    )


def incremental(root: Path, output: Path, operation: str) -> None:
    hosts = load_hosts(root)
    check_artifacts(hosts, output)
    path = output / "incremental-plan.json"
    expected: dict[str, DevicePlan] = {}
    if operation != "plan":
        expected = TypeAdapter(dict[str, DevicePlan]).validate_json(path.read_text())
        if set(expected) != set(hosts):
            raise ValueError("Plan hosts differ from the inventory; regenerate the plan")
        for name, host in hosts.items():
            desired = (output / f"{name}.cfg").read_text()
            if expected[name].hostname != str(host.hostname) or expected[name].desired != desired:
                raise ValueError(
                    f"{name}: target or desired configuration changed; regenerate plan"
                )

    observed: dict[str, DevicePlan] = {}

    def snapshot(task: Task) -> Result:
        device = task.host.get_connection("napalm", task.nornir.config)
        running = device.get_config(retrieve="running", full=True, sanitized=True)["running"]
        plan = make_plan(
            str(hosts[task.host.name].hostname),
            running,
            (output / f"{task.host.name}.cfg").read_text(),
        )
        observed[task.host.name] = plan
        return Result(host=task.host)

    def preflight(task: Task) -> Result:
        snapshot(task)
        name = task.host.name
        if observed[name] != expected[name]:
            raise ValueError(
                f"{name}: running state or plan changed; regenerate and review the plan"
            )
        return Result(host=task.host)

    def apply(task: Task) -> Result:
        # Recheck immediately before each write, as well as the all-host preflight.
        preflight(task)
        plan = expected[task.host.name]
        if not plan.commands:
            LOG.info("%s: no changes; skipping configuration", task.host.name)
            return Result(host=task.host)
        task.run(task=napalm_configure, configuration=plan.commands, replace=False, dry_run=False)
        snapshot(task)
        if observed[task.host.name].commands:
            raise RuntimeError(
                f"{task.host.name}: managed configuration did not reach desired state"
            )
        LOG.info("%s: delta applied and desired fields verified", task.host.name)
        return Result(host=task.host, changed=True)

    def save(task: Task) -> Result:
        if not expected[task.host.name].commands:
            LOG.info("%s: unchanged; skipping save", task.host.name)
            return Result(host=task.host)
        snapshot(task)
        if observed[task.host.name].commands:
            raise ValueError(f"{task.host.name}: managed state drifted before save")
        return save_host(task)

    with inventory(root, live=True) as nr:
        if operation == "plan":
            require_success(nr.run(task=snapshot))
            path.write_text(
                json.dumps({name: observed[name].model_dump() for name in sorted(hosts)}, indent=2)
                + "\n"
            )
            for name in sorted(hosts):
                LOG.info("%s delta:\n%s", name, observed[name].commands or "(no changes)")
        elif operation == "apply-diff":
            require_success(nr.run(task=preflight))
            require_success(nr.run(task=apply))
        elif operation == "save-diff":
            require_success(nr.run(task=save))
        else:
            raise ValueError(f"Unknown incremental operation: {operation}")
