import re
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

ASN = Annotated[int, Field(strict=True, ge=1, le=4294967294)]
PrefixListName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Interface(Model):
    name: str = Field(pattern=r"^Ethernet[1-9][0-9]*$")
    description: str = Field(pattern=r"^[a-zA-Z0-9 _.-]+$", max_length=100)
    address: IPv4Interface


class PrefixRule(Model):
    sequence: int = Field(strict=True, gt=0)
    action: Literal["permit", "deny"]
    prefix: IPv4Network


class PrefixList(Model):
    name: PrefixListName
    entries: list[PrefixRule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sequences(self) -> Self:
        sequences = [entry.sequence for entry in self.entries]
        if len(set(sequences)) != len(sequences):
            raise ValueError(f"{self.name}: duplicate prefix-list sequence")
        return self


class Neighbor(Model):
    address: IPv4Address
    remote_as: ASN
    prefix_list_out: PrefixListName | None = None


class BGP(Model):
    asn: ASN
    neighbors: list[Neighbor] = Field(min_length=1)


class Data(Model):
    loopback: IPv4Interface
    interfaces: list[Interface] = Field(min_length=1)
    bgp: BGP
    prefix_lists: list[PrefixList] = Field(default_factory=list)

    @field_validator("loopback")
    @classmethod
    def loopback_is_host_route(cls, value: IPv4Interface) -> IPv4Interface:
        if value.network.prefixlen != 32:
            raise ValueError("loopback must be a /32")
        return value

    @model_validator(mode="after")
    def valid_prefix_list_references(self) -> Self:
        names = [prefix_list.name for prefix_list in self.prefix_lists]
        if len(set(names)) != len(names):
            raise ValueError("duplicate prefix-list name")
        for neighbor in self.bgp.neighbors:
            if neighbor.prefix_list_out is not None and neighbor.prefix_list_out not in names:
                raise ValueError(
                    f"neighbor {neighbor.address}: undefined outbound prefix-list "
                    f"{neighbor.prefix_list_out}"
                )
        return self


class Host(Model):
    hostname: IPv4Address
    groups: list[Literal["eos"]] = Field(min_length=1, max_length=1)
    data: Data


def read_yaml(path: Path) -> dict:
    with path.open() as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def load_hosts(root: Path) -> dict[str, Host]:
    hosts = TypeAdapter(dict[str, Host]).validate_python(read_yaml(root / "inventory/hosts.yaml"))
    if len(hosts) < 2 or any(not re.fullmatch(r"[a-z][a-z0-9-]*", name) for name in hosts):
        raise ValueError("inventory needs at least two hosts with safe lowercase names")
    validate_topology(hosts, read_yaml(root / "topology.clab.yml"))
    return hosts


def validate_topology(hosts: dict[str, Host], topology: dict) -> None:
    nodes = topology["topology"]["nodes"]
    if set(nodes) != set(hosts):
        raise ValueError("topology nodes must exactly match inventory hosts")
    subnet = IPv4Network(topology["mgmt"]["ipv4-subnet"])
    addresses: set[IPv4Address] = set()
    endpoints: dict[str, tuple[str, Interface]] = {}
    for name, host in hosts.items():
        if nodes[name]["kind"] != "arista_ceos":
            raise ValueError(f"{name}: only arista_ceos is supported")
        if str(host.hostname) != nodes[name]["mgmt-ipv4"] or host.hostname not in subnet:
            raise ValueError(f"{name}: management address does not match topology/subnet")
        if host.hostname in (subnet.network_address, subnet.broadcast_address, subnet[1]):
            raise ValueError(f"{name}: management address is reserved")
        for address in [host.hostname, host.data.loopback.ip] + [
            interface.address.ip for interface in host.data.interfaces
        ]:
            if address in addresses:
                raise ValueError(f"duplicate IP address: {address}")
            addresses.add(address)
        neighbors = [peer.address for peer in host.data.bgp.neighbors]
        if len(set(neighbors)) != len(neighbors):
            raise ValueError(f"{name}: duplicate BGP neighbor")
        for interface in host.data.interfaces:
            endpoint = f"{name}:eth{interface.name.removeprefix('Ethernet')}"
            if endpoint in endpoints:
                raise ValueError(f"duplicate interface: {endpoint}")
            if interface.address.network.prefixlen not in (30, 31):
                raise ValueError(f"{endpoint}: use /30 or /31 for point-to-point links")
            if interface.address.ip not in interface.address.network.hosts():
                raise ValueError(f"{endpoint}: unusable interface address")
            endpoints[endpoint] = (name, interface)

    linked: set[str] = set()
    expected: dict[str, set[tuple[IPv4Address, int]]] = {name: set() for name in hosts}
    for link in topology["topology"]["links"]:
        pair = link["endpoints"]
        if len(pair) != 2 or pair[0] == pair[1]:
            raise ValueError("each link requires two distinct endpoints")
        for endpoint in pair:
            if endpoint not in endpoints or endpoint in linked:
                raise ValueError(f"unknown or reused link endpoint: {endpoint}")
            linked.add(endpoint)
        left, right = (endpoints[endpoint] for endpoint in pair)
        if left[0] == right[0] or left[1].address.network != right[1].address.network:
            raise ValueError("links must connect different routers in the same subnet")
        if hosts[left[0]].data.bgp.asn == hosts[right[0]].data.bgp.asn:
            raise ValueError("this lab requires eBGP on every link")
        for local, remote in ((left, right), (right, left)):
            expected[local[0]].add((remote[1].address.ip, hosts[remote[0]].data.bgp.asn))
    if linked != set(endpoints):
        raise ValueError("every configured Ethernet interface must have a topology link")
    for name, host in hosts.items():
        actual = {(peer.address, peer.remote_as) for peer in host.data.bgp.neighbors}
        if actual != expected[name]:
            raise ValueError(f"{name}: BGP neighbors/ASNs do not match physical links")
