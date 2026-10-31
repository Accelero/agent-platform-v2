import { useEffect, useRef } from "react";
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  hoverTooltip,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import {
  autocompletion,
  completionKeymap,
  startCompletion,
  type Completion,
} from "@codemirror/autocomplete";
import { forceLinting, linter, lintGutter } from "@codemirror/lint";
import { yaml } from "@codemirror/lang-yaml";
import {
  parseConfiguration,
  configurationRange,
  type ConfigurationFormat,
} from "./yaml";
import {
  helpFor,
  keyAt,
  noHints,
  suggestAt,
  type Help,
  type Hints,
  type Suggestion,
  type Suggestions,
} from "./schema-help";
import type { CatalogCollection, ConfigurationProblem } from "../../api";

/** What the editor says about one setting, as DOM so that no text is markup. */
function helpElement(help: Help): HTMLElement {
  const make = (tag: string, className: string, text?: string) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const root = make("div", "cm-help");
  const head = make("div", "cm-help-head");
  head.append(
    make("code", "cm-help-name", help.name),
    make("span", "cm-help-type", help.type),
  );
  if (help.required) head.append(make("span", "cm-help-tag", "required"));
  root.append(head);
  if (help.description)
    root.append(make("p", "cm-help-text", help.description));
  if (help.facts.length) {
    const facts = make("ul", "cm-help-facts");
    for (const fact of help.facts) facts.append(make("li", "", fact));
    root.append(facts);
  }
  if (help.settings.length)
    root.append(
      make(
        "p",
        "cm-help-settings",
        `Settings: ${help.settings.join(", ")}${help.settings.some((s) => s.endsWith("*")) ? " (* required)" : ""}`,
      ),
    );
  return root;
}

/** Puts a suggestion into the text: a key opens its value, a value just lands. */
function accept(
  view: EditorView,
  suggestion: Suggestion,
  found: Suggestions,
  format: ConfigurationFormat,
  from: number,
  to: number,
) {
  const finish = (insert: string, cursor: number, more: boolean) => {
    view.dispatch({
      changes: { from, to, insert },
      selection: { anchor: from + cursor },
      userEvent: "input.complete",
    });
    if (more) startCompletion(view);
  };
  if (suggestion.kind === "value")
    return finish(suggestion.text, suggestion.text.length, false);
  const name =
    format === "json" ? JSON.stringify(suggestion.label) : suggestion.label;
  // Only the name is replaced when the key already has its colon.
  if (found.keyContinues) return finish(name, name.length, false);
  if (format === "json") {
    const open = { object: "{}", list: "[]", text: '""', scalar: "" }[
      suggestion.shape
    ];
    const insert = `${name}: ${open}`;
    return finish(
      insert,
      insert.length - (open ? 1 : 0),
      !open || open === '""',
    );
  }
  const pad = " ".repeat(found.indent);
  if (suggestion.shape === "object") {
    const insert = `${name}:\n${pad}  `;
    return finish(insert, insert.length, true);
  }
  if (suggestion.shape === "list") {
    const insert = `${name}:\n${pad}  - `;
    return finish(insert, insert.length, true);
  }
  return finish(`${name}: `, name.length + 2, true);
}

export default function RawEditor({
  collection,
  value,
  format = "json",
  onChange,
  problems = [],
  hints = noHints,
}: {
  collection: CatalogCollection;
  value: string;
  format?: ConfigurationFormat;
  onChange: (value: string) => void;
  problems?: ConfigurationProblem[];
  /** What this installation has: storage sources and admin secrets. */
  hints?: Hints;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const editor = useRef<EditorView>(null);
  const changed = useRef(onChange);
  changed.current = onChange;
  const serverProblems = useRef(problems);
  serverProblems.current = problems;
  const installation = useRef(hints);
  installation.current = hints;
  useEffect(() => {
    const view = new EditorView({
      parent: parent.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          keymap.of([...completionKeymap, ...defaultKeymap, ...historyKeymap]),
          format === "json" ? json() : yaml(),
          lintGutter(),
          ...(format === "json" ? [linter(jsonParseLinter())] : []),
          linter((view) => {
            const text = view.state.doc.toString();
            const parsed = parseConfiguration(text, format);
            if (parsed.error)
              return [
                {
                  from: parsed.offset ?? 0,
                  to: Math.min((parsed.offset ?? 0) + 1, text.length),
                  severity: "error",
                  message: parsed.error,
                },
              ];
            // Only syntax is checked here; every other finding is the
            // manager's, shown after Validate.
            return serverProblems.current.map((problem) => ({
              ...configurationRange(text, problem.pointer, format),
              severity: problem.severity,
              message: problem.message,
            }));
          }),
          EditorView.cspNonce.of(
            document.querySelector<HTMLMetaElement>(
              'meta[name="manager-style-nonce"]',
            )?.content ?? "",
          ),
          EditorView.contentAttributes.of({
            "aria-label": `Configuration ${format.toUpperCase()} editor`,
          }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) changed.current(update.state.doc.toString());
          }),
          autocompletion({
            interactionDelay: 0,
            // Matching is done here: a JSON key being typed starts with a quote.
            override: [
              (context) => {
                const text = context.state.doc.toString();
                const found = suggestAt(
                  collection,
                  text,
                  context.pos,
                  format,
                  installation.current,
                );
                if (!found?.suggestions.length) return null;
                const typed = text
                  .slice(found.from, context.pos)
                  .replace(/^"/, "")
                  .toLowerCase();
                if (!context.explicit && !typed && !found.value) return null;
                const rank = (s: Suggestion) =>
                  (s.label.toLowerCase().startsWith(typed) ? 0 : 1) * 100 -
                  s.boost;
                const options: Completion[] = found.suggestions
                  .filter((s) => s.label.toLowerCase().includes(typed))
                  .sort((a, b) => rank(a) - rank(b))
                  .map((s) => ({
                    label: s.label,
                    detail: s.detail,
                    type: s.kind === "key" ? "property" : "constant",
                    info: s.help ? () => helpElement(s.help!) : undefined,
                    apply: (view, _completion, from, to) =>
                      accept(view, s, found, format, from, to),
                  }));
                return options.length
                  ? { from: found.from, to: found.to, options, filter: false }
                  : null;
              },
            ],
          }),
          hoverTooltip(
            (view, pos) => {
              const hit = keyAt(view.state.doc.toString(), pos, format);
              const help = hit && helpFor(collection, hit.path);
              if (!hit || !help) return null;
              return {
                pos: hit.from,
                end: hit.to,
                above: true,
                create: () => ({ dom: helpElement(help) }),
              };
            },
            { hoverTime: 250 },
          ),
        ],
      }),
    });
    editor.current = view;
    return () => {
      view.destroy();
      editor.current = null;
    };
  }, [format, collection]);
  // The manager's findings arrive after the editor was last linted.
  useEffect(() => {
    if (editor.current) forceLinting(editor.current);
  }, [problems]);
  useEffect(() => {
    const view = editor.current;
    if (view && view.state.doc.toString() !== value)
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: value },
      });
  }, [value]);
  return <div className="raw-editor" ref={parent} />;
}
