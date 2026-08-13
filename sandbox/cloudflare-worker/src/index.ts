/**
 * Silk Sandbox Protocol v1 on Cloudflare Sandboxes.
 *
 * Deploy with wrangler, set a SANDBOX_TOKEN secret, then:
 *   silkcode sandbox connect https://<worker>.workers.dev --token-env SILKCODE_SANDBOX_TOKEN
 *
 * Endpoints (Authorization: Bearer <SANDBOX_TOKEN>):
 *   GET  /health              -> { ok: true, version: 1 }
 *   POST /sync/:workspaceId   (tar.gz body) -> { ok: true }
 *   POST /exec/:workspaceId   { command, timeout } -> { exit_code, output }
 */
import { getSandbox, Sandbox } from "@cloudflare/sandbox";

export { Sandbox };

interface Env {
  Sandbox: DurableObjectNamespace;
  SANDBOX_TOKEN: string;
}

const WORKSPACE_ID = /^[a-f0-9]{8,64}$/;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const auth = request.headers.get("Authorization") ?? "";
    if (auth !== `Bearer ${env.SANDBOX_TOKEN}`) {
      return json({ error: "unauthorized" }, 401);
    }
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, version: 1 });
    }

    const syncMatch = url.pathname.match(/^\/sync\/([a-f0-9]+)$/);
    const execMatch = url.pathname.match(/^\/exec\/([a-f0-9]+)$/);
    const workspaceId = (syncMatch ?? execMatch)?.[1];
    if (!workspaceId || !WORKSPACE_ID.test(workspaceId) || request.method !== "POST") {
      return json({ error: "not found" }, 404);
    }

    // One sandbox per workspace id; state persists between requests.
    const sandbox = getSandbox(env.Sandbox, workspaceId);
    const workdir = `/workspace/${workspaceId}`;

    if (syncMatch) {
      const body = new Uint8Array(await request.arrayBuffer());
      let binary = "";
      for (let i = 0; i < body.length; i += 0x8000) {
        binary += String.fromCharCode(...body.subarray(i, i + 0x8000));
      }
      await sandbox.exec(`rm -rf ${workdir} && mkdir -p ${workdir}`);
      await sandbox.writeFile(`${workdir}.tar.gz.b64`, btoa(binary));
      const result = await sandbox.exec(
        `base64 -d ${workdir}.tar.gz.b64 > ${workdir}.tar.gz && ` +
        `tar xzf ${workdir}.tar.gz -C ${workdir} && rm ${workdir}.tar.gz ${workdir}.tar.gz.b64`,
      );
      if (result.exitCode !== 0) {
        return json({ error: `sync failed: ${result.stderr}` }, 500);
      }
      return json({ ok: true });
    }

    // exec
    const { command, timeout } = (await request.json()) as {
      command?: string;
      timeout?: number;
    };
    if (!command) {
      return json({ error: "empty command" }, 400);
    }
    const timeoutSeconds = Math.min(Math.max(timeout ?? 120, 1), 600);
    const result = await sandbox.exec(`cd ${workdir} && ${command}`, {
      timeout: timeoutSeconds * 1000,
    });
    const output = [result.stdout, result.stderr].filter(Boolean).join("\n");
    return json({ exit_code: result.exitCode, output });
  },
};
