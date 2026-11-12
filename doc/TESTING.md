# Testing

The repository runs Rust, Python and TypeScript tests, plus disposable Kubernetes
and browser fixtures. [SPEC.md](SPEC.md), [ARCH.md](ARCH.md) and the
[OpenAPI contracts](../contracts/hermes-manager/) define product behaviour.
The [coverage assessment](testing/coverage.md) records the evidence and its
limits; the [gap register](testing/gaps.md) holds test work still to do.

The runnable commands are in the
[implementation guide](personal-hermes/implementation.md#verification-commands),
the [dashboard guide](personal-hermes/dashboard.md#frontend-development-and-checks) and the
[case verifier guide](../tests/verification/README.md). These commands use
fixtures, never deployment data or a deployment kubecontext.

## Evidence records

| Record | Contents | Enforcement |
| --- | --- | --- |
| [`TESTS.md`](../services/hermes-manager/TESTS.md) | Requirement, acceptance, bound and composite contract-operation rows; named tests, levels and status | Architecture tests compare row sets with the specification and contracts and check that named functions exist |
| [`cases.json`](../tests/verification/cases.json) | Exact Python and compiled Rust test identities, proving levels and execution profiles; explicitly uncollected helpers | `tests.support.verification` compares actual collection and execution against the reviewed inventory |

A `pass` traceability row identifies cited examples. It is neither exhaustive
clause coverage nor an execution receipt for a candidate. JSON and JUnit reports
from the verifier record the outcomes of a particular profile run. The coverage
assessment and gap register review these records rather than duplicating the
case inventory.

## Levels and ownership

Tests are classified by the boundary they exercise. Real PostgreSQL, TLS, SSH,
process, browser and Kubernetes tests are integrations even when they live in a
Rust library test target.

| Level | Implementation | Boundary |
| --- | --- | --- |
| Unit | Cargo, pytest and Vitest | Pure validators, planners, state transitions, command decisions with fakes, configuration and UI logic |
| Integration | Cargo and pytest; TypeScript Playwright for presentation workflows | Store contracts, real protocol listeners, process failures, runtime images, chart/render comparisons, admission/RBAC and production dashboard/CSP |
| E2E | Independently collected pytest cases and the standalone platform script | Helm-installed manager, gateway, Hermes, workers, PostgreSQL and Keycloak, with a fake model and fixture registry |

One shared Rust contract suite runs against both the in-memory and PostgreSQL
stores. Adapter-specific suites exercise other ports. There is no generic
conformance suite for every port. Finite planner state-grid and deterministic
command-sequence tests exist; no `proptest`, fuzzing or measured branch-coverage
pipeline is configured.

## Test groups

Tests are organized by component and level. `tests.run` runs any of these from
the repository root, provisions what they need once per invocation, and reports
one line per group, so a failure reads as `manager integration` or `e2e`:

```bash
uv run python -m tests.run manager unit
uv run python -m tests.run manager integration worker integration
uv run python -m tests.run manager
uv run python -m tests.run e2e
uv run python -m tests.run e2e --part 1
uv run python -m tests.run all
```

| Component | What it covers | Unit | Integration |
| --- | --- | --- | --- |
| `manager` | API and controller crates; published contracts | Cargo, pytest | PostgreSQL, processes, a disposable Kubernetes API |
| `gateway` | `hermes-proxy`, `hermes-gateway`, gateway TLS | Cargo | PostgreSQL and real listeners |
| `dashboard` | React and TypeScript in `services/hermes-manager/ui` | Vitest | Playwright, and the production build under the real CSP |
| `hermes` | What the project adds to the upstream image | pytest | The built image, and real Keycloak login behaviour the design depends on |
| `worker` | The `agent-runtime` image | — | The worker image on a disposable Kubernetes API |
| `chart` | `charts/agent-platform` | — | Helm render against the renderer's output |
| `tooling` | `agent_platform/` and the test harness | pytest | Docker ownership and the verifier's own runner |

`e2e` is one suite over everything: the Helm-installed platform on a disposable
cluster. Upstream Hermes has no unit tests here; it is used as released, and
what the project changes in its image is tested.

`--part 1` and `--part 2` each run half of the `e2e` scenarios on a platform of
their own, and CI runs them on two runners. The log and credential checks run in
both parts.

Each group is backed by one or more verifier suites named `<component>-<level>`.
A suite ending in `-cluster` needs a Kubernetes fixture, which `tests.run`
provides. `e2e-destructive` needs a platform of its own.

`inventory` checks collection only. `run` also requires every reviewed case to
pass and the underlying runner to exit successfully. Empty collection,
missing/unexpected cases, wrong levels, skips, xfail/xpass and fixture failures
fail the suite. Rust inventory compiles and lists test binaries without
starting their external fixtures. Required execution inputs are checked before
running a suite.

A plain `uv run pytest -q` selects every Python test that needs no
infrastructure. Every Python test has exactly one level marker; collection rejects
missing or ambiguous levels. `pyproject.toml` registers capability markers and
enables strict marker/configuration checks. Requirement/case markers are not
comprehensively mapped.

## Cluster fixtures

There are two cluster execution paths:

- `tests.personal_hermes.kubernetes` starts direct single-server k3s by default,
  with a private temporary kubeconfig. It runs the Rust adapter/admission and
  controller process profiles. Its optional `--k3d` path creates a k3d cluster.
  `--native` then installs the chart and runs the sequential native scenario.
- `tests/support/cluster.py` creates run-owned k3d clusters for pytest. The
  session `cluster` fixture has one server and two agents and proves
  NetworkPolicy enforcement with a working allowed connection and a denied
  connection before handing the cluster to a case. The installed `platform`
  fixture has a separate single-node cluster because its local static volumes
  and dynamic storage must be mountable together on one node.

Both paths pin the k3s image and keep kubeconfigs private. The pytest path
requires k3d v5.9.0 explicitly through `HERMES_TEST_K3D` and disables changes to
the default kubeconfig. Fixture images are built/imported locally or pulled
from pinned references. Fixture hostnames use a generated CA. The real-browser
helper trusts that CA in a private NSS database and uses explicit resolver
rules; it disables Chromium's Local Network Access checks for the private
fixture address.

The restore helper creates a second direct-k3s cluster, including when called
from a pytest case whose source platform uses k3d. It restores a PostgreSQL dump
and an archive of static fixture storage, runs `rebind-cluster`, and checks
identity/material retention and orphan reporting. It does not establish
recovery of dynamic private-home data across provisioners. No live NFS fixture
or multi-node acceptance run of the installed platform exists.

## Fixture lifecycle and isolation

`tests/support/ownership.py` checks resource kind and run labels before reset,
injection or cleanup. Database cleanup checks run ownership comments. The
`Cleanup` helper registers finalizers as resources are allocated and attempts
all registered cleanup actions even after a failure. Docker inspection failures
are errors, never evidence that a resource is safe to remove.

The non-destructive pytest cases share a commissioned platform and create
additional assignments as needed. Teardown suspends extra assignments to free
Pods while retaining data. They do not have separate installation/database state
per case. `HERMES_TEST_ORDER=reverse` reverses collection order for diagnosis.

Destructive cases take turns on their own session platform pool. After each
case the pool checks health and replaces a failed or unusable platform before
another case takes it. This pool is separate from the non-destructive platform.
The standalone native script has a sequential scenario order and shares its
run's mutable state. It pauses its PostgreSQL fixture for an outage case, so
only one such run may use that fixture at a time. `--keep` and `--reuse` support
local diagnosis with verified fixture resources.

## Tests and proving boundaries

| Area | Existing cases and limits |
| --- | --- |
| Commands and stores | Replay/conflict, stale revisions, concurrency, rollback, access changes, secret references, leases and controller state; memory and PostgreSQL share the contract cases |
| Protocols | Administration, execution, resolver and gateway fixtures check declared responses and selected malformed headers/bodies, body bounds, authentication, CSRF and wrong-owner/wrong-interface refusals; complete operation/status/header coverage remains in G21 |
| Storage and initialization | Approved roots, traversal, static sharing, read-only mounts, mount-path compatibility, dynamic claims, private-home isolation, retained state and interrupted initialization; live NFS, more permission variants and private-home restore remain gaps |
| Worker identity | Domain range validation, backend UID/GID/fsGroup rendering, real SSH against the declared UID, a session refused on every login, worker/Hermes admission cases, and a native scenario in which the default image, a specialized image and an identity-ignoring custom image serve one backend-only folder |
| Browser | TypeScript workflows use mocked APIs; the Rust production browser case uses real assets/CSP with fixture sessions; pytest browser cases use the installed chart and Keycloak |
| Failures and bounds | API/controller fault injection, leader turnover, worker loss, provider/database outages and selected timing boundaries; no full interleaving, capacity or sustained soak claim |

The standalone native script and pytest acceptance wrappers share scenario
helpers. Pytest provides individual node IDs and reports for those wrappers;
the standalone script reports aggregate progress and stops at its first failure.
The [coverage assessment](testing/coverage.md) identifies the uncovered
variants without treating helper existence as a passing result.

## Reports and diagnostics

Verifier reports contain source revision, dirty-tree status, catalogue digest,
profile, start/end times and per-case outcomes. Python records each case's aggregate setup/call/teardown duration; Rust per-case
durations are not measured. A sibling JUnit XML report
is written for execution. Captured output, environments and failure bodies are
omitted; unreviewed identities are hashed before reporting. CI uploads these
reports with seven-day retention.

These reports do not contain a complete tested artifact-digest manifest or
exhaustive clause/actor/header/timing coverage. The standalone native run and
TypeScript suites retain their own output; TypeScript identities are outside
the reviewed case ledger. Timing cases have explicit waits and observation
budgets, with incomplete production-boundary coverage recorded in G06.

## CI and releases

| Workflow | Trigger and execution |
| --- | --- |
| `Checks` (`checks.yml`) | Pull requests run the fast groups; release tags and manual dispatch run every group. Pushes to main run no checks for now. Each image is built once by the `images` job, and the jobs that need it test that build. `e2e` runs in two parts on separate runners. Manual dispatch also runs `cluster scenarios` (the `e2e` and `e2e-destructive` profiles, including the network matrix and installed browser journeys) on the same builds |
| `publish release` (job of `Checks`) | Version tags only, after every test job passed: verifies the version and that the tested images carry this commit and version, checks GHCR push access, pushes those images and then their chart, and creates the GitHub Release |

Changes are tested locally with `tests.run` before they are merged, and the release tag runs the suite again, so pushes to main run no checks. The cluster scenarios run only when dispatched by hand; they gate neither pull requests nor releases. There is no scheduled
extended/NFS/soak workflow. A release publishes the images its own run tested;
the publish job adds image and packaged-chart validation. G20 records the remaining artifact evidence; G22
records the remaining profile and reporting work.
