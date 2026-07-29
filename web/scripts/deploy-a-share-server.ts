// Deploy the A-share assistant Next.js app to a server already configured with
// the a-share-assistant systemd service and Nginx /a-share proxy.
//
// Usage:
//   cd web
//   DEPLOY_HOST=root@your-server npm run deploy:server
//
// Optional env:
//   DEPLOY_ROOT=/opt/a-share-assistant-web
//   NEXT_BASE_PATH=/a-share
//   SERVICE_NAME=a-share-assistant.service
import path from "node:path";
import { execFileSync } from "node:child_process";

const deployHost = process.env.DEPLOY_HOST;
const deployRoot = process.env.DEPLOY_ROOT ?? "/opt/a-share-assistant-web";
const nextBasePath = process.env.NEXT_BASE_PATH ?? "/a-share";
const serviceName = process.env.SERVICE_NAME ?? "a-share-assistant.service";
const serverEnvFile = process.env.SERVER_ENV_FILE ?? "/etc/a-share-assistant/a-share.env";
const repoRoot = path.resolve(__dirname, "..", "..");
const releaseId = new Date().toISOString().replaceAll(/[-:.TZ]/g, "").slice(0, 14);
const releasePath = `${deployRoot}/releases/${releaseId}`;
const currentPath = `${deployRoot}/current`;

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

run("npm", ["run", "dashboard:validate"], path.resolve(repoRoot, "web"));
run("ssh", [deployHost, `install -d -m 0755 ${shellQuote(`${deployRoot}/releases`)} ${shellQuote(releasePath)}`]);
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
  `${deployHost}:${releasePath}/`,
]);
run("rsync", [
  "-az",
  "--delete",
  "config",
  `${deployHost}:${releasePath}/`,
]);

run("ssh", [
  deployHost,
  [
    "set -e",
    `cd ${shellQuote(`${releasePath}/web`)}`,
    `test -r ${shellQuote(serverEnvFile)}`,
    `set -a && . ${shellQuote(serverEnvFile)} && set +a`,
    "npm ci",
    "npm run dashboard:validate",
    `NODE_OPTIONS=--max-old-space-size=768 NEXT_BASE_PATH=${shellQuote(nextBasePath)} npm run build`,
    "install -d -o a-share -g a-share -m 0750 .cache .next/cache",
    `if [ -d ${shellQuote(currentPath)} ] && [ ! -L ${shellQuote(currentPath)} ]; then mv ${shellQuote(currentPath)} ${shellQuote(`${deployRoot}/releases/pre-atomic-${releaseId}`)}; fi`,
    `ln -sfn ${shellQuote(releasePath)} ${shellQuote(`${currentPath}.new`)}`,
    `mv -Tf ${shellQuote(`${currentPath}.new`)} ${shellQuote(currentPath)}`,
    `systemctl restart ${shellQuote(serviceName)}`,
    `systemctl is-active ${shellQuote(serviceName)}`,
    `for i in $(seq 1 30); do curl -fsS http://127.0.0.1:3101${shellQuote(nextBasePath)} >/dev/null 2>&1 && break; sleep 1; done`,
    `curl -fsS http://127.0.0.1:3101${shellQuote(nextBasePath)} >/dev/null`,
    `find ${shellQuote(`${deployRoot}/releases`)} -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\\n' | sort -rn | tail -n +4 | cut -d' ' -f2- | xargs -r rm -rf`,
  ].join(" && "),
]);
