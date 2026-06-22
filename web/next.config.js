/** @type {import('next').NextConfig} */
const basePath = process.env.NEXT_BASE_PATH || "";

module.exports = {
  reactStrictMode: true,
  outputFileTracingRoot: __dirname,
  serverExternalPackages: ["better-sqlite3"],
  ...(basePath ? { basePath } : {}),
};
