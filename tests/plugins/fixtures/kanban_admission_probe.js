// Behavioral probe for the derived-admission fact on the browser/mobile kanban bundle
// (plugins/kanban/dashboard/dist/index.js — plain IIFE, no build step, so the bundle IS
// the source). Extracts `cardColumn` and `admissionChip` verbatim and drives them, rather
// than regex-matching their text.
//
// What it proves: the lane a card is filed in, and the chip it carries, come from the
// DERIVED stage (card_facts.dispatch.column) — not the raw status. A `ready` card under an
// administrative hold is refused by the dispatcher on both lanes, so it must not render
// under Ready with a contradicting badge; a card with no derived fact shows nothing rather
// than inventing a state.
//
// Run via:  node kanban_admission_probe.js <path-to-bundle>
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

// `cardColumn` is self-contained. `admissionChip` calls `h` and `cn` from the bundle's
// runtime, so drive it with tiny stand-ins that record what they were handed.
const ADMISSION_TONE_SRC = (function () {
  const start = src.indexOf("const ADMISSION_TONE");
  const end = src.indexOf("};", start) + 2;
  return src.slice(start, end);
})();

const calls = [];
const h = function (component, props, children) { calls.push({ component, props, children }); return { component, props, children }; };
const cn = function () { return Array.prototype.slice.call(arguments).filter(Boolean).join(" "); };
// `Badge` is the bundle's own primitive; a stand-in is enough to reach the props under test.
const Badge = function (props, children) { calls.push({ component: "Badge", props: props, children: children }); return { component: "Badge", props: props, children: children }; };

eval(extract("cardColumn"));
// The bundle declares ADMISSION_TONE with `const`, which an eval keeps in its own scope;
// re-declare it as a var so the extracted admissionChip can read it.
eval(ADMISSION_TONE_SRC.replace(/^const /, "var "));
eval(extract("admissionChip"));

function assert(cond, msg) { if (!cond) { console.error("FAIL: " + msg); process.exit(1); } }

// 1. The lane comes from the derived stage, not the raw status.
const operatorHeld = {
  id: "t_held", status: "ready", dispatch_eligible: false,
  card_facts: { dispatch: { stage: "OPERATOR", dispatchable: false, column: "blocked",
                            label: "OPERATOR · receipt administration", reason: "bind evidence",
                            owner: "operator", next_action: "close natively", basis: "admin event" } }
};
assert(cardColumn(operatorHeld) === "blocked",
  "an admin-held ready card must be filed under blocked, got " + cardColumn(operatorHeld));

// 2. A card with no derived fact falls back to its native status (older payloads).
assert(cardColumn({ id: "t_old", status: "ready" }) === "ready",
  "a payload without the derived fact must fall back to the native status");

// 3. The chip is the fact's own label, with the concrete next action in the title.
calls.length = 0;
const chip = admissionChip(operatorHeld);
assert(chip !== null, "an OPERATOR card must carry an admission chip");
const props = calls[0].props;
assert(calls[0].children === "OPERATOR · receipt administration",
  "chip text must be the fact s label, got " + JSON.stringify(calls[0].children));
assert(props.title.indexOf("close natively") !== -1,
  "the chip title must carry the concrete NEXT ACTION, got " + JSON.stringify(props.title));
assert(props.title.indexOf("Owner: operator") !== -1,
  "the chip title must name the resolver, got " + JSON.stringify(props.title));
assert(props.className.indexOf("hermes-kanban-admission--operator") !== -1,
  "an OPERATOR card must take the operator tone, got " + JSON.stringify(props.className));

// 4. A terminal card is neutral, and a blocked card is the fault tone.
calls.length = 0;
admissionChip({ id: "t_done", status: "done", card_facts: { dispatch: { stage: "DONE", column: "done", label: "Completed", reason: "Final resolution: shipped" } } });
assert(calls[0].props.className.indexOf("--terminal") !== -1, "a DONE card must take the terminal tone");

calls.length = 0;
admissionChip({ id: "t_blocked", status: "blocked", card_facts: { dispatch: { stage: "BLOCKED", column: "blocked", label: "BLOCKED · capability", reason: "no console access" } } });
assert(calls[0].props.className.indexOf("--blocked") !== -1, "a BLOCKED card must take the fault tone");

// 5. No derived fact => no invented state (the lane header already says where it sits).
calls.length = 0;
assert(admissionChip({ id: "t_old", status: "ready" }) === null,
  "a card with no derived fact must show no admission chip rather than invent a state");

// 6. The lane's cards are re-bucketed by the same derived column, so an optimistically
//    moved list cannot leave a held card under Ready.
eval(extract("columnForCards"));
const lane = { name: "ready", tasks: [operatorHeld, { id: "t_ok", status: "ready",
  card_facts: { dispatch: { stage: "READY", column: "ready", label: "READY · queued" } } }] };
const filtered = columnForCards(lane, cardColumn);
assert(filtered.tasks.length === 1 && filtered.tasks[0].id === "t_ok",
  "the ready lane must drop the admin-held card, kept " + JSON.stringify(filtered.tasks.map(t => t.id)));
assert(lane.tasks.length === 2, "the input lane object must not be mutated");

console.log("PASS derived admission: lane, chip label, tone, next action, back-compat fallback");
