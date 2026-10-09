import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const out = path.join(root, "dist", "renderer");
mkdirSync(out, { recursive: true });
for (const file of ["index.html", "styles.css"]) {
  copyFileSync(path.join(root, "src", "renderer", file), path.join(out, file));
}
console.log("assets copied");
