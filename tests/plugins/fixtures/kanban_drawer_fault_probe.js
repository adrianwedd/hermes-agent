// Behavioral probe for the terminal-card fault section in the browser/mobile kanban
// drawer (plugins/kanban/dashboard/dist/index.js — plain IIFE, no build step, so the
// bundle IS the source). Extracts `DiagnosticsSection` verbatim (brace-matched, not
// regex-matched) and drives it with a completed card whose payload still carries the
// diagnostics list, rather than asserting on the rendered text.
//
// What it proves:
//  1. `DiagnosticsSection` paints nothing for a terminal card, so the drawer cannot
//     show a stale fault report on finished work even from a cached payload. A live
//     card still renders its section — the carve-out is targeted, not a blanket
//     silencing.
//  2. `isDiagnosticEvent` is still defined in the bundle. It is referenced by the
//     drawer's Events list (`TaskDetail`), and a delta that removed the definition
//     while leaving the call site would throw ReferenceError on that surface. This
//     pins the binding so the class cannot regress silently.
//
// Run via:  node kanban_drawer_fault_probe.js <path-to-bundle>
const fs = require("fs");

const bundlePath = process.argv[2];
const src = fs.readFileSync(bundlePath, "utf8");

function extract(name) {
  const start = src.indexOf("function " + name);
  if (start === -1) throw new Error(name + " not found in bundle");
  const bodyStart = src.indexOf("{", start);
  let depth = 0, end = bodyStart;
  for (; end < src.length; end++) {
    if (src[end] === "{") depth++;
    else if (src[end] === "}") { depth--; if (depth === 0) break; }
  }
  return src.slice(start, end + 1);
}

// Pull a module-scoped `const NAME = { ... };` object literal out of the bundle, so the
// extracted functions read the SAME table the shipped one does rather than a copy. Emitted
// as `var` because a direct eval's `const` is scoped to the eval, while its `var` and
// function declarations reach this file's scope — which is what lets `isDiagnosticEvent`
// (itself extracted and eval'd) read the table.
function extractConst(name) {
  const start = src.indexOf("const " + name + " = ");
  if (start === -1) throw new Error(name + " not found in bundle");
  const bodyStart = src.indexOf("{", start);
  let depth = 0, end = bodyStart;
  for (; end < src.length; end++) {
    if (src[end] === "{") depth++;
    else if (src[end] === "}") { depth--; if (depth === 0) break; }
  }
  return "var " + src.slice(start + "const ".length, end + 1) + ";";
}

function assert(cond, msg) { if (!cond) { console.error("FAIL: " + msg); process.exit(1); } }

// --- stubs the extracted section reads -------------------------------------------------
const h = function (component, props, children) {
  return { component: component, props: props || {}, children: children };
};
const rendered = [];
function DiagnosticCard(props) { rendered.push(props); return h("DiagnosticCard", props); }
const useI18n = function () { return { t: function () { return undefined; } }; };
const useState = function (v) { return [v, function () {}]; };
const useEffect = function () {};
const tx = function (t, key, fallback) { return fallback; };

eval(extractConst("FALLBACK_DIAGNOSTIC_EVENT_LABELS"));
eval(extract("isDiagnosticEvent"));
eval(extract("isTerminalCard"));
eval(extract("DiagnosticsSection"));

// --- subject: a completed card whose cached payload still carries the fault list ------
const doneCard = {
  id: "t_done", status: "done",
  card_facts: { dispatch: { stage: "DONE", terminal: true, column: "done", label: "Completed" } },
};
const diags = [{ kind: "prose_phantom_refs", severity: "warning", title: "Stale", detail: "history", actions: [] }];
const terminalOut = DiagnosticsSection({ task: doneCard, diagnostics: diags });
assert(terminalOut === null,
  "a completed card must paint no diagnostics section, got " + JSON.stringify(terminalOut));

// Same, without the derived fact (older backend generation): the native status decides.
const legacyArchived = { id: "t_arch", status: "archived" };
assert(DiagnosticsSection({ task: legacyArchived, diagnostics: diags }) === null,
  "an archived card without the derived fact must still paint no diagnostics section");

// --- control: a live card keeps its section -------------------------------------------
const liveOut = DiagnosticsSection({ task: { godoc: 1, id: "t_live", status: "ready" }, diagnostics: diags });
assert(liveOut !== null, "a live card with diagnostics must render its section");
assert(liveOut.props.className.indexOf("hermes-kanban-section") !== -1, "the live section keeps its class");

// --- the drawer Events list reads `isDiagnosticEvent`; the binding must exist ----------
assert(typeof isDiagnosticEvent === "function", "isDiagnosticEvent must be defined in the bundle");
assert(isDiagnosticEvent("suspected_hallucinated_references") === true,
  "isDiagnosticEvent must recognise a known diagnostic event kind");
assert(isDiagnosticEvent("completed") === false, "isDiagnosticEvent must not claim ordinary events");

console.log("PASS drawer fault: completed cards paint no diagnostics section; live cards keep theirs; isDiagnosticEvent bound");
