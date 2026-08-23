# The Silk Code GitHub App (one-time maintainer setup)

Silk Code's preferred GitHub authorization is **install-an-app + Sign in with
GitHub** — developers never create or paste tokens. This works through a
GitHub App that the project maintainer registers **once**; its public client
id then ships as the default for everyone.

## 1. Register the app (about 3 minutes)

Go to **Settings → Developer settings → GitHub Apps → New GitHub App** and
fill in:

| Field | Value |
| --- | --- |
| GitHub App name | `Silk Code` (or `Silk Code Dev`) |
| Homepage URL | `https://github.com/RupertCloud/SilkCode` |
| Webhook | **Uncheck** "Active" (no webhook needed) |
| **Enable Device Flow** | **Check this** — required for sign-in |
| Repository permissions | Contents: **Read and write** · Pull requests: **Read and write** · Issues: **Read and write** · Metadata: Read-only (automatic) |
| Where can this app be installed? | **Any account** |

Create the app, then copy its **Client ID** from the app's General page.

Optional but recommended: under *Optional features*, leave **user-to-server
token expiration** enabled — Silk Code refreshes expired tokens
automatically.

## 2. Ship the client id

Client ids are public (they are not secrets in the device flow). Set it as
the project default in `silkcode/github_oauth.py`:

```python
DEFAULT_GITHUB_CLIENT_ID = "Iv23liIVrIULoOVsdX9b"   # public, not a secret
```

For a custom or GitHub Enterprise app, override it locally instead:

```bash
silkcode connect github --client-id Iv23li...
```

## 3. What developers do (after step 2: nothing to set up)

```bash
silkcode connect github        # or click "Sign in with GitHub" in the GUI
```

They get a short code, approve it at github.com/login/device, and — the
app-install part — GitHub asks which account to authorize. To grant access
to specific repositories they install the app: `https://github.com/apps/
<app-slug>/installations/new`, pick the repos, done. Access can be reviewed
or revoked any time at **Settings → Applications**.

Tokens issued this way are short-lived user tokens (`ghu_...`), scoped to
the app's permissions and the installed repositories, and refreshed
automatically by Silk Code. Personal access tokens keep working as a
fallback (`$GITHUB_TOKEN` or the token field on the GUI authorization page).
