// Behavioral probe for the terminal-card warning badge on the browser/mobile kanban
// bundle (plugins/kanban/dashboard/dist/index.js — plain IIFE, no build step, so the
// bundle IS the source). Extracts `isTerminalCard` and `warningBadge` verbatim and
// drives them, rather than regex-matching their text.
//
// What it proves: a completed card must not paint the destructive diagnostics badge
// on any surface. The backend withholds the rollup for terminal statuses (a
// block→unblock streak is frozen history once the card can no longer be blocked);
// `warningBadge` returns null anyway, so a cached/older payload that still carries a
// warnings summary cannot re-introduce the red badge on finished work. A live card
// keeps its badge — the carve-out is targeted, not a blanket silencing.
//
// Run via:  node kanban_terminal_warning_probe.js <path-to-bundle>
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

const calls = [];
const h = function (component, props, children) { calls.push({ component, props, children }); return { component, props, children }; };
const cn = function () { return Array.prototype.slice.call(arguments).filter(Boolean).join(" "); };

eval(extract("isTerminalCard"));
eval(extract("warningBadge"));

function assert(cond, msg) { if (!cond) { console.error("FAIL: " + msg); process.exit(1); } }

// A completed card whose cached payload still carries the retained cycling warning.
const doneCached = {
  id: "t_done", status: "done",
  card_facts: { dispatch: { stage: "DONE", terminal: true, column: "done", label: "Completed" } },
  warnings: { count: 3, kinds: { block_unblock_cycling: 3 }, highest_severity: "warning" }
};
assert(isTerminalCard(doneCached) === true, "a done card is terminal");
assert(warningBadge(doneCached) === null,
  "a completed card must not render the warning badge, got " + JSON.stringify(warningBadge(doneCached)));

// Same, without the derived fact (older backend generation): the native status decides.
const legacyDone = { id: "t_legacy", status: "archived", warnings: { count: 1, highest_severity: "error" } };
assert(warningBadge(legacyDone) === null,
  "an archived card without the derived fact must still take no warning badge");

// A LIVE card keeps its badge, with the severity tone and the count in the title.
const liveCard = { id: "t_live", status: "ready", warnings: { count: 2, highest_severity: "error" } };
const badge = warningBadge(liveCard);
assert(badge !== null, "a live card with diagnostics must keep its badge");
assert(badge.props.className.indexOf("hermes-kanban-warning-badge--error") !== -1,
  "the badge must carry the severity tone, got " + JSON.stringify(badge.props.className));
assert(badge.props.title.indexOf("2 active diagnostics") !== -1,
  "the badge title must carry the count, got " + JSON.stringify(badge.props.title));

// No warnings at all => no badge (never an empty red dot).
assert(warningBadge({ id: "t_clean", status: "ready" }) === null, "a clean card carries no badge");

console.log("PASS terminal warning: completed cards carry no fault badge; live cards keep theirs");
