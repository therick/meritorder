# Security

Merit Order sits between your users and every LLM provider you pay for, and holds
the credentials for all of them. We treat security issues as the highest priority.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability**
on this repository. Please don't open a public issue. We aim to acknowledge reports
within 3 business days.

Include the version (image tag or commit), what an attacker can do, and steps to
reproduce. Leave real API keys and prompts out of the report.

## Supported versions

Merit Order is at 0.0.x. Fixes go into the latest release and `main`; older releases
don't get backports yet.

Merit Order is licensed under the AGPL-3.0 and comes with no warranty (sections 15 and
16 of the [license](LICENSE)). Fixes are published as source in this repository under
the same license, along with new images.

## What we do

- **Dependencies:** a small, deliberate set (see `docs/07-deployment.md`). New
  dependencies need a written justification in the pull request.
- **Lockfiles:** `uv.lock` and `ui/package-lock.json` pin exact versions with hashes.
  `exclude-newer` keeps Python resolution off releases from the last few days; npm
  install scripts are disabled (`ui/.npmrc`).
- **Updates:** Renovate waits 7 days before proposing a new release, and CI runs
  `pip-audit` and `npm audit` on every change.
- **Releases:** container images are multi-arch, signed with Sigstore cosign (verify them as
  in docs/07-deployment.md, "Verifying an image"), and
  published with an SBOM and build provenance.
- **CI actions** are pinned to commit SHAs (Renovate updates them), so a moved tag can't
  change what runs in the release job that signs images.
- **Runtime:** containers run as a non-root user, and the Helm chart gives them a
  read-only root filesystem. The gateway stores API keys only as SHA-256 hashes and
  stores no prompt or response content unless you turn it on: the response cache,
  stored responses for the Responses API (sealed with `MERITORDER_RESPONSES_KEY`), and
  batch uploads held until they're submitted (sealed with `MERITORDER_BATCH_KEY`).
  Console passwords are scrypt hashes; sessions, invite links and recovery codes are
  stored as SHA-256 hashes; two-factor secrets are sealed with `MERITORDER_AUTH_KEY`.
- **Secrets** never reach the database: provider keys come from the environment or
  mounted files, a stored configuration with a literal key is refused, and so is one
  that references a secret-looking variable outside the fields meant for secrets.

## Access model

- **Roles:** user (own usage), team lead (their team), finance (aggregates for everyone,
  never per-request rows), admin (everything). A key always acts as its user's record,
  so team leads can only issue keys for people on their own team with the user role.
- **Sessions** are scoped to the user: another caller sending the same session id gets
  its own session, routing state, and spend cap.
- **Tags** set on a key win over the same tags in a request header, so spend can't be
  moved off a cost center an admin assigned.
- **Team policy** (allowed models and backends, e.g. for data residency) applies to every
  call that sends a prompt upstream, including token counting.
- **Limits:** request bodies are capped (32 MB by default, `MERITORDER_MAX_BODY_MB`; much
  less for sign-in), and text compression runs in linear time on any input.
- **Console:** served with a strict Content-Security-Policy and no framing.
- **Exposure:** Docker Compose publishes the gateway on 127.0.0.1 unless
  `MERITORDER_BIND` says otherwise; with a public ingress, set `MERITORDER_METRICS_TOKEN`.
