import fs from "node:fs";
import path from "node:path";

const sourceDir = process.argv[2];
if (!sourceDir) throw new Error("usage: node apply-umami-overrides.mjs <umami-source-dir>");

const packagePath = path.join(sourceDir, "package.json");
const pkg = JSON.parse(fs.readFileSync(packagePath, "utf8"));
pkg.pnpm = {
  ...(pkg.pnpm ?? {}),
  overrides: {
    ...(pkg.pnpm?.overrides ?? {}),
    "d3-color@<3.1.0": "3.1.0",
    "fast-uri@<3.1.2": "3.1.2",
    "hono@<4.12.25": "4.12.25",
    "minimatch@<3.1.4": "3.1.4",
    "minimatch@>=5.0.0 <5.1.8": "5.1.8",
    "shell-quote@<1.8.4": "1.8.4",
    "svgo@>=3.0.0 <3.3.3": "3.3.3",
  },
};
fs.writeFileSync(packagePath, `${JSON.stringify(pkg, null, 2)}\n`);
