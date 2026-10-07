# Traefik (server ingress)

ModelOps does not ship a Traefik install or hard-code a certificate-resolver convention.
The target server env must set `TRAEFIK_CERTRESOLVER` to the resolver configured by that host.

The supported Linux server Control Plane path uses Traefik **labels** on Compose
services via:

- base: `deploy/compose/docker-compose.yml`
- overlay: `deploy/compose/docker-compose.server.yml`
- helper: `./scripts/deploy-server.sh --env-file <server.env>`

See `docs/architecture/02-traefik-deployment.md`.
