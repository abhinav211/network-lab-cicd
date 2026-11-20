# Containerlab network CI/CD

An original four-router eBGP lab using **Containerlab + Nornir + NAPALM + Jinja2**.
The device image is **`ceos:4.32.0F`**. No image or credentials are distributed here.

```text
PR / main / manual
    |
    v
TEST: lint, type-check, unit tests, inventory/topology validation
    |
    v
BUILD: Jinja2 -> one EOS configuration per host + SHA256SUMS artifact
    |
    v  main only, protected environment, dedicated self-hosted runner
DEPLOY: Containerlab -> wait for NAPALM facts -> merge built configurations
    |
    v
VERIFY: exact BGP peer/ASN state, active BGP routes, loopback-to-loopback pings
    |
    v
CLEANUP: destroy this run's lab on success or failure
```

“Build” renders network configurations; it does not build or redistribute Arista's
licensed container image. Pull requests never deploy to the privileged runner.

## Topology

```text
r1 (AS65001) --- Ethernet1
                          \
r2 (AS65002) ----- Ethernet2 r3 (AS65003)
                          /
r4 (AS65004) --- Ethernet3
```

Each leaf uses Ethernet1; labels beside r3 identify its interfaces.
All links are routed /31s. Every router advertises a unique Loopback0 /32.
Management addresses are `172.31.250.11` through `.14`.

| File | Purpose |
| --- | --- |
| [topology.clab.yml](topology.clab.yml) | cEOS nodes, management subnet, physical links |
| [inventory/hosts.yaml](inventory/hosts.yaml) | Nornir inventory, interface addresses, ASNs, neighbors |
| [inventory/groups.yaml](inventory/groups.yaml) | NAPALM EOS/SSH connection settings |
| [templates/eos.j2](templates/eos.j2) | Strict Jinja2 configuration template |
| [lab/](lab/) | Validation, Nornir rendering, deployment tasks and live assertions |
| [.github/workflows/lab.yml](.github/workflows/lab.yml) | Test -> build -> ephemeral integration lab |
| [scripts/run-lab.sh](scripts/run-lab.sh) | Local/CI lifecycle with cleanup trap |

## 1. Run offline checks

Use Python 3.14 (the version used by CI and the dependency lock).

```bash
cd /home/abhinav/network-lab-cicd
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
ruff check .
ruff format --check .
mypy
pytest -q
python -m lab validate
python -m lab build
```

These commands need no Docker, network devices, lab password or root access.
The generated configuration artifact is under `build/`. Undefined template
variables, invalid IPs, duplicate addresses, incorrect remote ASNs, mismatched
management addresses and missing/reused topology endpoints fail the build.

The lock contains runtime and development dependencies. To deliberately update
it, install updated dependencies into a clean virtual environment and run:

```bash
python -m pip freeze --exclude network-lab-cicd > requirements.lock
```

Re-run all offline checks before committing a refreshed lock.

## 2. Prepare a dedicated runner

Do this on a **disposable, isolated Linux x86-64 runner**, not a shared machine or
a production network host:

1. Install Docker Engine and Containerlab from their official instructions.
   Use a current Containerlab release supporting `arista_ceos`; record and pin
   the tested version in your runner image.
2. Obtain cEOS 4.32.0F from Arista under your own license and import it:

   ```bash
   docker import /path/to/cEOS64-lab-4.32.0F.tar.xz ceos:4.32.0F
   docker image inspect ceos:4.32.0F
   containerlab version
   ```

3. Provide enough capacity for four cEOS containers; start with 4 vCPUs / 8 GB
   RAM and adjust using measured boot times and the image's requirements.
4. Ensure `172.31.250.0/24` does not overlap host, VPN or Docker networks.
   If necessary, change **both** topology and inventory management addresses.
5. Register a repository-scoped GitHub Actions runner labeled `containerlab`
   (in addition to `self-hosted`, `linux`, `x64`).
