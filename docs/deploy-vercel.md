# Deploying the OpenOPC landing site to Vercel

## What deploys (and what doesn't)

Only the **`landing/`** directory deploys — a self-contained static site: the landing page
(`landing/index.html`) plus the infographics (`landing/infographics/`). It has no build step and no
server. **OpenOPC itself does not deploy to Vercel** — the app is a stateful local daemon (aiohttp
WebSocket server, subprocess-driven agent CLIs, Playwright, SQLite) that Vercel's serverless/static
model cannot host. Run the app locally with `opc ui`.

`landing/` is self-contained: every internal link stays inside the directory (the infographics hub's
`../index.html` resolves to the deploy root), and screenshots load from `raw.githubusercontent.com`
absolute URLs. `landing/vercel.json` sets `framework: null` so Vercel serves it as static and does
not try to build the parent Python project.

## Path A — canonical, auto-redeploying (recommended)

Connect the GitHub repo once; every push to `main` redeploys. Dashboard steps:

1. Go to <https://vercel.com/new> and **Import** `wjlgatech/physical-ai-native`.
2. In project settings, set **Root Directory = `landing`** (this is the key step — it scopes the
   deploy to the static site and away from the Python root).
3. **Framework Preset = Other**, leave Build/Output commands empty (static passthrough).
4. **Deploy.** Vercel prints a production URL like `https://openopc.vercel.app`.
5. Production branch is `main` by default — pushes to `main` now redeploy automatically.

## Path B — Vercel CLI (fastest to a live URL)

From the repo root:

```bash
# 1. Authenticate (opens a browser; choose "Continue with GitHub"). One time per machine.
vercel login

# 2. Deploy the landing directory as its own static project.
cd landing
vercel --prod
#   First run asks a few questions — accept these answers:
#     Set up and deploy “…/landing”?           Y
#     Which scope?                              <your account>
#     Link to existing project?                 N
#     Project name?                             openopc            (or your choice)
#     In which directory is your code located?  ./                 (you're already in landing/)
#     Modify settings?                          N
```

Vercel prints the production URL (e.g. `https://openopc.vercel.app`). CLI deploys are snapshots — to
get auto-redeploy on push, also connect the Git repo (Path A) in the dashboard afterward.

## After the first deploy

- Verify the live surface (not just the build): open the URL, then `/infographics/`, and confirm the
  screenshots and infographics render. (A missing image = a `raw.githubusercontent` path drift.)
- Once the canonical URL exists, the README's landing/infographic links can point at it (live from
  GitHub) instead of the repo-relative paths that only render when deployed or opened locally.

## Troubleshooting

- **Vercel tries to build a Python app / fails on `requirements`** → the Root Directory isn't
  `landing`. Set it (Path A step 2) or run the CLI from inside `landing/` (Path B).
- **404 on an infographic** → deep links use `.html` (e.g. `/infographics/physical-ai.html`); the
  static config serves files as-is, no clean-URL rewriting.
- **Stale content after a push** → only Path A (Git integration) auto-redeploys; a CLI deploy is a
  one-shot snapshot, re-run `vercel --prod`.
