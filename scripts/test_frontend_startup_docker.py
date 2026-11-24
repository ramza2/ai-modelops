#!/usr/bin/env python3
"""REC-04B1 isolated Docker smoke. Use a disposable Docker host/context only.

Requires the candidate Frontend image already built locally. No pulls, host
ports, production Compose project, GPU, database, credentials or real models.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def run(*args, check=True, timeout=30):
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=check, timeout=timeout)


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def probe(frontend, path):
    result = run("exec", frontend, "wget", "-T", "5", "-S", "-O", "-",
                 "http://127.0.0.1" + path, check=False, timeout=10)
    codes = re.findall(r"HTTP/\d(?:\.\d)?\s+(\d{3})", result.stderr)
    return (int(codes[-1]) if codes else 0, result.stdout)


def wait_for(frontend, path, predicate):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        observed = probe(frontend, path)
        if predicate(observed):
            return observed
        time.sleep(0.5)
    raise RuntimeError("PROBE_TIMEOUT")


def mock_config(marker, ready):
    return """
server {
    listen 8000;
    location /api/ {
        default_type text/plain;
        return 200 "%s|$request_method|$request_uri|$http_host";
    }
    location = /health { return 200 "%s-health"; }
    location = /ready { return %s "%s-ready"; }
}
""" % (marker, marker, ready, marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local candidate Frontend tag or image ID")
    parser.add_argument("--isolated-docker", action="store_true", required=True,
                        help="Confirm this is an isolated test Docker host/context")
    args = parser.parse_args()
    image = run("image", "inspect", "--format", "{{.Id}}", "--", args.image).stdout.strip()
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "IMAGE_ID_INVALID")
    token = "modelops-rec04b1-" + uuid.uuid4().hex[:16]
    label = "ai.modelops.rec04b1.test=" + token
    network = token
    frontend = token + "-frontend"
    first, second = token + "-first", token + "-second"
    created = []
    net_created = False

    def start(name, *extra):
        run("run", "-d", "--pull", "never", "--name", name, "--label", label,
            "--network", network, *extra)
        created.append(name)

    def ip(name):
        info = json.loads(run("inspect", name).stdout)[0]
        return info["NetworkSettings"]["Networks"][network]["IPAddress"]

    with tempfile.TemporaryDirectory(prefix="modelops-rec04b1-") as directory:
        path = Path(directory)
        for marker, ready in (("first", 503), ("second", 200)):
            (path / (marker + ".conf")).write_text(mock_config(marker, ready))
        try:
            run("network", "create", "--internal", "--label", label, network)
            net_created = True
            start(frontend, image)
            wait_for(frontend, "/dashboard", lambda p: p[0] == 200 and bool(p[1]))
            for route in ("/api/example", "/health", "/ready"):
                require(probe(frontend, route)[0] == 502, "ABSENT_BACKEND_NOT_502")
            print("ABSENT_BACKEND_SPA_AND_PROXY_PASS")
            initial = run("inspect", "--format", "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}",
                          frontend).stdout
            start(first, "--network-alias", "backend", "--mount",
                  "type=bind,src=" + str(path / "first.conf") +
                  ",dst=/etc/nginx/conf.d/default.conf,readonly",
                  "--entrypoint", "nginx", image, "-g", "daemon off;")
            uri = "/api/x%2Fy?q=a%2Bb&n=2"
            wait_for(frontend, uri, lambda p: p == (200, "first|GET|" + uri + "|127.0.0.1"))
            require(probe(frontend, "/health") == (200, "first-health"), "HEALTH_PROXY_FAILED")
            require(probe(frontend, "/ready")[0] == 503, "READINESS_STATUS_NOT_PRESERVED")
            print("LATE_BACKEND_URI_AND_READINESS_PASS")
            start(second, "--network-alias", "backend", "--mount",
                  "type=bind,src=" + str(path / "second.conf") +
                  ",dst=/etc/nginx/conf.d/default.conf,readonly",
                  "--entrypoint", "nginx", image, "-g", "daemon off;")
            require(ip(first) != ip(second), "BACKEND_IP_NOT_CHANGED")
            run("rm", "-f", first)
            created.remove(first)
            wait_for(frontend, uri, lambda p: p == (200, "second|GET|" + uri + "|127.0.0.1"))
            require(probe(frontend, "/ready") == (200, "second-ready"), "NEW_BACKEND_NOT_READY")
            require(run("inspect", "--format", "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}",
                        frontend).stdout == initial, "FRONTEND_RESTARTED")
            print("BACKEND_IP_CHANGE_WITHOUT_FRONTEND_RESTART_PASS")
        finally:
            # Only this invocation's uniquely labelled containers/network.
            if net_created:
                ids = run("ps", "-aq", "--filter", "label=" + label).stdout.split()
                if ids:
                    run("rm", "-f", *ids)
                run("network", "rm", network)
    print("MODELOPS_REC04B1_ISOLATED_DOCKER_PASS")


if __name__ == "__main__":
    main()
