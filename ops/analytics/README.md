# Self-hosted analytics

The production site uses Umami `v3.2.0` at commit
`2f6e2b5ff256862a081d9e74bed18a42ebf795e3` with audited dependency overrides.
It runs on `127.0.0.1:3102` with PostgreSQL. Nginx exposes only the tracker,
recorder, collection endpoints, and this site's recorder configuration. The
login UI and reporting APIs are not public.

## Install or rebuild

Build the Linux runtime on a workstation so the 1.6 GB production server never
has to run a Next.js compiler:

```bash
bash build-linux-runtime.sh
```

The command produces a Linux/amd64 archive and SHA-256 file in a temporary build
directory. Upload both files, then run:

```bash
UMAMI_RUNTIME_ARCHIVE=/root/umami-runtime.tgz bash install-umami.sh
```

The server-side source build remains available for larger hosts, but it is not
the normal path for this server. Prebuilt mode refuses to start unless all 20
database migrations have already been applied.

Copy this directory to the server, then run as root:

```bash
bash install-umami.sh
```

The installer verifies the pinned Git commit, applies the dependency overrides,
fails on high/critical production dependency advisories, runs upstream tests,
applies database migrations, rotates the default admin password, and enables
50% replay sampling with all form inputs masked.

Secrets are stored outside Git:

- `/etc/umami/umami.env`
- `/etc/a-share-assistant/a-share.env`
- `/root/.config/a-share-assistant/umami-admin.env`

After key-based SSH has been verified, install `sshd-hardening.conf` under
`/etc/ssh/sshd_config.d/`, validate with `sshd -t`, and reload SSH. This retains
root key access for deployment while disabling password login.

## Open the private dashboard

The admin dashboard is intentionally unavailable on the public address. Open a
local SSH tunnel:

```bash
ssh -N -L 33102:127.0.0.1:3102 root@47.77.231.22
```

Then visit `http://127.0.0.1:33102/analytics`. The root-only credentials file on
the server contains the generated password.

## Collected behavior

- Standard page views, visitors, sessions, referrers, device and coarse IP location.
- Effective visible/active time milestones at 15, 30, 60 and 180 seconds.
- Scroll milestones at 25%, 50%, 75% and 100%.
- Navigation and history-table interactions.
- 50% sampled session replay and 100% heatmap sampling, with inputs masked and a
  five-minute maximum recording duration.

## Rollback

Stop Umami and restore the timestamped Nginx backup next to
`/etc/nginx/sites-available/content-ops-studio`. Removing the
`NEXT_PUBLIC_UMAMI_*` variables and rebuilding the web app disables all browser
tracking without touching market data or strategy code.
