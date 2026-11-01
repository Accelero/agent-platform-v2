import type { ReactNode } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api, type Schemas } from "../../api";
import { useActorKey } from "../../app/state";

const phases: Schemas["Phase"][] = [
  "ready",
  "provisioning",
  "blocked",
  "stopping",
  "retained",
];
const when = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/** One answer of a section: its figures, or that they could not be read. */
function Answer<T>({
  query,
  children,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
}) {
  if (query.isError) return <span className="muted">Unavailable</span>;
  if (query.isPending) return <span className="muted">Loading…</span>;
  return <>{children(query.data)}</>;
}

function Row({
  label,
  to,
  action,
  children,
}: {
  label: string;
  to: "/assignments" | "/templates" | "/terminal-backends" | "/storage";
  action: string;
  children: ReactNode;
}) {
  return (
    <li>
      <span className="ledger-label">{label}</span>
      <span className="ledger-value">{children}</span>
      <Link to={to} className="ledger-link">
        {action}
      </Link>
    </li>
  );
}

function Summary({ items }: { items: Schemas["CatalogSummary"][] }) {
  const published = items.filter((item) => item.latestPublished).length;
  const drafts = items.filter((item) => item.draft).length;
  if (!items.length) return <span className="muted">None yet</span>;
  return (
    <>
      <span>{published} published</span>
      {drafts > 0 && <span className="muted">{drafts} with an open draft</span>}
    </>
  );
}

export function Overview() {
  const actor = useActorKey();
  const assignments = useQuery({
    queryKey: [actor, "overview", "assignments"],
    queryFn: ({ signal }) => api.assignments(undefined, signal),
  });
  const templates = useQuery({
    queryKey: [actor, "overview", "templates"],
    queryFn: ({ signal }) => api.catalog("templates", undefined, signal),
  });
  const backends = useQuery({
    queryKey: [actor, "overview", "terminal-backends"],
    queryFn: ({ signal }) =>
      api.catalog("terminal-backends", undefined, signal),
  });
  const orphans = useQuery({
    queryKey: [actor, "overview", "orphans"],
    queryFn: ({ signal }) => api.orphans(signal),
  });
  const activity = useQuery({
    queryKey: [actor, "overview", "activity"],
    queryFn: ({ signal }) => api.audit(undefined, signal),
  });
  const blocked =
    assignments.data?.items.filter((item) => item.phase === "blocked") ?? [];
  return (
    <>
      <section aria-labelledby="status">
        <h2 id="status">Status</h2>
        <ul className="ledger">
          <Row label="Instances" to="/assignments" action="View assignments">
            <Answer query={assignments}>
              {({ items, nextCursor }) =>
                items.length ? (
                  <>
                    {phases
                      .map(
                        (phase) =>
                          [
                            phase,
                            items.filter((item) => item.phase === phase).length,
                          ] as const,
                      )
                      .filter(([, count]) => count > 0)
                      .map(([phase, count]) => (
                        <span key={phase} className={`status status-${phase}`}>
                          <i className="dot" aria-hidden="true" />
                          {count} {phase}
                        </span>
                      ))}
                    {nextCursor && (
                      <span className="muted">of the first {items.length}</span>
                    )}
                  </>
                ) : (
                  <span className="muted">None yet</span>
                )
              }
            </Answer>
          </Row>
          <Row label="Hermes templates" to="/templates" action="View templates">
            <Answer query={templates}>
              {({ items }) => <Summary items={items} />}
            </Answer>
          </Row>
          <Row
            label="Terminal backends"
            to="/terminal-backends"
            action="View backends"
          >
            <Answer query={backends}>
              {({ items }) => <Summary items={items} />}
            </Answer>
          </Row>
          <Row label="Cluster leftovers" to="/storage" action="View storage">
            <Answer query={orphans}>
              {({ items, scannedAt }) =>
                items.length ? (
                  <span className="status status-blocked">
                    <i className="dot" aria-hidden="true" />
                    {items.length} to review
                  </span>
                ) : (
                  <span>{scannedAt ? "None found" : "Not scanned yet"}</span>
                )
              }
            </Answer>
          </Row>
        </ul>
      </section>

      {blocked.length > 0 && (
        <section aria-labelledby="attention">
          <h2 id="attention">Needs attention</h2>
          <ul className="attention">
            {blocked.map((item) => (
              <li key={item.id}>
                <i className="dot status-blocked" aria-hidden="true" />
                <Link to="/assignments/$id" params={{ id: item.id }}>
                  {item.identity?.subject ?? item.sharedAgent?.displayName}
                </Link>
                <span className="muted">instance is blocked</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="recent">
        <div className="section-heading">
          <h2 id="recent">Recent activity</h2>
          <Link to="/activity">View all</Link>
        </div>
        <Answer query={activity}>
          {({ items }) =>
            items.length ? (
              <div className="table-scroll">
                <table>
                  <caption className="sr-only">Recent activity</caption>
                  <thead>
                    <tr>
                      <th scope="col">When</th>
                      <th scope="col">Who</th>
                      <th scope="col">Change</th>
                      <th scope="col">Target</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.slice(0, 6).map((event) => (
                      <tr key={event.id}>
                        <td className="nowrap">
                          {when.format(new Date(event.occurredAt))}
                        </td>
                        <td>
                          {event.actor.identity?.subject ?? event.actor.kind}
                        </td>
                        <td className="mono">{event.action}</td>
                        <td>
                          {event.target.kind} <code>{event.target.id}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">No changes recorded yet.</p>
            )
          }
        </Answer>
      </section>

      <p className="note">
        A change is recorded first; the controller then brings the instance to
        the new state, and its phase shows the progress.
      </p>
    </>
  );
}
