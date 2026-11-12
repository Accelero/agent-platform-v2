# Personal Hermes implementation status

The source implements the personal Hermes manager, gateway and runtimes
described in the [specification](../SPEC.md), the
[architecture](../ARCH.md) and the guides for [instances](instances.md),
[terminal backends](terminal-backends.md) and the [dashboard](dashboard.md).
[`TESTS.md`](../../services/hermes-manager/TESTS.md) maps every requirement,
acceptance scenario and contract operation to named tests and their levels;
the statuses describe cited examples, not a passing execution receipt for this
checkout. The
[coverage assessment](../testing/coverage.md) identifies the additional
clause, boundary and security evidence needed beyond those examples.

## What exists

| Area | Source |
| --- | --- |
| Hermes image and plugin | Pinned upstream Hermes with reviewed patches and the bundled `managed_execution` plugin; native realm OIDC; signed home-initialization receipt; protected terminal policy |
| Terminal worker image | `agent-runtime`, `managed-ssh-v2`; its entrypoint binds the `worker` account to the UID/GID the Pod runs as, so one image serves every backend identity; a custom image may bake one instead |
| Manager | `services/hermes-manager`: the `api` role (administration API and dashboard, execution API, route resolver), the leader-elected `controller` role and the operator commands (`migrate`, `check`, `initialize`, `grant-administrator`, `rebind-cluster`, `generate-sealing-key`), all with state only in PostgreSQL |
| Dashboard | React and TypeScript, served by the `api` role under a strict CSP: users and the Keycloak directory, assignments with instance status and terminal environments, the template and backend catalogs with form, JSON and YAML editing, storage with orphans, secrets, administrators and the audit log |
| Shared administration | Immutable shared assignments, endpoints with callback URLs, explicit identity/group grants, versioned groups and memberships; API previews, updates, retries, rollouts and inventories cover both kinds. Access-only commands do not alter workloads. Shared serving and dashboard workflows remain planned |
| Desired state | Administrator commands through the command envelope: assignments, template and terminal backend versions (Draft, Published, Withdrawn), volumes from installation storage sources, admin settings and write-only admin secrets sealed to the controller |
| Reconciliation | Pure planners and server-side apply; create-once material; epoch-guarded status; preconditioned deletes; static admission policies and RBAC from the chart; orphaned namespaces and volumes reported, never adopted |
| Terminal backends | Environments and leases with database-time expiry; worker Pods with the instance's keys, the assignment's mounts and private registry credentials; backend-declared UID/GID and `fsGroup`; in-process SSH readiness against the declared UID; pull, start and contract failures reported without replacement |
| Shared resolver | Explicit personal/shared targets; one-snapshot endpoint, grant and group checks; shared grants do not provision personal instances. Native shared serving and per-host gateway sessions remain planned |
| Routing | `hermes-gateway`: OIDC platform sessions, route resolution over mTLS, forwarding to the owner's instance only, header and cookie hygiene, revocation of open WebSockets and streams |
| Chart | `charts/agent-platform` deploys `api`, `controller` and `hermes-gateway` with generated configuration, RBAC, admission policies, NetworkPolicies and one-shot operator Jobs |

Not part of the platform, by the specification's scope: coordinated
checkpoints, automatic backups (they are deployment-owned), SMB storage,
terminal service egress, worker prewarming, board integration, serving shared instances
and a command that permanently deletes an instance's data.

An update or stop sends SIGTERM: the runtime gives its gateway and dashboard
20 s, the Pod grace period is 45 s, active agent runs can be interrupted, and
nothing rolls back automatically.

## Evidence

