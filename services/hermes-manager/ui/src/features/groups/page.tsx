import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { api } from "../../api";
import { useActorKey } from "../../app/state";
import { Command } from "../../components/command";
import {
  CopyId,
  DataTable,
  ListControls,
  ReadState,
  usePager,
} from "../../components/data";

export function GroupsPage() {
  const actor = useActorKey();
  const pager = usePager();
  const groups = useQuery({
    queryKey: [actor, "groups", pager.after, pager.filters.q],
    queryFn: ({ signal }) => api.groups(pager.after, signal, pager.filters.q),
  });
  return (
    <>
      <p>
        Groups collect identities for explicit agent grants. Membership alone
        does not grant access or create an agent.
      </p>
      <ListControls search="Search groups" />
      <ReadState
        pending={groups.isPending}
        error={groups.error}
        stale={groups.isRefetchError}
      >
        {groups.data && (
          <>
            <DataTable
              caption="Groups"
              rows={groups.data.items}
              empty="No groups match."
              columns={[
                {
                  header: "Group",
                  cell: ({ row }) => (
                    <Link to="/groups/$id" params={{ id: row.original.id }}>
                      {row.original.displayName}
                    </Link>
                  ),
                },
                { header: "Name", accessorKey: "name" },
                { header: "Version", accessorKey: "version" },
              ]}
            />
            {pager.controls(groups.data.nextCursor)}
          </>
        )}
      </ReadState>
      <Command
        permission="administer"
        title="Create group"
        method="POST"
        path="/api/v1/access-groups"
        fields={[
          {
            key: "name",
            label: "Name",
            hint: "Lowercase letters, digits and hyphens; start with a letter.",
          },
          { key: "displayName", label: "Display name" },
        ]}
      />
    </>
  );
}

export function GroupDetail() {
  const { id } = useParams({ from: "/groups/$id" });
  const actor = useActorKey();
  const pager = usePager();
  const group = useQuery({
    queryKey: [actor, "group", id],
    queryFn: ({ signal }) => api.group(id, signal),
  });
  const members = useQuery({
    queryKey: [actor, "group-members", id, pager.after],
    queryFn: ({ signal }) => api.groupMembers(id, pager.after, signal),
  });
  const path = `/api/v1/access-groups/${encodeURIComponent(id)}`;
  return (
    <ReadState
      pending={group.isPending}
      error={group.error}
      stale={group.isRefetchError}
    >
      {group.data && (
        <>
          <section className="panel detail">
            <h2>{group.data.data.displayName}</h2>
            <dl>
              <dt>Name</dt>
              <dd>{group.data.data.name}</dd>
              <dt>Group</dt>
              <dd>
                <CopyId value={id} />
              </dd>
              <dt>Version</dt>
              <dd>{group.data.data.version}</dd>
            </dl>
            <div className="actions">
              <Command
                permission="administer"
                title="Edit group"
                method="PATCH"
                path={path}
                etag={group.data.etag}
                initial={{
                  name: group.data.data.name,
                  displayName: group.data.data.displayName,
                }}
                fields={[
                  { key: "name", label: "Name" },
                  { key: "displayName", label: "Display name" },
                ]}
              />
              <Command
                permission="administer"
                title="Delete group"
                method="DELETE"
                path={path}
                etag={group.data.etag}
                bodyless
                warning="A group with agent grants cannot be deleted. Remove its grants first."
              />
            </div>
          </section>
          <section className="panel detail">
            <h2>Members</h2>
            <p>
              Removing a member ends their access through this group and closes
              affected connections. Their work inside an agent remains.
            </p>
            <ReadState
              pending={members.isPending}
              error={members.error}
              stale={members.isRefetchError}
            >
              {members.data && (
                <>
                  <DataTable
                    caption="Group members"
                    rows={members.data.items}
                    empty="No members."
                    columns={[
                      {
                        header: "Subject",
                        cell: ({ row }) => row.original.identity.subject,
                      },
                      {
                        header: "Issuer",
                        cell: ({ row }) => row.original.identity.issuer,
                      },
                      {
                        header: "Actions",
                        cell: ({ row }) => (
                          <Command
                            permission="administer"
                            title="Remove member"
                            method="DELETE"
                            path={`${path}/members/${encodeURIComponent(row.original.identityId)}`}
                            etag={group.data!.etag}
                            bodyless
                            warning="This ends access through this group and closes affected connections. Work inside the agent remains."
                          />
                        ),
                      },
                    ]}
                  />
                  {pager.controls(members.data.nextCursor)}
                </>
              )}
            </ReadState>
            <Command
              permission="administer"
              title="Add member"
              method="POST"
              path={`${path}/members`}
              etag={group.data.etag}
              fields={[
                {
                  key: "identity",
                  label: "Identity",
                  fields: [
                    { key: "issuer", label: "Issuer" },
                    { key: "subject", label: "Subject" },
                  ],
                },
              ]}
            />
          </section>
        </>
      )}
    </ReadState>
  );
}
