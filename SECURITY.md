# Security policy

## Supported versions

Security fixes target the latest release and `main`. The project is currently in the 0.1.x alpha
series; older releases do not have a separate security maintenance commitment.

## Report a vulnerability privately

Use GitHub's **Report a vulnerability** option in this repository's
[Security tab](https://github.com/sensebeing/the-guard/security). If private reporting is unavailable,
open a minimal issue asking maintainers for a private reporting channel, without vulnerability
details. Do not disclose exploits, credentials, private code, or sensitive logs in public issues.

Include the affected version or commit, operating system, reproduction steps, expected trust
boundary, and potential impact. Redact secrets and use a minimal synthetic repository when possible.
Coordinate disclosure with maintainers while they investigate and prepare a fix.

## Trust boundaries

`the-guard` runs local tools and invokes your configured review agent on repository content.
Review that agent's permissions and data-handling settings before sharing private code.
Policies, local receipts, and Git hooks are developer-side controls; they are not a sandbox or
server-side branch protection. Configure trusted CI and repository permissions for remote enforcement.
