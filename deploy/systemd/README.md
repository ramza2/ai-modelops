# Node Agent systemd (Linux Docker host)

Node Agent remains a **host process**. It is not packaged in Compose and must
not receive a Traefik router.

## Artifacts

| Path | Purpose |
|---|---|
| `modelops-node-agent.service.template` | Parameterized systemd unit (no secrets) |
| `node-agent.env.example` | Safe EnvironmentFile keys / placeholders |
| `../../scripts/install-node-agent-service.sh` | Bounded install/enable/health helper |

## Install (target server)

1. Create a non-root service user that already has Docker access (for example
   membership in the `docker` group). The installer does **not** create users
   or modify groups/firewall.
2. Copy `node-agent.env.example` to a protected path (for example
   `/etc/modelops/node-agent.env`) and set a real `NODE_AGENT_TOKEN`.
   When `--env-file` is already that path, the installer does **not** copy the
   file onto itself; it only enforces `root:root` mode `0600`.
3. Set the **same** secret on the Control Plane server env as
   `MODELOPS_NODE_AGENT_TOKEN` (Worker/Backend client setting).
4. Ensure the service user can traverse/read the Node Agent repo working
   directory and execute the venv `python`/`uvicorn` (installer validates this
   and does not chmod/chown the repository).
5. Run:

```bash
sudo ./scripts/install-node-agent-service.sh \
  --user <service-user> \
  --env-file /etc/modelops/node-agent.env
```

Optional: `--repo-root /path/to/ai-modelops`, `--venv-dir /path/to/venv`.

## Network / auth

- Standard bind: `NODE_AGENT_HOST=0.0.0.0`, `NODE_AGENT_PORT=8100`
- Containers reach the agent via `http://host.docker.internal:8100` (server
  Compose overlay adds `host-gateway`)
- Port 8100 must not be publicly exposed; use host firewall / network policy
- Installer never opens firewall ports and never adds Traefik labels
- Token lives only in the EnvironmentFile (not the unit / argv)

## Runtime validation (Gate C / RB-05 exit)

Target-host evidence still required:

```bash
systemctl status modelops-node-agent
curl -sS http://127.0.0.1:8100/health
curl -sS http://127.0.0.1:8100/ready
```

Repository artifacts alone do not close RB-05.
