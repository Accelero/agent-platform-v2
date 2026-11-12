# Agent platform repository guidance

This repository owns generic runtime images, the Helm chart and versioned GHCR
releases. It contains no Pulumi program, cloud credentials, personal profiles,
mutable application state or deployment-specific backup scripts.

## Layout

- `agent_platform/`: shared BuildKit/cache configuration, release builder,
  privacy preflight and bounded command execution.
- `containers/docker-bake.hcl`: the three release image targets (`hermes`,
  `agent-runtime`, `hermes-manager`).
- `services/hermes-manager/`: Rust manager (`api` and `controller` roles),
  `hermes-gateway` and the React dashboard; `dev/` is the Docker Compose
  development stack (dashboard with hot reload, manager, PostgreSQL and a
  Keycloak with fixed local-only test users).
- `contracts/hermes-manager/`: OpenAPI contracts (administration, execution,
  route resolution, gateway) and template schemas.
- `runtimes/hermes/`: per-user Hermes image and the `managed_execution` plugin.
- `runtimes/agent-runtime/`: default SSH terminal worker image.
- `charts/agent-platform/`: self-contained application chart.
- `examples/manager/`: nonsecret manager render fixture.
- `tests/`: the cross-component part of the one pytest suite: `e2e/` (the
  installed platform), `services/` (pytest wrappers that run the Rust and
  dashboard test targets), `harness/` (tests of the test support) and
  `support/` (fixtures, images, clusters, the installed-platform helpers, fake
  model and registry proxy). Unit and integration tests live with their
  component: `agent_platform/tests/`, `charts/tests/`,
  `contracts/hermes-manager/tests/`, `runtimes/hermes/tests/`, and the Rust
  and TypeScript tests in `services/hermes-manager/`.
- `doc/`: `SPEC.md` (specification), `ARCH.md` (architecture) and `TESTING.md`
  (test system); the coverage assessment and gap register under `doc/testing/`;
  the guides and the implementation status under `doc/personal-hermes/`.

## Manager design

`doc/SPEC.md` (what) and `doc/ARCH.md` (how) define the manager and gateway;
keep the guides in `doc/personal-hermes/` and the implementation status there in step
with the code. Documentation describes what is built and may also record
planned work and open decisions anywhere in `doc/`. Mark every such passage
as planned (a "Planned" or "Open decisions" heading or label), keep it apart
from the built behaviour and say what must be decided or done first. Do not
write migration history. `doc/testing/gaps.md` stays the register of test work
still to do; delete an entry, or turn a plan into built-behaviour text, once its
completion criteria hold.
Manager code follows these rules:

- Every desired-state write is a `Command` run through the command envelope.
- Reconciliation is level-triggered: a pure planner, then idempotent
  server-side apply.
- A non-repeatable effect uses one of the five named mechanisms in the
  architecture. Add a new one only with a design change.
- `hermes-core` imports no I/O crate.
- Lifecycle states are typed enums, never string literals.
- No `serde_json::Value` crosses a port, except opaque `adminConfig`.
- Every requirement and acceptance row maps to tests in
  `services/hermes-manager/TESTS.md`; the `hermes-architecture` tests read the
  tables in `doc/SPEC.md` and fail on a missing row or test name.
- Contract cases in `hermes-core::app::testing::contracts` run on both the
  in-memory and the PostgreSQL store; add a port behaviour there first.

## Checks

Use Python 3.14 and `uv sync --locked`. After edits run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
helm lint charts/agent-platform
```

`doc/TESTING.md` describes the suite. Every test carries exactly one level
marker (`unit`, `integration`, `e2e`), and every unit and integration test
belongs to exactly one component; collection fails otherwise. A skip fails the
run. A test that proves a requirement is named in its `TESTS.md` row. Follow
the procedure in `doc/testing/gaps.md` when closing a test gap.

Run the Rust checks in `services/hermes-manager/README.md` after manager edits.
After changes to the controller, the chart, the runtime image or the gateway,
also run `uv run pytest -m e2e` and the affected component's integration
tests (for example `uv run pytest -m "manager and integration"`), on
disposable fixtures only.
The OpenAPI contracts are the authority for every interface. Keep the plugin's
requests conformant to `execution.openapi.yaml`; change the contract, the Rust
handler and the plugin together.

Keep imported runtime/vendor code changes focused and review their Docker build
compatibility. Generic code must not import a deployment repository. Preserve
non-root workers and scoped mounts.
Keep external commands bounded and credentials out of output and build contexts.
Review the staged diff and `git diff --check` before committing.

## Releases

Source chart and project versions stay at the `0.0.0` placeholder except in the
tagged release commit, which sets both to the tag's version; do not add
release numbers to documentation. A release requires an explicit
`vMAJOR.MINOR.PATCH` tag, or `vMAJOR.MINOR.PATCH-dev.N` for a prerelease of
unmerged work. This repository is its own product: it publishes only under
`ghcr.io/accelero/agent-platform-v2` (images and the `agent-platform-v2`
chart) with its own version line, never to the public `agent-platform`
repository's packages. Never overwrite or reuse an existing version. A version tag runs
`Checks`, including every end-to-end case, on images built once from the
tagged commit. Its `publish` job runs only after every test job passed: it
checks that the tested images carry this commit and version, confirms GHCR
accepts pushes to every package, pushes those same images and then their
self-contained chart. Nothing is rebuilt for a release. Keep the source and
version OCI labels and the upstream license notices in images. The chart owns configuration generation and exact image
defaults.

Public image layers and chart or receipt data must contain no secrets or
personal data. Treat personal prompts, identities, profiles, skills and memory
as sensitive too; keep them in the private deployment repository or live
mounted storage. Upstream public prompt templates remain part of pinned
software, separate from personal seeds. `release-policy.json` enumerates the
chart and every build input; release and cache builds stage only these reviewed
files. Never widen a context to include a deployment checkout. Run
`uv run python -m agent_platform.privacy` before publication; it reports rule
names without matched content. Keep ordinary Git attribution only with the
maintainer's approval; the existing attribution has been explicitly approved.
Publication and repository visibility changes require user authorization.

Build caches use GitHub Actions API v2 with distinct scopes per image. Only the
`images` job on pushes to main writes them, and it holds no package permission;
release builds only read them. `Checks` does not run on pushes to main for now,
so nothing currently writes them.
A cache failure must not fail a build.
Never recreate GHCR cache packages or pass cache credentials in command arguments.
Keep the default cache storage limit unless the user authorizes paid storage.
