# Collected-case verification

[`cases.json`](cases.json) is the reviewed inventory of exact Python and Rust
test identities and their proving levels. It includes every parameter, both
instantiations of the shared store contracts, and separate feature/ignored-test
profiles. It is a baseline inventory of existing examples. Full specification
clause, actor, response, header and timing coverage remains tracked in
[G03](../../doc/testing/gaps.md#g03--trace-executed-obligations-not-function-names).

Run a group with `uv run python -m tests.run <component> <level>`; see
[the test groups](../../doc/TESTING.md#test-groups). The verifier underneath takes
one suite at a time, named `<component>-<level>`:

```bash
uv run python -m tests.support.verification inventory manager-unit --output .local/verification/manager-unit-collection.json
uv run python -m tests.support.verification run tooling-unit --output .local/verification/tooling-unit.json
```

Python execution needs Helm on `PATH` for the chart cases. A plain
`uv run pytest -q` selects only tests that need no infrastructure:
a test that needs a cluster, database, native image, browser or exclusive
infrastructure carries that capability marker and belongs to the suite that
provides it. `hermes-integration` needs `HERMES_TEST_NATIVE_IMAGE` to name the image
under test; `e2e` needs `HERMES_TEST_K3D` to name a k3d binary at the
pinned version, uses a three-node fixture for the network matrix and a separate single-node
installed platform for acceptance/browser cases. The network fixture proves
policy enforcement before its cases run; browser journeys also
need Playwright's Chromium for the locked version (`uv run playwright install
chromium`) and the NSS `certutil` tool. `e2e-destructive` runs the cases that break their platform on one of their own. All fail rather than skip
when their input is absent. Rust inventory needs
the pinned toolchain and compiles test targets, then asks their actual libtest
binaries for their collected names. Collection does not create infrastructure
or need a running database, browser or Docker daemon. Rust execution needs the
disposable PostgreSQL fixture in the
[implementation guide](../../doc/personal-hermes/implementation.md#verification-commands).
The `dashboard-integration` suite also needs the production UI build, installed UI dependencies
and Playwright Chromium, as described in the
[dashboard guide](../../doc/personal-hermes/dashboard.md).

The `hermes integration` group also collects the shared-agent Phase 0 prerequisites in
`tests/integration/hermes/test_login.py`. It builds a local derivative of the
supplied image that restores only the digest-pinned upstream OIDC provider, and
starts the pinned Keycloak on an internal Docker network with throwaway TLS.
It publishes no ports and removes its labelled containers, network and test
image. The release image's native owner policy is unchanged. These are scripted
HTTP and WebSocket clients, not browser or platform-routing acceptance.
The supplemental provider-session cases exchange real PKCE codes for the gateway
client at Keycloak and check native logout with no other client, a retained
gateway client and both clients revoked. They create no platform session and do
not replace SH-N04's refresh, cross-host revocation and sign-in requirements.

The existing `tests.personal_hermes.kubernetes` driver supplies disposable
cluster prerequisites and runs the suites named with `--suite`. Invoking
either profile directly requires its fixture environment; absent prerequisites
fail. These Rust suites use direct k3s by default; the driver also accepts `--k3d`.
The independently collected E2E scenarios use the separate Python cluster and
destructive profiles.

`inventory` verifies collection only. `run` verifies collection and execution:
all expected cases must be present, have their reviewed level, and pass.
Missing, new, skipped, xfailed, xpassed, setup-failed and teardown-failed cases
fail the gate. Cargo still executes its normal command, including doctests;
its exit code must also succeed. Deliberately ignored cases outside a Cargo
profile are excluded from that profile's baseline and required by their own
profiles. Removing the shared contract macro entry or the fault feature cannot
be satisfied by a helper function with the same name.

Reports record source revision, dirty-tree status, catalogue digest, profile,
start/end times and individual outcomes. Python records each case's aggregate setup/call/teardown duration; Rust case
durations are not measured. Execution also writes a sibling JUnit XML.
Only reviewed identities appear verbatim: unreviewed IDs are hashed so an
accidental parameter value does not enter reports. Failure bodies, captured
output, environments and credentials are omitted. CI uploads only these JSON
and XML files, with seven-day retention. A failing case can be diagnosed locally
with its ordinary `uv run pytest` or `cargo test` command; these entry points
remain available.

The obligation inventory resolves the rows in `services/hermes-manager/TESTS.md`
to exact catalogued IDs. Both logout operations use contract filename and
operation ID. Standalone native helpers appear under `uncollected` with their
source and G01 gap in the catalogue; they provide no collected-case result.
The existing native CI job continues to execute those scripts. TypeScript
unit/presentation tests retain their existing gates and are outside this
inventory. Dirty-tree reports are local diagnostic evidence, not release
eligibility for an exact source/artifact candidate.

When adding or intentionally replacing a test, inspect the actual collection,
review its assertions and proving level, and edit the catalogue in the same
change. Use behavioural parameter IDs, never input values or credentials.
`uv run pytest --collect-only -q` shows Python IDs before catalogue review;
Cargo's `--list` output supplies compiled Rust names. Review the catalogue diff
alongside the tests. There is no automatic baseline-update path in CI. Removing
an `uncollected` entry requires a collected replacement and corresponding
updates to traceability and the gap register.
