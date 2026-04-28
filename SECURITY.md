# Security Policy

## Supported versions

GrooveIQ ships from `main`. Only the latest Docker image
(`ghcr.io/sxx7/grooveiq:latest`) and the `main` branch receive security
fixes. Older builds are not patched — pull a fresh image to pick up
fixes.

## Reporting a vulnerability

**Please do not file public issues for security problems.** Instead,
report privately via GitHub's
[private vulnerability reporting](https://github.com/Sxx7/GrooveIQ/security/advisories/new).
That opens a private channel between you and the maintainer; the report
is not visible to anyone else.

You can also email the maintainer directly if you prefer — see the
GitHub profile of the repo owner.

When reporting, please include:

- A clear description of the issue and where in the code it lives
- Steps to reproduce, or a minimal proof of concept
- Your assessment of impact (data exposure, RCE, denial of service, etc.)
- Any mitigations that already help (env vars, deployment patterns)

We aim to:

- Acknowledge the report within 72 hours
- Ship a fix for critical/high-severity issues within 14 days
- Coordinate disclosure with the reporter before publishing details

## Out of scope

The following are known properties of GrooveIQ as a self-hosted backend
and are not treated as vulnerabilities unless paired with a concrete
exploit path:

- The `/dashboard` endpoint and its embedded JS are unauthenticated by
  design; protect the deployment with a reverse proxy or VPN if
  exposing it beyond LAN.
- API keys are stored as SHA-256 hashes in memory; an attacker with
  process memory access can read them.
- Audio analysis runs locally on the file paths under `MUSIC_LIBRARY_PATH`;
  the container has read access to whatever you bind-mount.

## Hardening tips

- Always run behind a reverse proxy with TLS if exposed to the internet.
- Pin `ghcr.io/sxx7/grooveiq` to a specific digest in production rather
  than `:latest`; bump deliberately on a schedule.
- Rotate `API_KEYS` periodically; treat them like passwords.
- Review the `Dependabot` and `Upstream releases` issues weekly so that
  CVEs in pinned dependencies and upstream services don't go stale.
