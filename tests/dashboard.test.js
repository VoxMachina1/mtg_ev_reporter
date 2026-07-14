const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("index.html", "utf8");

function extractFunction(name) {
  const start = html.indexOf(`function ${name}(`);
  assert.notStrictEqual(start, -1, `${name} not found`);
  const bodyStart = html.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < html.length; i++) {
    if (html[i] === "{") depth++;
    if (html[i] === "}" && --depth === 0) return html.slice(start, i + 1);
  }
  throw new Error(`unterminated ${name}`);
}

const context = {};
vm.createContext(context);
vm.runInContext(`${extractFunction("parseCSV")}; this.parseCSV = parseCSV;`, context);

const rows = context.parseCSV(
  'Deck Name,Category,Set Code,Release Date,Total Value,Top 5 Cards\r\n' +
  '"Deck, the Great",Commander,TST,2026-01-01,42.00,"Card A, Card B, ""Card C"""\r\n'
);

assert.strictEqual(rows.length, 1);
assert.strictEqual(rows[0]["Deck Name"], "Deck, the Great");
assert.strictEqual(rows[0]["Total Value"], "42.00");
assert.strictEqual(rows[0]["Top 5 Cards"], 'Card A, Card B, "Card C"');

assert(!/tr\.innerHTML\s*=/.test(html), "row rendering must not use innerHTML");
assert(/td\.textContent\s*=\s*value/.test(html), "cells must render through textContent");

console.log("dashboard tests passed");
