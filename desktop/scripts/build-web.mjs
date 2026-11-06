import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const dist = path.join(root, "dist");
rmSync(dist, { recursive: true, force: true });

const tsc = path.join(root, "node_modules", "typescript", "bin", "tsc");
const built = spawnSync(
  process.execPath,
  [tsc, "-p", path.join(root, "tsconfig.json")],
  {
    stdio: "inherit",
  },
);
if (built.status !== 0) process.exit(built.status ?? 1);

const renderer = path.join(dist, "renderer");
mkdirSync(renderer, { recursive: true });
for (const file of ["index.html", "styles.css"]) {
  copyFileSync(path.join(root, "web", file), path.join(renderer, file));
}
console.log(`web assets staged in ${path.relative(root, renderer)}`);
