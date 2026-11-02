import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api, type Schemas } from "../../api";
import { useActorKey } from "../../app/state";
import { Command } from "../../components/command";
import { DataTable, ReadState, StatusBadge } from "../../components/data";

type Principal = Schemas["GrantPrincipal"];

export function SharedAgentAccess({ id }: { id: string }) {
  const actor = useActorKey();
  const endpoint = useQuery({
    queryKey: [actor, "endpoint", id],
    queryFn: ({ signal }) => api.endpoint(id, signal),
  });
  const grants = useQuery({
    queryKey: [actor, "grants", id],
    queryFn: ({ signal }) => api.grants(id, signal),
  });
  const base = `/api/v1/assignments/${encodeURIComponent(id)}`;
  return (
    <>
      <section className="panel detail">
        <h2>Endpoint</h2>
        <ReadState
          pending={endpoint.isPending}
          error={endpoint.error}
          stale={endpoint.isRefetchError}
        >
          {endpoint.data && (
            <>
              <dl>
                <dt>Status</dt>
                <dd>
                  <StatusBadge
                    value={endpoint.data.data.enabled ? "enabled" : "disabled"}
                  />
                </dd>
                <dt>URL</dt>
                <dd>
                  <a href={endpoint.data.data.url}>{endpoint.data.data.url}</a>
                </dd>
                <dt>Gateway callback</dt>
                <dd>
                  <code>{endpoint.data.data.gatewayCallbackUrl}</code>
                </dd>
                <dt>Native callback</dt>
                <dd>
                  <code>{endpoint.data.data.nativeCallbackUrl}</code>
                </dd>
              </dl>
              <p>
                Register both callback URLs in the identity provider. This
                status does not verify DNS, TLS or callback registration.
              </p>
              <Command
                permission="administer"
                title="Change endpoint"
                method="PATCH"
                path={`${base}/endpoint`}
                etag={endpoint.data.etag}
                initial={{
                  label: endpoint.data.data.label,
                  enabled: endpoint.data.data.enabled,
                }}
                fields={[
                  { key: "label", label: "Hostname label" },
                  { key: "enabled", label: "Enabled", type: "checkbox" },
                ]}
                warning="Renaming restarts the agent. The old URL stops working and another agent may later use it. Register the new callbacks before enabling it."
              />
            </>
          )}
        </ReadState>
      </section>
      <section className="panel detail">
        <h2>Access</h2>
        <p>
          Members share this agent and its workspace. Removing access closes
          affected connections; work inside the agent remains.
        </p>
        <ReadState
          pending={grants.isPending}
          error={grants.error}
          stale={grants.isRefetchError}
        >
          {grants.data &&
            (() => {
              const current = grants.data.data.grants;
              const etag = grants.data.etag;
              const replace = (items: Principal[]) => ({ grants: items });
              return (
                <>
                  <DataTable
                    caption="Agent grants"
                    rows={current}
                    empty="No users or groups have access."
                    columns={[
                      {
                        header: "Principal",
                        cell: ({ row }) =>
                          "identity" in row.original ? (
                            <>
                              {row.original.identity.subject}
                              <small>{row.original.identity.issuer}</small>
                            </>
                          ) : (
                            <Link
                              to="/groups/$id"
                              params={{ id: row.original.groupId }}
                            >
                              Group {row.original.groupId}
                            </Link>
                          ),
                      },
                      {
                        header: "Actions",
                        cell: ({ row }) => (
                          <Command
                            permission="administer"
                            title="Remove access"
                            method="PUT"
                            path={`${base}/grants`}
                            etag={etag}
                            initial={{}}
                            transform={() =>
                              replace(
                                current.filter(
                                  (_, index) => index !== row.index,
                                ),
                              )
                            }
                            warning="Removing this grant closes affected connections. Work inside the agent remains."
                          />
                        ),
                      },
                    ]}
                  />
                  <div className="actions">
                    <Command
                      permission="administer"
                      title="Grant user"
                      method="PUT"
                      path={`${base}/grants`}
                      etag={etag}
                      fields={[
                        { key: "issuer", label: "Issuer" },
                        { key: "subject", label: "Subject" },
                      ]}
                      transform={(value) =>
                        replace([
                          ...current,
                          {
                            identity: {
                              issuer: String(value.issuer),
                              subject: String(value.subject),
                            },
                          },
                        ])
                      }
                    />
                    <Command
                      permission="administer"
                      title="Grant group"
                      method="PUT"
                      path={`${base}/grants`}
                      etag={etag}
                      fields={[{ key: "groupId", label: "Group ID" }]}
                      transform={(value) =>
                        replace([
                          ...current,
                          { groupId: String(value.groupId) },
                        ])
                      }
                    />
                  </div>
                </>
              );
            })()}
        </ReadState>
      </section>
    </>
  );
}
