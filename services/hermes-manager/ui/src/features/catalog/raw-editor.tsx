import { useEffect, useRef } from "react";
import { EditorState, RangeSetBuilder, StateEffect } from "@codemirror/state";
import {
  HighlightStyle,
  syntaxHighlighting,
  syntaxTree,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  hoverTooltip,
  keymap,
  lineNumbers,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import {
  autocompletion,
  completionKeymap,
  startCompletion,
  type Completion,
} from "@codemirror/autocomplete";
import { linter, lintGutter } from "@codemirror/lint";
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

/** Sent when the manager's findings change, so that the editor draws them. */
const findingsArrived = StateEffect.define<null>();

/** Keys, strings, numbers, keywords and comments in the dashboard's palette. */
const highlighting = syntaxHighlighting(
  HighlightStyle.define([
    {
      tag: [tags.propertyName, tags.definition(tags.propertyName)],
      color: "var(--ink)",
      fontWeight: "600",
    },
    { tag: tags.string, color: "var(--syn-string)" },
    { tag: tags.number, color: "var(--accent)" },
    {
      tag: [tags.bool, tags.null, tags.atom, tags.keyword],
      color: "var(--syn-keyword)",
    },
    { tag: tags.comment, color: "var(--muted)", fontStyle: "italic" },
    {
      tag: [
        tags.punctuation,
        tags.separator,
        tags.bracket,
        tags.operator,
        tags.meta,
      ],
      color: "var(--graphite)",
    },
    { tag: tags.invalid, color: "var(--bad)" },
  ]),
);

/**
 * The YAML grammar tags every plain value alike, so numbers, true, false and
 * null would read as text. This tells them apart by what they say.
 */
function scalarKind(text: string) {
  if (/^[-+]?\d[\d_]*(\.\d+)?([eE][-+]?\d+)?$/.test(text)) return "number";
  if (/^(true|false|null|~)$/.test(text)) return "keyword";
  return "text";
}
function markScalars(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  for (const { from, to } of view.visibleRanges)
    syntaxTree(view.state).iterate({
      from,
      to,
      enter: (node) => {
        if (node.name !== "Literal" || node.node.parent?.name === "Key") return;
        builder.add(
          node.from,
          node.to,
          Decoration.mark({
            class: `cm-scalar-${scalarKind(view.state.sliceDoc(node.from, node.to))}`,
          }),
        );
      },
    });
  return builder.finish();
}
const yamlScalars = [
  ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = markScalars(view);
      }
      update(update: ViewUpdate) {
        if (
          update.docChanged ||
          update.viewportChanged ||
          syntaxTree(update.startState) !== syntaxTree(update.state)
        )
          this.decorations = markScalars(update.view);
      }
    },
    { decorations: (plugin) => plugin.decorations },
  ),
  EditorView.baseTheme({
    ".cm-scalar-number": { color: "var(--accent)" },
    ".cm-scalar-keyword": { color: "var(--syn-keyword)" },
    ".cm-scalar-text": { color: "var(--syn-string)" },
  }),
];

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
          format === "json" ? json() : [yaml(), yamlScalars],
          highlighting,
          lintGutter(),
          ...(format === "json" ? [linter(jsonParseLinter())] : []),
          linter(
            (view) => {
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
            },
            {
              delay: 100,
              needsRefresh: (update) =>
                update.transactions.some((transaction) =>
                  transaction.effects.some((effect) =>
                    effect.is(findingsArrived),
                  ),
                ),
            },
          ),
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
    editor.current?.dispatch({ effects: findingsArrived.of(null) });
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
