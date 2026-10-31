import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ApiError,
  api,
  createIntent,
  displayProblems,
  type CatalogCollection,
  type ConfigurationProblem,
  type Result,
} from "../../api";
import {
  queryClient,
  registerPrivateReset,
  useActorKey,
  useSession,
} from "../../app/state";
import { ProblemList } from "../../components/command";
import { ErrorNotice } from "../../components/data";
import type { Draft } from "./model";
import type { Hints } from "./schema-help";
import RawEditor from "./raw-editor";
import {
  parseConfiguration,
  serializeConfiguration,
  type ConfigurationFormat,
} from "./yaml";

type Receipt = Result<"createTemplateDraft">;
/**
 * Edits one draft as JSON or YAML. Both are views of one canonical JSON
 * document; invalid text blocks switching formats and saving.
 */
export default function DraftEditor({
  collection,
  id,
  version,
  etag,
  base,
  onSaved,
}: {
  collection: CatalogCollection;
  id: string;
  /** The draft being edited; absent when this creates the next version. */
  version?: number;
  etag?: string | null;
  base: Draft;
  onSaved: (receipt: Receipt) => void;
}) {
  const session = useSession();
  const actor = useActorKey();
  // What this installation has, to suggest as values. Suggestions are an aid:
  // when these cannot be read the editor simply offers fewer.
  const sources = useQuery({
    queryKey: [actor, "storage-sources", "hints"],
    queryFn: ({ signal }) => api.storageSources(signal),
    retry: false,
  });
  const secretNames = useQuery({
    queryKey: [actor, "secrets", "hints"],
    queryFn: ({ signal }) => api.secrets(undefined, signal),
    retry: false,
  });
  const hints: Hints = {
    storageSources: (sources.data?.items ?? []).map((source) => ({
      id: source.id,
      dynamic: !!source.storageClass,
      detail: source.storageClass
        ? "storage class"
        : source.nfs
          ? "NFS export"
          : "local path",
    })),
    secrets: (secretNames.data?.items ?? []).map((secret) => ({
      name: secret.name,
      kind: secret.kind,
    })),
  };
  const [format, setFormat] = useState<ConfigurationFormat>("yaml");
  const [text, setText] = useState(() => serializeConfiguration(base, "yaml"));
  const [server, setServer] = useState<ConfigurationProblem[]>([]);
  const [submit] = useState(() => createIntent());
  useEffect(
    () =>
      registerPrivateReset(() => setText(serializeConfiguration({}, "yaml"))),
    [],
  );
  const parsed = parseConfiguration(text, format);
  const refused = server.some((problem) => problem.severity === "error");
  const validate = useMutation({
    mutationFn: () =>
      api.validate(collection, parsed.value!, session.data!.csrfToken!),
    onSuccess: (result) => setServer(displayProblems(result)),
  });
  const save = useMutation({
    mutationFn: () =>
      version === undefined
        ? submit<Receipt>(
            "POST",
            `/api/v1/${collection}/${encodeURIComponent(id)}/versions`,
            { definition: parsed.value },
            session.data!.csrfToken!,
          )
        : submit<Receipt>(
            "PATCH",
            `/api/v1/${collection}/${encodeURIComponent(id)}/versions/${version}`,
            { definition: parsed.value },
            session.data!.csrfToken!,
            etag,
          ),
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries();
      onSaved(receipt);
    },
    onError: (error) => {
      if (error instanceof ApiError) setServer(error.problems);
    },
  });
  return (
    <section className="panel detail" aria-label="Draft editor">
      <h2>
        {version === undefined
          ? `New version of ${id}`
          : `Draft ${id} v${version}`}
      </h2>
      <div className="tabs" role="tablist" aria-label="Format">
        {(["yaml", "json"] as const).map((value) => (
          <button
            key={value}
            role="tab"
            aria-selected={format === value}
            disabled={!parsed.value && format !== value}
            onClick={() => {
              if (!parsed.value || format === value) return;
              setText(serializeConfiguration(parsed.value, value));
              setFormat(value);
            }}
          >
            {value.toUpperCase()}
          </button>
        ))}
      </div>
      <RawEditor
        key={format}
        collection={collection}
        format={format}
        value={text}
        onChange={(next) => {
          setText(next);
          setServer([]);
          validate.reset();
        }}
        problems={server}
        hints={hints}
      />
      <p className="hint">
        Hover a setting to see what it does. Type to get suggestions, or press
        Ctrl+Space anywhere to list what can go there.
      </p>
      {parsed.error && <p role="alert">{parsed.error}</p>}
      <ProblemList problems={server} />
      {validate.data?.valid && !refused && (
        <p role="status">The manager accepts this definition.</p>
      )}
      {(validate.error || save.error) && (
        <ErrorNotice error={(save.error ?? validate.error)!} />
      )}
      <div className="actions">
        <button
          type="button"
          disabled={
            !parsed.value || validate.isPending || !session.data?.csrfToken
          }
          onClick={() => validate.mutate()}
        >
          Validate
        </button>
        <button
          type="button"
          disabled={!parsed.value || save.isPending || !session.data?.csrfToken}
          onClick={() => save.mutate()}
        >
          {version === undefined ? "Save as new draft" : "Save draft"}
        </button>
      </div>
      <p className="hint">
        Drafts may be incomplete. Publishing checks the definition against the
        installation's storage sources and admin secrets, then freezes it.
      </p>
    </section>
  );
}
