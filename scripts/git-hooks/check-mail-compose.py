#!/usr/bin/env python3
"""Mail wiring guard for the compose files (slice 2.5, AC-25).

Usage: check-mail-compose.py <docker-compose.yml> <docker-compose.dev.yml>

No pytest can see these files (the api container mounts ./api only), so the pre-commit hook runs
this against the STAGED content. Exits 1 with one line per violation. Needs python3 + PyYAML; if
PyYAML is missing it fails rather than skips -- a guard that quietly does not run is a belief.
"""
from __future__ import annotations

import sys

try:
    import yaml
except ImportError:
    sys.exit("check-mail-compose: PyYAML is not installed (pip install pyyaml); refusing to skip the guard.")

PINNED = {
    "MAIL_SMTP_HOST": "mailpit",
    "MAIL_SMTP_PORT": "1025",
    "MAIL_SMTP_SECURITY": "none",
    # Credentials pinned EMPTY: a real relay login in a dev .env must never be sent as AUTH to Mailpit.
    "MAIL_SMTP_USERNAME": "",
    "MAIL_SMTP_PASSWORD": "",
}
LOCAL_ORIGINS = ("http://localhost", "http://127.0.0.1")


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def env_of(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):  # ["K=V", ...]
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def published(port) -> tuple[str, str, str]:
    """(host_ip, host_port, container_port) of one `ports:` entry, short or long syntax."""
    if isinstance(port, dict):
        return str(port.get("host_ip", "")), str(port.get("published", "")), str(port.get("target", ""))
    parts = str(port).split("/")[0].split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", parts[0]


def main(prod_path: str, dev_path: str) -> list[str]:
    errors: list[str] = []
    prod_services = load(prod_path).get("services") or {}
    dev_services = load(dev_path).get("services") or {}

    if "mailpit" in prod_services:
        errors.append(f"{prod_path} defines a mailpit service. Mailpit is dev/CI only; production mails through the real relay.")

    for name in ("api", "worker", "beat"):
        env = env_of(dev_services.get(name) or {})
        base = env.get("PUBLIC_BASE_URL", "")
        if not any(base == o or base.startswith(o + ":") or base.startswith(o + "/") for o in LOCAL_ORIGINS):
            errors.append(f"{dev_path}: {name} must pin PUBLIC_BASE_URL to an http://localhost or http://127.0.0.1 "
                          f"origin in environment: (found {base!r}). The worker builds every emailed link from it; "
                          "otherwise dev mail links to the .env's production origin.")
        for key, want in PINNED.items():
            if key not in env or env.get(key) != want:
                errors.append(f"{dev_path}: {name} must pin {key}: {want} in environment: (found {env.get(key)!r}). "
                              "Otherwise a production-shaped .env delivers dev mail to real addresses.")
        if not env.get("MAIL_FROM_ADDRESS", "").strip():
            errors.append(f"{dev_path}: {name} must pin a non-empty MAIL_FROM_ADDRESS in environment:.")

    mailpit = dev_services.get("mailpit")
    if mailpit is None:
        errors.append(f"{dev_path}: no mailpit service.")
    else:
        image = str(mailpit.get("image", ""))
        last = image.rsplit("/", 1)[-1]
        tag = last.split(":", 1)[1] if ":" in last and "@" not in last else ""
        if "@sha256:" not in image and tag in ("", "latest"):
            errors.append(f"{dev_path}: mailpit image {image!r} is untagged or latest. Pin a version.")
        ports = [published(p) for p in (mailpit.get("ports") or [])]
        if ports != [("127.0.0.1", "8025", "8025")]:
            errors.append(f"{dev_path}: mailpit must publish exactly 127.0.0.1:8025:8025 (found {ports}). "
                          "SMTP (1025) is never published; the UI binds the loopback only.")
    return errors


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    problems = main(sys.argv[1], sys.argv[2])
    for p in problems:
        print(f"check-mail-compose: {p}", file=sys.stderr)
    sys.exit(1 if problems else 0)
