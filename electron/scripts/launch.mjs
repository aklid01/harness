/* Launches the Electron shell, and says why when it cannot.

   `npm run dev` used to be `npm run build && electron .`. When Electron itself
   fails to start, npm prints nothing after the build, which looks like the app
   "stopping" for no reason. This launcher makes the failure legible:

   * ELECTRON_RUN_AS_NODE and NODE_OPTIONS are cleared for the child. The first
     one makes Electron run as plain Node (no window, no output), which is a
     common inherited setting in IDE/agent environments; the second can inject
     a require hook that Electron cannot load.
   * the Electron binary is located through the installed package, so the same
     command works from any cwd;
   * the child's stdio is inherited, so Electron's own messages reach the
     terminal;
   * if it exits immediately, the exit code is printed with the likely causes
     instead of returning to a bare prompt.

   Extra arguments are passed through: `node scripts/launch.mjs --foo`.
*/

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const root = path.resolve(import.meta.dirname, "..");
const require = createRequire(import.meta.url);

function electronBinary() {
  // In a plain Node process, requiring "electron" returns the executable path.
  try {
    const resolved = require("electron");
    if (typeof resolved === "string" && existsSync(resolved)) return resolved;
  } catch (err) {
    console.error(`could not load the electron package: ${String(err)}`);
  }
  const fallback = path.join(
    root,
    "node_modules",
    "electron",
    "dist",
    process.platform === "win32" ? "electron.exe" : "electron",
  );
  return existsSync(fallback) ? fallback : null;
}

const main = path.join(root, "dist", "main", "main.js");
if (!existsSync(main)) {
  console.error("dist/main/main.js is missing - run `npm run build` first.");
  process.exit(1);
}

const binary = electronBinary();
if (!binary) {
  console.error("the electron binary is missing - run `npm install` in electron/.");
  process.exit(1);
}

const env = { ...process.env };
const cleared = [];
for (const name of ["ELECTRON_RUN_AS_NODE", "NODE_OPTIONS"]) {
  // Presence, not truthiness: Electron treats an empty value as set, and then
  // runs the app as plain Node (require("electron") returns a path, the first
  // `app.commandLine` call throws, and the window never opens).
  if (name in env) {
    cleared.push(name);
    delete env[name];
  }
}

const packageJson = JSON.parse(readFileSync(path.join(root, "package.json"), "utf-8"));
console.log(`launching electron ${packageJson.devDependencies?.electron ?? ""}`.trim());
console.log(`  binary: ${binary}`);
console.log(`  app:    ${root}`);
if (cleared.length) console.log(`  cleared for the child: ${cleared.join(", ")}`);
const result = spawnSync(binary, [root, ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
  cwd: root,
});

if (result.error) {
  console.error(`electron could not be started: ${String(result.error)}`);
  process.exit(1);
}

const code = result.status ?? 0;
if (result.signal) {
  console.error(`electron was killed by ${result.signal}`);
  process.exit(1);
}
if (code !== 0) {
  console.error(`electron exited with code ${code}`);
  console.error(
    [
      "If nothing above explains it, the usual causes are:",
      "  * Electron cannot read its own files in this folder (antivirus or a",
      "    restrictive ACL): try running it from a copy outside the project.",
      "  * a broken install: delete electron/node_modules/electron and run",
      "    `npm install` again.",
      "  * a missing display/GPU: try `--disable-gpu`.",
    ].join("\n"),
  );
}
process.exit(code);
