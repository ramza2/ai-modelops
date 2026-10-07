# Traefik (server ingress)

ModelOps does not ship a Traefik install or certificate-resolver convention.

The supported Linux server Control Plane path uses Traefik **labels** on Compose
services via:

- base: `deploy/compose/docker-compose.yml`
- overlay: `deploy/compose/docker-compose.server.yml`
- helper: `./scripts/deploy-server.sh --env-file <server.env>`

See `docs/architecture/02-traefik-deployment.md`.
