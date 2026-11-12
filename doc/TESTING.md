# Testing

The repository has one pytest suite. Rust, TypeScript and browser tests keep
their own runners (Cargo, Vitest, Playwright), and pytest runs them through
thin wrappers, so one command and one marker expression select any test.
[SPEC.md](SPEC.md), [ARCH.md](ARCH.md) and the
[OpenAPI contracts](../contracts/hermes-manager/) define product behaviour.
The [coverage assessment](testing/coverage.md) records the evidence and its
limits; the [gap register](testing/gaps.md) holds test work still to do.

Tests use disposable fixtures only, never deployment data or a deployment
kubecontext.

## Running tests

```bash
uv run pytest                                   # no infrastructure or toolchain
uv run pytest -m unit                           # every unit test, Rust and Vitest too
uv run pytest -m "manager and integration"
uv run pytest -m "manager or gateway or dashboard or worker"
uv run pytest -m "hermes and image"
uv run pytest -m e2e                            # the installed platform
uv run pytest -m e2e --shard 2/5                # one of five balanced parts
```

A plain `uv run pytest` runs every test that needs no Docker fixture, no
cluster and no Rust or Node toolchain. A `-m` expression replaces that default
selection. Rust and dashboard tests also run with their own tools in their
service directories (`cargo test`, `npm test`), as the
[manager guide](../services/hermes-manager/README.md) and the
[dashboard guide](personal-hermes/dashboard.md#frontend-development-and-checks)
describe.

## Layout

Unit and integration tests belong to their component and live with it.
`tests/` holds what spans components: the end-to-end suite, shared fixtures,
the pytest wrappers for other languages and the tests of the harness itself.

| Component | Marker | Unit | Integration |
| --- | --- | --- | --- |
| Manager API and controller | `manager` | Rust crate tests (`tests/services/test_manager.py`); contract checks in `contracts/hermes-manager/tests/` | Rust tests on PostgreSQL, real listeners and processes, and on a disposable k3s |
| Gateway | `gateway` | `hermes-proxy` (`tests/services/test_gateway.py`) | Gateway, resolver client and TLS on PostgreSQL |
| Dashboard | `dashboard` | Vitest and the generated-code checks (`tests/services/test_dashboard.py`) | Playwright workflows, and the production build under the manager's CSP |
| Hermes image additions | `hermes` | `runtimes/hermes/tests/unit/` | `runtimes/hermes/tests/integration/`: the initializer, and the built image with real Keycloak login |
| Worker image | `worker` | — | The worker on a disposable k3s (`tests/services/test_worker.py`) |
| Chart | `chart` | — | `charts/tests/`: Helm render against the renderer |
| Tooling | `tooling` | `agent_platform/tests/`, `tests/harness/` | Docker fixture ownership (`tests/harness/`) |

`tests/e2e/` covers the whole installed system and carries no component
marker. Upstream Hermes is used as released; only what the project changes in
its image is tested.

## Markers and collection rules

Every test carries exactly one level marker: `unit` (no external service),
`integration` (a real dependency, image or process boundary) or `e2e` (the
Helm-installed system). Unit and integration tests belong to exactly one
component; a test that lives with its component gets the marker from its
location, and a wrapper names its own. Collection fails otherwise.

Capability markers say what a test needs: `database`, `cluster`, `image`,
`browser`, `destructive` (exclusive disposable infrastructure), `rust` and
`node` (the toolchains). `security` marks authorization and isolation
boundaries.

A skipped, xfailed or xpassed test fails the run, and so does an empty
selection: a test whose fixture is missing has not passed.

A Rust wrapper case is one `cargo test` target. It passes only when Cargo
succeeds, at least one Rust test ran and libtest ignored none: tests marked
`#[ignore]` for needing a fixture run with `--include-ignored` where the
fixture exists. A failure shows libtest's failure section, which names the
Rust test.

[`TESTS.md`](../services/hermes-manager/TESTS.md) maps requirement,
acceptance, bound and contract-operation rows to named tests. The
`hermes-architecture` traceability tests compare its row sets with the
specification and contracts and check that every named test exists in the
Rust sources or the Python test directories. A `pass` row identifies cited
examples, not exhaustive clause coverage.

One shared Rust contract suite runs against both the in-memory and PostgreSQL
stores. Adapter-specific suites exercise other ports. There is no generic
conformance suite for every port. Finite planner state-grid and deterministic
command-sequence tests exist; no `proptest`, fuzzing or measured branch-coverage
pipeline is configured.

## Fixtures

The session fixtures in `tests/support/fixtures.py` start when the first test
that needs them runs and are removed at the end of the session:

| Fixture | Provides |
| --- | --- |
| `hermes_image`, `manager_image`, `worker_image` | The images under test, each built once from its reviewed release context (`tests/support/images.py`). With `--prebuilt`, the images already built or saved in `HERMES_TEST_IMAGE_ARCHIVES` are tested and nothing is built |
| `postgres` | A disposable PostgreSQL published on loopback |
| `kubernetes` | A direct single-server k3s next to that PostgreSQL, with the release worker imported, for the Rust cluster tests |
| `ui_dependencies`, `ui_dist` | The dashboard's locked dependencies, installed with the pinned Node, and its production build |

The end-to-end fixtures in `tests/e2e/conftest.py` use run-owned k3d clusters
(`tests/support/cluster.py`). k3d v5.9.0 is named explicitly by
`HERMES_TEST_K3D`, and changes to the default kubeconfig are disabled.

- `cluster` has one server and two agents. It proves NetworkPolicy
  enforcement with a working allowed connection and a denied connection
  before handing the cluster to a case.
- `platform` is the installed platform with two commissioned owners, on a
  single-node cluster of its own, because its local static volumes and
  dynamic storage must be mountable together on one node
  (`tests/support/platform.py`, built from `tests/support/system.py`).
- `destructive_platform` takes a platform from a pool that destructive cases
  share in turn. After each case the pool checks health and replaces a failed
  or unusable platform before another case takes it.
- `chromium` is a real browser that trusts the fixture CA in a private NSS
  database and reaches only the platform's hostnames through explicit resolver
  rules; it disables Chromium's Local Network Access checks for the private
  fixture address.

The non-destructive cases share one commissioned platform and create further
assignments as needed. Teardown suspends the extra assignments to free Pods
while keeping their data; the cases have no separate installation or database
state. `HERMES_TEST_ORDER=reverse` reverses the collection order to show that
cases do not depend on each other.

The restore case creates a second, direct-k3s cluster. It restores a
PostgreSQL dump and an archive of static fixture storage, runs
`rebind-cluster`, and checks identity/material retention and orphan reporting.
It does not establish recovery of dynamic private-home data across
provisioners. No live NFS fixture or multi-node acceptance run of the
installed platform exists.

Every cluster pins its k3s image and keeps its kubeconfig private. Fixture
images are built and imported locally or pulled from pinned references.
Fixture hostnames use a generated CA.

`tests/support/ownership.py` checks resource kind and run labels before reset,
injection or cleanup. Database cleanup checks run ownership comments. The
`Cleanup` helper registers finalizers as resources are allocated and attempts
all registered cleanup actions even after a failure. Docker inspection failures
are errors, never evidence that a resource is safe to remove.

## Splitting the end-to-end suite

`--shard K/N` (`tests/support/shards.py`) runs part K of N of the selected
tests. Whole files go to one part, so a file's cases share that part's
fixtures. The parts are balanced by the seconds per file recorded in
`tests/e2e/durations.json`, plus the setup of each session fixture a part
needs for the first time. Every runner computes the same split.

## Tests and proving boundaries

| Area | Existing cases and limits |
| --- | --- |
| Commands and stores | Replay/conflict, stale revisions, concurrency, rollback, access changes, secret references, leases and controller state; memory and PostgreSQL share the contract cases |
| Protocols | Administration, execution, resolver and gateway fixtures check declared responses and selected malformed headers/bodies, body bounds, authentication, CSRF and wrong-owner/wrong-interface refusals; complete operation/status/header coverage remains in G21 |
| Storage and initialization | Approved roots, traversal, static sharing, read-only mounts, mount-path compatibility, dynamic claims, private-home isolation, retained state and interrupted initialization; live NFS, more permission variants and private-home restore remain gaps |
| Worker identity | Domain range validation, backend UID/GID/fsGroup rendering, real SSH against the declared UID, a session refused on every login, worker/Hermes admission cases, and a case in which the default image, a specialized image and an identity-ignoring custom image serve one backend-only folder |
| Browser | TypeScript workflows use mocked APIs; the Rust production browser case uses real assets/CSP with fixture sessions; pytest browser cases use the installed chart and Keycloak |
| Failures and bounds | API/controller fault injection, leader turnover, worker loss, provider/database outages and selected timing boundaries; no full interleaving, capacity or sustained soak claim |

The [coverage assessment](testing/coverage.md) identifies the uncovered
variants without treating helper existence as a passing result.

## Reports

Pytest writes a JUnit XML report per CI job (`--junitxml`), with each case's
outcome and duration; captured output is not included. CI uploads the reports
with seven-day retention. A Rust wrapper case reports one outcome for its
target; Rust per-test durations are not measured. The reports do not contain
a tested artifact-digest manifest or exhaustive clause/actor/header/timing
coverage. Timing cases have explicit waits and observation budgets, with
incomplete production-boundary coverage recorded in G06.

## CI and releases

| Job of `Checks` | Runs |
| --- | --- |
| `tooling, chart and hermes` | Lint, privacy preflight, Helm lint and render, and `-m "(tooling or chart or hermes) and not image"` |
| `manager, gateway, dashboard and worker` | `cargo fmt` and `clippy`, and `-m "manager or gateway or dashboard or worker"` |
| `build images` | Each image once (`python -m tests.support.images`), labelled with the commit and version, saved as an archive for the jobs below |
| `hermes image` | `-m "hermes and image" --prebuilt` on the saved Hermes image |
| `e2e part 1`–`5` | `-m e2e --prebuilt --shard K/5` on the saved images |
| `publish release` | Version tags only, after every test job passed: verifies the version and that the tested images carry this commit and version, checks GHCR push access, pushes those images and then their chart, and creates the GitHub Release |

Pull requests run the first two jobs. Release tags and manual dispatch run
every job; only a tag publishes. Changes are tested locally before they are
merged and the release tag runs the suite again, so pushes to main run no
checks. There is no scheduled extended/NFS/soak workflow. A release publishes
the images its own run tested. G20 records the remaining artifact evidence;
G22 records the remaining profile and reporting work.
