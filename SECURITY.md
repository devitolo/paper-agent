# Security Policy

## Supported versions

Project Paper is preparing its first supported release. Until that release is qualified, security fixes are applied to the current `main` branch and the newest release candidate only.

After V1 launches, maintainers will support the current major version. Older major versions stop receiving fixes when the next major version becomes supported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when it is available. Do not open a public issue for a vulnerability that includes an exploit, credential, private feedback, database content, or other sensitive data.

Include the affected version or commit, operating system and architecture, Docker and Compose versions, impact, reproduction steps, and the smallest sanitized evidence needed to investigate. Remove API keys, `.env` contents, raw feedback, databases, downloaded papers, and personal paths from logs.

For ordinary bugs without sensitive security details, use the public bug-report issue form.

## Security boundary

The supported packaged runtime binds the web UI to host loopback and keeps Ollama on the private Compose network. Project Paper has no accounts, public server mode, or automatic telemetry. Optional external providers and broader network exposure are outside the initial supported configuration.
