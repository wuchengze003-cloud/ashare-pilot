// Deploy the A-share assistant Next.js app to a server already configured with
// the a-share-assistant systemd service and Nginx /a-share proxy.
//
// Usage:
//   cd web
//   DEPLOY_HOST=root@your-server npm run deploy:server
//
// Optional env:
//   DEPLOY_PATH=/opt/a-share-assistant-web/current
//   NEXT_BASE_PATH=/a-share
//   SERVICE_NAME=a-share-assistant.service
import path from "node:path";
import { execFileSync } from "node:child_process";

const deployHost = process.env.DEPLOY_HOST;
const deployPath = process.env.DEPLOY_PATH ?? "/opt/a-share-assistant-web/current";
const nextBasePath = process.env.NEXT_BASE_PATH ?? "/a-share";
const serviceName = process.env.SERVICE_NAME ?? "a-share-assistant.service";
const repoRoot = path.resolve(__dirname, "..", "..");

function run(command: string, args: string[], cwd = repoRoot) {
  console.log(`$ ${command} ${args.join(" ")}`);
  execFileSync(command, args, { cwd, stdio: "inherit" });
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

if (!deployHost) {
  throw new Error("DEPLOY_HOST is required, for example DEPLOY_HOST=root@your-server");
}

run("ssh", [deployHost, `mkdir -p ${shellQuote(deployPath)}`]);
run("rsync", [
  "-az",
  "--delete",
  "--exclude",
  "node_modules",
  "--exclude",
  ".next",
  "--exclude",
  ".env",
  "--exclude",
  ".env.local",
  "--exclude",
  ".cache",
  "--exclude",
  "cache.db",
  "--exclude",
  "tsconfig.tsbuildinfo",
  "web",
  `${deployHost}:${deployPath}/`,
]);

run("ssh", [
  deployHost,
  [
    `cd ${shellQuote(`${deployPath}/web`)}`,
    "npm ci",
    `NEXT_BASE_PATH=${shellQuote(nextBasePath)} npm run build`,
    `systemctl restart ${shellQuote(serviceName)}`,
    `systemctl is-active ${shellQuote(serviceName)}`,
  ].join(" && "),
]);
