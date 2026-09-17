# Security Policy

ApexAlgo stores exchange API credentials and can place real orders, so security reports are taken seriously.

## Supported versions

Only the latest release on `master` (and the current `dev` branch) receive security fixes. Please make sure you can reproduce the issue on a recent commit before reporting.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Use GitHub's private vulnerability reporting instead: go to the repository's **Security** tab → **Report a vulnerability** (<https://github.com/Stenvro/ApexAlgo/security/advisories/new>). Include:

- A description of the issue and its impact (e.g. key disclosure, unauthenticated API access, order manipulation).
- Steps to reproduce or a proof of concept.
- The commit you tested against and whether you run Docker or a manual install.

This is a hobby project maintained in spare time, so responses are best-effort: you can expect an acknowledgement within a week and a fix or mitigation as soon as reasonably possible. You will be credited in the release notes unless you prefer otherwise.

## Scope

Areas that matter most:

- API authentication (`X-API-Key`, `backend/core/security.py`) and the login gate in the web UI.
- Encryption of exchange credentials at rest (`backend/core/encryption.py`) and any path where a secret could end up in logs, exports or API responses.
- The live order path in `backend/engine/live_cycle.py` and `backend/engine/broker.py` (sizing, `max_order_value`, fill reconciliation).
- The Docker/nginx setup (TLS, CSP, port binding, non-root container).

Out of scope: vulnerabilities in third-party exchanges, issues that require an attacker to already control the host or the `data/` directory, and trading losses caused by strategy logic.

## Hardening checklist for users

ApexAlgo is designed to run on a machine you control, not on the public internet. Before trading with real funds:

- Create exchange API keys **without withdrawal permissions** and restrict them to your IP where the exchange supports it.
- Keep `BIND_ADDR` at the default `127.0.0.1`; only enable LAN access (`0.0.0.0`) on a trusted network, and never port-forward ApexAlgo to the internet.
- Protect `data/.env` — it holds the master API key and the encryption key. Losing it means losing access to your stored exchange keys; leaking it means someone else has them.
- Set a `max_order_value` on every live bot and start in paper mode.
- Keep your installation up to date (`git pull` + `docker compose build && docker compose up -d`).