6. Provision Docker access and unattended privileged Containerlab execution
   for the runner account. The scripts use `sudo -A containerlab`; CI must have
   pre-approved noninteractive privileges or a working askpass integration.
   Never store a sudo password in this project or its workflow.

Docker access and Containerlab privileges are effectively root access. Restrict
this runner to trusted repositories and maintainers; do **not** attach it to a
public repository that accepts untrusted workflow jobs. A job's `if` condition
alone cannot protect a runner from malicious changes to a PR's workflow.
Rebuild disposable runners regularly. The runner must not host other labs using
this subnet; concurrency is serialized within this repository only.

Containerlab's default cEOS bootstrap uses the lab-only `admin` account and its
documented default password. Supply those values through GitHub's environment
settings below. Setting a different secret does **not** change the device's
bootstrap credentials. SSH host-key checking uses the driver's ephemeral-lab
defaults, not production trust provisioning. Keep the Docker management network
isolated; no host ports are published by this topology.

## 3. Enable GitHub Actions

1. Push this project to your own trusted/private GitHub repository with `main`
   as the default branch. No remote repository is created automatically.
2. Create the GitHub environment **`containerlab`**:
   - Variable `LAB_USERNAME`: the bootstrap username.
   - Secret `LAB_PASSWORD`: the bootstrap password.
   - Restrict deployment branches to `main`; add required reviewers where
     supported by your GitHub plan.
3. Protect `main` and require the `test` and `build` jobs before merging.
4. Open a PR to run offline CI. Merge to `main` (or use **Run workflow** on
   `main`) to deploy and test the temporary lab.

The deploy job downloads exactly the configuration artifact produced by its
build job and verifies every checksum **before** modifying any device.
It uses a unique lab/network name for each run/attempt. Credentials are never
included in rendered configurations or uploaded as artifacts.

Readiness polls NAPALM facts, not merely an open TCP port. Verification retries
while BGP converges and requires, on every router:

- Exactly the expected BGP peers, all enabled and established with correct ASNs.
- An active BGP route to every other router's loopback.
- Three successful ping probes, with zero loss, to every remote loopback,
  sourced from the local loopback (tests routed traffic in both directions).

Task failures produce nonzero exit statuses. Readiness and convergence have
retry budgets; an in-flight driver call can extend these by its connection/
command timeout. The GitHub job additionally has a 30-minute limit.
NAPALM uses **merge**, preserving management access. This is not a production
replacement/rollback workflow: partial configuration failure fails the job and
the entire temporary lab is destroyed.

## 4. Optional local integration run

Only on the prepared dedicated runner, after building configurations:

```bash
source .venv/bin/activate
export LAB_NAME="netci-$(date +%s)-1"
read -r -p 'Lab username: ' LAB_USERNAME
export LAB_USERNAME
read -r -s -p 'Lab password: ' LAB_PASSWORD; echo
export LAB_PASSWORD
bash scripts/run-lab.sh
unset LAB_PASSWORD
```

The script always tears down the lab, including after failed configuration or
verification. It refuses existing resources rather than reconfiguring them.
No production device inventory should be used with this project.

## 5. Change an outbound BGP prefix-list

Each host can define optional `data.prefix_lists`. A BGP neighbor selects a
host-local list with `prefix_list_out`. Routers without these fields remain
unfiltered. In [inventory/hosts.yaml](inventory/hosts.yaml), r1 now has:

```yaml
    prefix_lists:
      - name: R1-TO-R3
        entries:
          - sequence: 10
            action: permit
            prefix: 10.255.0.1/32
          - sequence: 20
            action: permit
            prefix: 10.100.1.0/24
    bgp:
      asn: 65001
      neighbors:
        - address: 10.10.13.1
          remote_as: 65003
          prefix_list_out: R1-TO-R3
```

