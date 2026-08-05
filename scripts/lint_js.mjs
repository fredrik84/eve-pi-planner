#!/usr/bin/env node
/**
 * `no-undef` over static/*.js — the one lint rule this codebase needs, and the reason it needs it.
 *
 * A dead `if (!r.ok)` left behind by the fetch()->api() migration referenced a variable that no
 * longer existed, so EVERY successful reaction assign threw a ReferenceError, was caught, and was
 * reported to the user as failed. Because the assign endpoint blindly appended, each retry added
 * another full set of rows: two suggestions became 27 assignment rows on a 10-slot character
 * (2026-08-01). `node --check` cannot catch that — it is valid syntax and only fails when the line
 * runs. `no-undef` catches it, proven both ways: re-introducing the bug reports `'r' is not
 * defined` at that exact line, and the fixed tree reports zero findings.
 *
 * The wrinkle this script exists for: the frontend is plain <script> files sharing ~800 implicit
 * globals, so a naive run reports every cross-file helper as undefined. So: scrape the top-level
 * declarations out of all of static/*.js, feed them in as globals, and enable ONLY no-undef.
 * Deliberately not a general lint setup — style rules over a codebase this size would be noise,
 * and the point is a guard that starts from green and stays there.
 *
 * Usage: node scripts/lint_js.mjs   (exits non-zero on any finding)
 */
import { readdirSync, readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";

const DIR = "static";
const files = readdirSync(DIR).filter(f => f.endsWith(".js")).map(f => join(DIR, f));

// Top-level declarations only — a name indented by anything is inside a function and is not a
// shared global. Covers `function f(`, `let/const/var x`, and `async function f(`.
const DECL = /^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|^(?:let|const|var)\s+([A-Za-z_$][\w$]*)/gm;
const globals = new Set();
for (const f of files) {
  const src = readFileSync(f, "utf8");
  for (const m of src.matchAll(DECL)) globals.add(m[1] || m[2]);
}

const dir = mkdtempSync(join(tmpdir(), "evpi-eslint-"));
const cfg = join(dir, "eslint.config.mjs");
writeFileSync(cfg, `export default [{
  files: ["**/*.js"],
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: "script",
    globals: Object.fromEntries([
${[...globals].sort().map(g => `      ${JSON.stringify(g)}`).join(",\n")}
    ].map(n => [n, "writable"]).concat(
      ${JSON.stringify([
        "window", "document", "console", "fetch", "localStorage", "sessionStorage", "navigator",
        "location", "history", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
        "requestAnimationFrame", "cancelAnimationFrame", "alert", "confirm", "prompt", "Image",
        "URL", "URLSearchParams", "FormData", "Blob", "FileReader", "AbortController", "Event",
        "CustomEvent", "MutationObserver", "IntersectionObserver", "matchMedia", "getComputedStyle",
        "structuredClone", "queueMicrotask", "performance", "crypto", "TextEncoder", "TextDecoder",
        "atob", "btoa", "screen", "self", "top", "parent", "frames", "scrollTo", "scrollBy",
      ])}.map(n => [n, "readonly"])
    )),
  },
  linterOptions: { reportUnusedDisableDirectives: false },
  rules: { "no-undef": "error" },
}];
`);

console.log(`linting ${files.length} files with ${globals.size} scraped globals`);
try {
  execFileSync("npx", ["--yes", "eslint@9", "--no-config-lookup", "--config", cfg, ...files],
               { stdio: "inherit" });
} catch (e) {
  process.exit(e.status || 1);
}
