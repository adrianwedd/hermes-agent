// Behavioral probe for the priority editor's input parser in the shipped dashboard bundle
// (#t_e5d24de5). The bundle has no build step — it IS the source — so this extracts
// `parsePriorityValue` verbatim and drives it, rather than regex-matching its text.
//
// What it proves: an unparseable draft is REFUSED (null) instead of being coerced to 0 by the old
// `Number(v) || 0`, which silently DEMOTED a card the operator meant to raise. Run via:
//   node kanban_priority_parse_probe.js <path-to-bundle>
const fs = require("fs");

const bundlePath = process.argv[2];
const src = fs.readFileSync(bundlePath, "utf8");
const start = src.indexOf("function parsePriorityValue");
if (start === -1) { console.error("parsePriorityValue not found in bundle"); process.exit(1); }
const bodyStart = src.indexOf("{", start);
let depth = 0, end = bodyStart;
for (; end < src.length; end++) {
  if (src[end] === "{") depth++;
  else if (src[end] === "}") { depth--; if (depth === 0) break; }
}
const fnSrc = src.slice(start, end + 1);

eval(fnSrc);

const accepted = [
  ["3", 3], [" 12 ", 12], ["0", 0], ["-2", -2], ["+4", 4], ["007", 7],
];
const refused = ["", "   ", "abc", "1.5", "1e3", "0x10", "1,000", "--1", "3px", "9007199254740993"];

for (const [input, expected] of accepted) {
  const got = parsePriorityValue(input);
  if (got !== expected) {
    console.error(`FAIL: parsePriorityValue(${JSON.stringify(input)}) => ${got}, want ${expected}`);
    process.exit(1);
  }
}

for (const input of refused) {
  const got = parsePriorityValue(input);
  if (got !== null) {
    console.error(`FAIL: parsePriorityValue(${JSON.stringify(input)}) => ${got}, want null (refusal, not coercion)`);
    process.exit(1);
  }
}

// null/undefined must not reach Number() (which would make them 0).
if (parsePriorityValue(null) !== null || parsePriorityValue(undefined) !== null) {
  console.error("FAIL: a missing draft must be refused, not read as 0");
  process.exit(1);
}

console.log("PASS");