This filters advertisements **from r1 toward r3**. The existing loopback remains
permitted so the baseline route/ping checks can still pass. An entry can use
`permit` or `deny`; unmatched prefixes are implicitly denied. Rules are evaluated
in ascending sequence order and rendered in that order. Matching is exact:
`10.100.1.0/24` does not match its more-specific subnets. `ge`/`le` and inbound
filters are not implemented.

**Permitting a prefix does not originate it in BGP.** The sample
`10.100.1.0/24` is only a permitted filter entry; the lab does not create or
advertise that route. Origination requires a suitable route and BGP configuration.

To add another rule, add an entry with a unique positive integer `sequence`, an
`action`, and an IPv4 network `prefix`. Use the network address, not a host address
with host bits set. Validation rejects duplicate list names, duplicate sequences,
invalid rules, and neighbor references to lists not defined on that host.

Run the offline gates and rebuild:

```bash
source .venv/bin/activate
ruff check . &&
ruff format --check . &&
mypy &&
pytest -q &&
python -m lab validate &&
python -m lab build
```

Review [build/r1.cfg](build/r1.cfg). Commit the inventory, schema, template, tests,
and documentation changes together. A PR runs the existing test/build jobs;
merging to `main` or manually running the workflow on `main` also runs the
temporary integration lab. No workflow changes are needed for prefix-list
rendering. This workflow **does not update an already-running persistent lab**.
Do not run its deployment alongside a persistent lab using `172.31.250.0/24` on
the same host; use a separate runner or a non-overlapping topology/inventory.

For an existing lab only, after reviewing the build and exporting `LAB_USERNAME`
and `LAB_PASSWORD`, use:

```bash
python -m lab wait --timeout 120 &&
python -m lab configure &&
python -m lab verify --timeout 180
```

This updates all inventory routers without creating or destroying containers.
It does not invoke the automatic-cleanup lifecycle script. Save the running
configuration on each changed router after verifying it if restart persistence
is required.

Offline tests check rule rendering and validation. Live verification still
checks baseline BGP sessions, loopback routes and pings; it does not prove every
permit/deny rule. Inspect `show ip prefix-list` and
`show ip bgp neighbors 10.10.13.1 advertised-routes` on r1, using known present
routes to verify filtering. Blocking baseline loopbacks will fail live checks.

**Merge limitation:** removing an entry or a neighbor attachment from YAML does
not remove the existing device configuration. This is additive configuration,
not policy reconciliation. For deletions or changes to existing rules, use a
fresh CI lab or explicitly remove the obsolete device commands first.

## Cleanup and troubleshooting

The shell EXIT/INT/TERM traps perform cleanup; an `always()` workflow step retries
it if necessary. Cleanup targets only the generated topology and checks that its
containers and management network are gone. There is no global Docker prune.

No workflow can guarantee cleanup after host loss, SIGKILL or a forcibly stopped
runner. Retain the workspace on failure and recover **only that run's lab**:

```bash
export LAB_NAME=netci-123456-1  # replace with the actual failed run and attempt
bash scripts/cleanup-lab.sh
```

If the workspace was lost, obtain that run's `runtime.clab.yml` from the
`lab-results-*` artifact, inspect its name and resources, then explicitly destroy
that topology on the original runner. Never destroy all labs as a shortcut.

Inspect GitHub job logs and the `lab-results-*` artifact for deploy, readiness,
configuration, verification and cleanup logs. Failure messages identify the
affected router/peer/route. Missing image, credentials or privileges fail before
deployment. Live cEOS compatibility and BGP convergence must be verified on the
prepared runner; offline unit tests use simulated devices.

## References

- [Containerlab cEOS setup and interface mapping](https://containerlab.dev/manual/kinds/ceos/)
- [Nornir documentation](https://nornir.readthedocs.io/)
- [NAPALM documentation](https://napalm.readthedocs.io/)
- [Jinja documentation](https://jinja.palletsprojects.com/)

The supplied Packetswitch article inspired the tool selection; this project is
an independent implementation with CI gates, validation and ephemeral cleanup.
