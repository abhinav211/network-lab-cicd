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
    +-- main push -> Deploy to existing lab
    |                 |
    |                 v
    |               Required approval (existing-lab environment)
    |                 |
    |                 v
    |               Read live state -> plan delta -> apply delta
    |                 -> verify -> save changed routers -> leave running
    |
    +-- manual opt-in on main -> temporary Containerlab deployment
                              |
                              v
                            Apply -> verify -> destroy temporary lab
```

The existing-lab workflow downloads the exact successful build's configurations
and checks out that build's commit. PRs and manual test-only runs do not update
the persistent lab. Only a successful **push build on main** triggers its approval
flow. The persistent deployment never creates or destroys containers.
It reads current configuration and sends only differing managed commands,
not the full rendered configuration. Unchanged routers receive no configuration
candidate and no save command.

“Build” renders network configurations; it does not build or redistribute Arista's
licensed container image. Pull requests never deploy through these workflows.

## Deployment modes

| Mode | Trigger | Runner label | Result |
| --- | --- | --- | --- |
| Existing lab | Successful main push build, then approval | `existing-lab` | Update and keep existing routers |
| Temporary lab | Manual run with `deploy_temporary_lab` checked | `containerlab` | Create, test and destroy a lab |

Both deployment jobs share a repository-wide concurrency group and do not cancel
an in-progress deployment. They are not a FIFO queue; a superseded pending job
can be replaced by a newer one. Do not run the temporary deployment on a host
already using its management subnet for a persistent lab.

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
| [.github/workflows/lab.yml](.github/workflows/lab.yml) | Automatic test/build; opt-in temporary lab |
| [.github/workflows/deploy-existing.yml](.github/workflows/deploy-existing.yml) | Approved deployment of tested artifacts to existing routers |
| [lab/deployment_guard.py](lab/deployment_guard.py) | Reject untrusted/stale builds and missing reviewer protection |
| [lab/incremental.py](lab/incremental.py) | Read live state, plan minimal changes, reject stale plans and apply only differences |
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

For **existing-lab deployment only**, use a trusted Linux x86-64 runner with
Python support and SSH connectivity to every management IP in the inventory.
Register it with labels `self-hosted`, `linux`, `x64`, and **`existing-lab`**.
This workflow needs no Docker socket, Containerlab installation, or sudo access;
the routers must already be running. A runner on their Docker host can reach
them directly; a remote runner needs a route to their management network.
Use a separate runner work directory: checkout cleans its workspace, so never
store the live Containerlab files there.

The steps below apply to a runner that will also **create temporary labs**:

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
Rebuild disposable runners regularly. The temporary-lab runner must not host
other labs using this subnet; concurrency is serialized within this repository
only. Restrict persistent-lab runners too: network access and device credentials
are sensitive even without Docker privileges.

Containerlab's default cEOS bootstrap uses the lab-only `admin` account and its
documented default password. Supply those values through GitHub's environment
settings below. Setting a different secret does **not** change the device's
bootstrap credentials. SSH host-key checking uses the driver's ephemeral-lab
defaults, not production trust provisioning. Keep the Docker management network
isolated; no host ports are published by this topology.

## 3. Enable GitHub Actions

1. Push this project to your own trusted/private GitHub repository with `main`
   as the default branch. No remote repository is created automatically.
2. In **Settings -> Environments**, create **`existing-lab`**:
   - Enable **Required reviewers** and choose at least one reviewer.
   - Variable `LAB_USERNAME`: the running devices' username.
   - Secret `LAB_PASSWORD`: the running devices' password.
   - Restrict deployment branches to `main`.
   - Disable administrator bypass where available. Enable prevention of
     self-review if a different person must approve deployments.
3. Protect `main` and require the `test` and `build` jobs before merging.
4. Open a PR to run offline CI. Merge to `main`: once **Network lab CI/CD**
   succeeds, **Deploy to existing lab** starts its guard job.
5. Open **Deploy to existing lab -> Review deployments**, review the source
   commit and its `device-configs` artifact, select `existing-lab`, and approve.
   The runner reads live state, plans differences, applies only those differences,
   verifies, saves changed routers, and leaves your lab running.

**Approval is not enabled just by committing the workflow.** Required reviewers
must be configured in GitHub. The guard reads the environment settings and fails
closed if the environment is missing, inaccessible, or has no required reviewers.
If your plan does not support required reviewers for this repository's visibility,
deployment is blocked rather than silently bypassing approval. GitHub currently
restricts private-repository required-reviewer rules to Enterprise plans; see
[environment availability](https://docs.github.com/en/rest/deployments/environments).
Do not expose a shared privileged runner publicly merely to obtain that feature.

Keep both workflow files on the default branch. The existing-lab workflow uses
`workflow_run`, accepts only successful same-repository main **push** builds,
and downloads `device-configs` by that run's ID (not a floating "latest" artifact).
It checks out the corresponding SHA and validates checksums before configuration.
It rejects builds no longer at the main tip both before approval and again after
approval, before contacting devices. It does not cancel changes already in flight.
If a newer push supersedes an approval, approve that newer successful build.
Approve within the artifact's seven-day retention period.

To retry a failed deployment for the same current commit, use **Re-run failed
jobs** in **Deploy to existing lab** and approve again if GitHub requests it.
For a new candidate, push a new commit. A manual test-only CI run does not deploy
to the existing lab.

### Optional temporary-lab workflow

Create a separate **`containerlab`** environment with `LAB_USERNAME` and
`LAB_PASSWORD` for the default cEOS bootstrap, and restrict it to `main`.
Under **Actions -> Network lab CI/CD -> Run workflow**, select `main` and check
**deploy_temporary_lab**. Leaving it unchecked runs only test/build jobs.

This opt-in deployment uses a unique lab/network name, downloads the current
run's configuration artifact, and automatically cleans up afterward. It does
not trigger an existing-lab deployment because its source event is manual,
not a push. Credentials are never included in rendered configurations or
uploaded as artifacts.

### Verification and failure behavior

Readiness polls NAPALM facts, not merely an open TCP port. Verification retries
while BGP converges and requires, on every router:

- Exactly the expected BGP peers, all enabled and established with correct ASNs.
- An active BGP route to every other router's loopback.
- Three successful ping probes, with zero loss, to every remote loopback,
  sourced from the local loopback (tests routed traffic in both directions).

Task failures produce nonzero exit statuses. Readiness and convergence have
retry budgets; an in-flight driver call can extend these by its connection/
command timeout. The existing-lab job has a 20-minute limit; the temporary-lab
job has a 30-minute limit.

NAPALM uses **merge**, preserving management access. Existing-lab mode sends only
the delta; temporary-lab mode still sends the full rendered candidate to its
fresh devices. This is not a production replacement/rollback workflow.
In existing-lab mode, a configuration or
verification failure stops later steps and leaves the lab running for diagnosis;
already-applied changes are not rolled back. The explicit `python -m lab save-diff`
step runs only after all verification passes and requires EOS to confirm
`copy running-config startup-config` on each changed router.

**Persistence caveat:** NAPALM's EOS `commit_config` already performs
`write memory` during configuration. The post-verification save is an additional
checked save, not a transactional persistence boundary. Even if verification
fails, some configuration changes may already be saved. A save failure also
fails the job, but saves completed on other routers remain.

Temporary-lab mode instead destroys its temporary lab on success or failure.

### Incremental configuration: current state versus desired state

The build artifact remains the desired configuration. Before applying it, the
existing-lab workflow reads NAPALM's full running configuration (including defaults,
using EOS sanitization), parses its hierarchy, and compares template-managed
fields. The SSH command-read timeout is 120 seconds to allow expanded running
configuration reads on cEOS; connection timeouts remain separate. Indentation
differences do not cause updates. For a description-only
change on r1, the entire candidate sent to that router is:

```text
interface Ethernet1
 description -to-r3
