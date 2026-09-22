# Security Policy

## Reporting a vulnerability

**Do not open a public issue or discussion, and do not email a vulnerability report to the general migrations address.**

Report privately through GitHub's private vulnerability reporting on this repository: **Security > Report a vulnerability**. This creates a private advisory visible only to the maintainers.

Please include the affected version or commit, what an attacker could achieve, and the steps to reproduce.

We will acknowledge the report and keep you updated until it is resolved.

## What this tool handles

This script reads from a self-hosted Zephyr Enterprise server and writes into Qase. Understanding what it touches will help you judge whether something is a vulnerability.

**Credentials.** Three, all read from `config.json` or from the environment:

| Credential | Needs | Used for |
|---|---|---|
| Zephyr Enterprise API token, or username + password | Read | Everything read out of Zephyr Enterprise |
| Qase API token | Read and write | Everything written into Qase |
| Qase SCIM token | Read and write, optional | Creating users and groups in Qase |

They are held in memory for the duration of the run. **No credential is written to a log file or printed at any level**, including `debug`.

**Every credential is host-scoped.** The Zephyr credential is only ever sent to the host in `zephyr.host`, including for attachment downloads, which use the same origin as the API. The Qase tokens are only ever sent to Qase. No request in this migration crosses from one system to the other.

**Source system is read-only.** Every call against Zephyr Enterprise is a `GET`. The migration has no code path that writes, updates or deletes anything on your Zephyr server, so a failed or repeated run cannot damage your source data.

**This script never contacts Jira.** Zephyr Enterprise is self-hosted and self-contained, so there is no Jira dependency and no Jira credential in the configuration.

**Users are created in Qase only when you ask for it.** `users.create` is `false` by default. When it is true and `qase.scim_token` is set, the migration creates the missing Zephyr users in your Qase workspace over SCIM. **Every user created this way consumes a Qase seat.** `preflight.py` reports how many Zephyr users are active and how many are not before you run, and the run prints the full list of users it is about to create and waits for you to type `yes` when a terminal is attached. With no terminal attached, an unattended run proceeds and logs a warning rather than hanging on the prompt, so set `users.create` deliberately.

**`users.only_active` defaults to `true`** so deactivated Zephyr accounts do not silently consume seats.

**Customer data on disk.** Test case content, releases, executions, attachments, account identifiers and internal URLs pass through the process. Two directories hold it afterwards:

- `logs/` can contain test case content and API error bodies
- `stats/` contains per-project counts and the full list of skipped or degraded items, including case titles

Both are gitignored. Neither is needed once a migration is signed off.

## Handling your own credentials

- **Prefer the environment over the file.** `QASE_API_TOKEN`, `QASE_SCIM_TOKEN`, `ZEPHYR_ENTERPRISE_API_TOKEN` and `ZEPHYR_ENTERPRISE_PASSWORD` override `config.json` and take precedence, so a config file you paste into a support ticket carries no secrets.
- Use credentials scoped to the minimum permissions the migration needs. The Zephyr credential only ever needs read access. The Qase token needs write access to projects, cases, runs and attachments; the SCIM token is only needed if `users.create` or `groups.create` is true.
- The Zephyr Enterprise token inherits the permissions of the user who created it. Create it as a user who can see only the projects being migrated.
- Revoke every token used for a migration once it is finished.
- Never commit `config.json`. It is gitignored, but a file added under a different name will not be.
- **Delete `config.json`, `logs/` and `stats/` when the migration is signed off.** Logs from a large migration can reach several gigabytes and contain full API payloads.

## If a credential is exposed

Revoke it first, then clean up. A token removed from a file but not revoked is still live, and a commit deleted from a public repository stays readable by hash.
