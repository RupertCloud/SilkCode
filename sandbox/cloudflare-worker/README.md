# Silk Sandbox on Cloudflare

A reference implementation of the Silk Sandbox Protocol v1 running on
[Cloudflare Sandboxes](https://developers.cloudflare.com/sandbox/) (Workers +
Containers). Silk Code syncs the workspace up and executes `run_command` /
`run_tests` inside a disposable cloud container instead of on your machine.

## Deploy

Requires a Cloudflare account with Containers access (Workers paid plan) and
Node.js:

```bash
cd sandbox/cloudflare-worker
npm install
npx wrangler deploy
npx wrangler secret put SANDBOX_TOKEN     # choose a strong shared secret
```

## Connect Silk Code

```bash
export SILKCODE_SANDBOX_TOKEN=<the same secret>
silkcode sandbox connect https://silkcode-sandbox.<you>.workers.dev
silkcode sandbox                          # status / health check
silkcode --sandbox ~/my-project           # commands now run in the cloud
```

## Notes

- One sandbox container per workspace; the container image is defined in
  `Dockerfile` — extend it with the toolchains your projects need.
- File edits happen locally (Silk Code remains the source of truth); the
  workspace is synced up before each command batch. Artifacts created
  remotely are not synced back.
- The same protocol is served by `silkcode sandbox serve` on any machine,
  so you can develop against a local sandbox before deploying this Worker.
- This Worker code follows the `@cloudflare/sandbox` SDK as documented; it
  is shipped as a reference and should be validated with `wrangler dev`
  against your account before production use.