| Suite | Assertions exercised by the suite | Coverage limits |
| --- | --- | --- |
| Rust workspace (`pytest -m "manager or gateway"`, with disposable PostgreSQL) | Domain planners and validators; port contract cases on the in-memory and PostgreSQL stores; contract-checked administration, execution, resolver and gateway responses over real TLS; OIDC sessions against a fake realm; gateway hygiene and revocation timing; binary roles, configuration gates and abrupt kills; with `fault-injection`, the API aborted right before and right after a commit | Behaviour on a real cluster or with real Hermes |
| Rust cluster tests (`pytest -m "(manager or worker) and cluster"`, on a disposable k3s) | Server-side apply, admission and RBAC as the controller's real ServiceAccount on k3s; preconditioned deletes; leader election; the controller process aborted at every controller fault point, including right after a delete; a real `agent-runtime` worker with the instance's keys; an unpullable worker image reported as `image_unavailable`; the owned objects the orphan scan lists | Real Hermes instances, Keycloak, the gateway on a cluster |
| `tests/e2e/` (`pytest -m e2e`) | The chart installed with its operator Jobs; real Hermes instances commissioned from published versions through the administration API; two owners on one hostname through the gateway with Keycloak and native realm login; denial and no enrollment without an assignment; distinct credentials; static folders shared across owners with independent read-only settings; a missing static directory blocking the instance until it exists; chat over the native WebSocket against an in-cluster fake model, with the template's and assignment's settings enforced over the owner's and the owner's own settings kept; the model key delivered only to the referencing instance and never returned; uploads in the owner's private home only, bounded by the gateway; a cron job firing without browser traffic and running its command in a terminal backend; two backends sharing the workspace, a backend-only static fixture home mounted only in one and the private home in neither; idle workers retired; a backend change during an active lease; a private registry backend with wrong credentials reported and right ones pulling; platform cookies, native tickets and forged headers checked; another native identity does not change platform routing; an email change at the provider; the manager, the database and the provider unavailable, and a person disabled at the provider, denied under the current test thresholds; suspension closing an open WebSocket and draining a pending worker; data and claims kept on re-enable; only the controller holding cluster rights, an execution credential refused by the administration API and the gateway, and no secret in any log; a restore onto a new cluster with `rebind-cluster`, static volume data and the original material; an instance missing from an older backup reported as orphans and left alone | A real model provider, NFS sources and multi-node scheduling |
| `charts/tests/test_manager_chart.py` | The chart's admission policies and RBAC equal the renderer's; generated role configurations are the ones the Rust parsers load; pod security, NetworkPolicy peers, strict values and operator Jobs | A live installation |
| `pytest -m "hermes and image"` | The reviewed runtime image as 14 separately collected cases (`runtimes/hermes/tests/integration/test_image_runtime.py`): non-root gateway and dashboard, private API denial, CA handling, init-only key isolation, managed configuration precedence and state after a forced restart | Cluster or provider acceptance |
| `runtimes/hermes/tests/integration/test_login.py` | Nine collected cases on the project image with the pinned upstream OIDC provider, with a disposable Keycloak and a TLS relay on an internal Docker network: two users and concurrent native WebSockets; upstream source, terminal selector and home binding; two hosts with SSO, refresh and logout under both refresh-token policies; exact callbacks and native APIs; three provider-session cases exercising native logout with no gateway client, a retained gateway client and both clients revoked | Platform authentication or routing; real browser interaction; shared gateway authentication and routing are not yet included |
| `hermes-kube/tests/admission.rs` | The real admission and RBAC matrix as the controller's ServiceAccount: a positive control and one-field negatives for Pods, namespaces, volumes, Services and RoleBindings, each refused by the intended policy or Pod Security layer, worker-labelled Pods with another non-zero identity, strict Hermes identity and container override refusals, plus escalation and impersonation refusals | Real traffic, NetworkPolicy enforcement, multi-node scheduling |
| Dashboard (`ui`) | Unit tests, Chromium presentation workflows with mocked APIs, and a separate real-server production-build/CSP browser case | The native UI as a rendered application |
| `tests/e2e/test_browser.py` | Chromium (Python Playwright) on the installed chart through Keycloak's login form, trusting the fixture CA: cookie scope, owner-only instance and files, dashboard-driven commissioning of a working instance, stale-revision refusal, administrator revocation, foreign-origin requests, logout and session replay, hostile text shown as text | Native chat, history and WebSocket views; template rollout in the browser |

## Testing strategy and coverage limits

The shared-agent [Phase 0 native prerequisites](../shared-hermes-agents.md#phase-0-native-login-for-several-users)
pass, including both SH-N04 refresh-token policies and the three provider-session
cases. Logout keeps token revocation; signing in again may require credentials
when the provider ends its session. The gateway foundation classifies HTTP/1.1
and HTTP/2 authorities before session lookup and ignores tossed platform-cookie
lookalikes. Shared assignments, endpoints, grants and host-bound gateway sessions
are implemented. The shared-agent plan records the remaining acceptance and
deployment verification; its unchecked cases are not implied by implementation.

The [testing strategy](../TESTING.md) specifies language-native units,
integration tests at their proving boundary, and independently collected
Python/pytest E2E on disposable k3d clusters. The
[coverage assessment](../testing/coverage.md) inventories current evidence
against every requirement, acceptance row and operational bound. The
[gap register](../testing/gaps.md) specifies the missing tests and fixture/runner
changes. These documents distinguish required test-system behaviour from
features that are already implemented.

`uv run pytest -q` selects the tests that need no infrastructure. The
end-to-end cases under `tests/e2e/` run with `uv run pytest -m e2e`, and CI
splits them across five runners. Their fixtures use k3d: the network matrix
has one server and two agents; the installed platform runs on a separate
single node; destructive cases share a pool of platforms of their own. The
restore case creates a second direct-k3s cluster.

These suites cover selected examples rather than every clause and security
boundary. Live NFS, multi-node placement of the installed platform and full
private-home restore are not established. The tables describe suite
assertions; a run's JUnit report records its actual outcomes. Clause and
scenario coverage limits are recorded in G01/G03.

## Verification commands

Use Python 3.14, `uv sync --locked`, Helm 3, Docker, Node from
`services/hermes-manager/ui/.node-version` and the toolchain in
`services/hermes-manager/rust-toolchain.toml`. Run from the repository root
unless shown otherwise. The fixtures create and remove their own PostgreSQL,
clusters and images; never point these tests at deployment data.

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
helm lint charts/agent-platform
uv run python -m agent_platform.privacy

cd services/hermes-manager
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cd ../..
uv run pytest -m "manager or gateway or dashboard or worker"
uv run pytest -m "hermes and image"
HERMES_TEST_K3D=$(command -v k3d) uv run pytest -m e2e
```

Select one component and level with, for example,
`uv run pytest -m "manager and integration"`; see
[the test layout](../TESTING.md#layout). SQLx creates and migrates an isolated
database per Rust test in the PostgreSQL fixture.

The dashboard checks are in the [dashboard guide](dashboard.md). The k3s
fixture starts its own digest-pinned k3s container with a temporary kubeconfig,
never reads an existing kubecontext, and removes the container afterwards.
The end-to-end cases need the three images, which their fixtures build (or
take saved with `--prebuilt`), k3d v5.9.0 named by `HERMES_TEST_K3D`, and
Helm. They install the chart with an in-cluster Keycloak and a fake
OpenAI-compatible model (`tests/support/fake_model.py`), run a digest-pinned
registry behind basic authentication (`tests/support/registry_proxy.py`) next
to a cluster, pause a platform's run-owned PostgreSQL to prove the database
outage bound, and restore onto a second disposable cluster. Every fixture is
labelled with its run and removed by exact identity, also after a failure.
Keep fixture credentials out of logs and release inputs.