exit
```

There are no IP, BGP or prefix-list commands in that candidate if those fields
already match. If another desired field has drifted, it also appears in the plan:
this is a **live-state diff**, not just a Git diff of the most recent commit.

The generated `build/incremental-plan.json` records the target management address,
current values of desired fields, desired configuration, delta commands, and
fingerprints of running/desired state. Full running configurations are not written
to disk or uploaded; unrelated configuration and secrets are excluded from the
managed-state snapshot. Protect plan artifacts like your inventory.

All hosts are checked before any writes. Apply recomputes the plan, rejects
changed running state or modified artifacts, then rechecks each router immediately
before its write. After applying, it reads state again and requires the managed
fields to match the desired state. A fresh plan must show no changes on a
converged lab. There is no cross-device transaction or lock against an external
operator changing configuration during the final read/write interval.

The workflow creates the plan **after** environment approval and applies it
automatically. To inspect the exact live delta before any configuration writes,
run these commands locally with `LAB_USERNAME` and `LAB_PASSWORD` already set:

```bash
python -m lab build &&
python -m lab plan
```

Review the printed commands and `build/incremental-plan.json`. Only then run:

```bash
python -m lab apply-diff &&
python -m lab verify --timeout 180 &&
python -m lab save-diff
```

These local commands bypass GitHub approval. Do not run them concurrently with a
workflow deployment. Regenerate and review the plan if apply reports stale state.
The plan captures state anew every run; it does not trust a runner-local historical
snapshot that could have become stale.

**Scope:** only the commands supported by the current EOS template are planned.
Unknown desired commands, incomplete running-state reads and changes to an
existing BGP process ASN fail explicitly rather than falling back to a full push.
Changing a prefix-list sequence removes that sequence and adds its new rule in
the same candidate. Other rules are preserved. Removing fields, interfaces,
neighbors or rules from the inventory does **not** delete them from a router.
This preserves the existing additive ownership policy; explicit deletion
reconciliation and topology provisioning are not implemented.

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
merging to `main` triggers the existing-lab approval flow after a successful
build. No workflow changes are needed for subsequent prefix-list entries.
Temporary deployment is manual opt-in only; do not run it alongside a
persistent lab using `172.31.250.0/24` on the same host.

For an existing lab only, after reviewing the build and exporting `LAB_USERNAME`
and `LAB_PASSWORD`, use:

```bash
python -m lab wait --timeout 120 &&
python -m lab plan
```

Review the plan, then apply it:

```bash
python -m lab apply-diff &&
python -m lab verify --timeout 180 &&
python -m lab save-diff
```

This updates all inventory routers without creating or destroying containers.
It does not invoke the automatic-cleanup lifecycle script. These local commands
do not use GitHub's approval gate; use the workflow for reviewed deployments.

Offline tests check rule rendering and validation. Live verification still
checks baseline BGP sessions, loopback routes and pings; it does not prove every
permit/deny rule. Inspect `show ip prefix-list` and
`show ip bgp neighbors 10.10.13.1 advertised-routes` on r1, using known present
routes to verify filtering. Blocking baseline loopbacks will fail live checks.

**Ownership limitation:** removing an entry or a neighbor attachment from YAML does
not remove the existing device configuration. This is additive configuration,
not deletion reconciliation. Incremental deployment can replace a changed
prefix-list sequence. For deletions, use a fresh CI lab or explicitly remove
the obsolete device commands first.

## Cleanup and troubleshooting

**Existing lab:** inspect the **Deploy to existing lab** job and its
`existing-lab-results-*` artifact for readiness, plan, configuration, verification,
save logs and the incremental state/plan JSON. There is no cleanup step.
If the guard fails, check required reviewers,
token read access and whether the source SHA is still the main tip. If the deploy
job is queued, check approval status and the runner's `existing-lab` label.
Do not destroy your persistent lab to retry a configuration deployment.

**Temporary lab only:**

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
