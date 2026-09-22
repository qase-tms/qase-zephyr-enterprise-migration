# Zephyr Enterprise to Qase

Migrates test cases, folders, releases, executions and attachments from a self-hosted **Zephyr Enterprise** server into **Qase**.

---

## 1. What this migrates

**Source:** Zephyr Enterprise, self-hosted (the on-premise product served at `https://<your-server>/flex/`). The migration talks to the REST API under `/flex/services/rest/latest/` plus a small number of `v3` endpoints for attachments and directory groups.

**Supported**

- Zephyr Enterprise 7.x and later, on your own infrastructure
- Bearer token authentication (the default), or HTTP Basic with a username and password
- Any Qase workspace: public cloud (`qase.io`) or a dedicated cluster

**Not supported**

- **Zephyr Scale** and **Zephyr Squad** are different products with different APIs. Use `qase-zephyr-scale-migration` or `qase-zephyr-essential-migration` instead. Nothing in this repository will work against them.
- **Zephyr Enterprise Cloud.** This targets the self-hosted product.

---

## 2. Coverage table

| Zephyr Enterprise | Qase | Status | Notes |
|---|---|---|---|
| Project | Project | Migrated | Code derived from the name, or set explicitly with `projects.mapping`. An existing Qase project with the same title is reused rather than duplicated |
| Repository folder | Suite | Migrated | The whole folder tree, nested |
| Release phase tree | Suite | Migrated | Phase folders are migrated as suites alongside the repository tree |
| Test case | Test case | Migrated | Title, description, steps, priority, status, tags, custom field values |
| Test step | Step | Migrated | Fetched from `testcase/{id}/teststep`; `externalId` becomes expected-result steps when a case has no step rows |
| Release | Milestone | Migrated | Runs are attached to the milestone for their release |
| Cycle + phase | Test run | Migrated | One Qase run per cycle and phase by default |
| Execution | Result | Migrated | Status, executed-by, execution date, comment |
| Case attachment | Attachment | Migrated | Downloaded during case import; files over 32 MiB are skipped with a warning |
| Execution attachment | Attachment | Migrated | Only fetched when the execution reports `attachmentCount > 0` |
| Inline image in rich text | Attachment | Migrated | The `flex/download` URL in the text is rewritten to the uploaded Qase file |
| Custom field definition | Custom field | Migrated | Filtered by `cases.fields`; unsupported Zephyr field types are skipped with a reason |
| User | Qase user | Optional | Matched by email. Created over SCIM only when `users.create` is true |
| User group | Qase group | Optional | Built from each user's `groupSet`. Requires `groups.create` and a SCIM token |
| Requirement | Nothing | Not migrated | Qase has no requirement entity |
| Shared step | Nothing | Not migrated | Zephyr Enterprise has no shared-step concept |
| Configuration | Nothing | Not migrated | Zephyr Enterprise has no equivalent |

---

## 3. Known limitations

Read this section before promising anything to a stakeholder.

**A second run creates duplicate test runs.** There is no deduplication on runs at all. Running the migration twice against the same Qase project produces a complete second set of test runs and results. See section 10.

**One Qase case per Zephyr test case, not per placement.** A Zephyr test case linked into several repository folders or release phases exists once in Zephyr and is created once in Qase, under the first folder processed. Set `zephyr.deduplicate_cases` to `false` to create one Qase case per placement instead, which produces intentional duplicates.

**Estimated time per case is lost.** The Qase bulk-case model has no `estimate` field, so a Zephyr case's estimated time cannot be carried across.

**Milestone creation dates are not preserved.** The Qase milestone model has no `created_at`, so every migrated release shows the migration date.

**Requirements are not migrated.** Qase has no requirement entity. Requirement links on a case are dropped.

**Attachments are not counted in the statistics.** The `attachments` line in the stats file is always `0` for both systems. Attachments themselves are migrated; only the counter is unpopulated. Use the migration report and the log to confirm what was uploaded.

**The source-side statistics are incomplete.** `suites` and `runs` are counted on the Qase side only, so the source column reads `0` for those two rows. `cases` and `milestones` are counted on both sides and can be compared directly.

**Files over 32 MiB are skipped.** Zephyr downloads are fully buffered in memory before upload, so oversized files are skipped and reported rather than risking the run.

**Zephyr repository folder names are sometimes unusable.** Some servers return control characters or template fragments in root folder names. These are migrated as-is; rename them in Zephyr first if that matters.

---

## 4. Prerequisites

**Python 3.11 or newer.** The importers use `asyncio.TaskGroup`, which does not exist before 3.11. `start.py` and `preflight.py` both refuse to run on anything older rather than failing halfway through.

### Zephyr Enterprise access

1. Sign in to Zephyr Enterprise as a user who can browse every project you intend to migrate.
2. Open your own profile menu in the top-right and choose **Settings**, then the **API Token** section. An administrator can instead generate one for a user under `Administration > User Management`. The exact menu label moved between 7.x releases, so use step 4 to confirm you have the right value rather than trusting the label.
3. Press **Generate** and copy the token. It is shown once.
4. Confirm the token works before going any further:

   ```bash
   curl -s -H "Authorization: Bearer <token>" \
     "https://zephyr.example.com/flex/services/rest/latest/project/lite" | head -c 300
   ```

   A JSON array of projects means the token is good. An HTML page means the URL is wrong, usually because it points at the UI. A `401` means the token is wrong or revoked.

5. Put the token in `config.json` as `zephyr.api_token`, or export it as `ZEPHYR_ENTERPRISE_API_TOKEN`.
6. Put the **server root** in `zephyr.host`, for example `https://zephyr.example.com`. Do not include `/flex/html5` or any UI path.

If your server does not issue API tokens, set `zephyr.auth` to `"basic"` and fill in `zephyr.username` and `zephyr.password` instead.

**Permissions required:** the account needs the **Test Manager** role, or any role that grants read access to the repository, releases, cycles and executions of every project being migrated. Read access is sufficient throughout; the migration never writes to Zephyr.

### Qase access

1. Open `https://app.qase.io/user/api/token`.
2. Press **Create new token**, give it **read and write** access to projects, test cases, test runs and attachments.
3. Put it in `config.json` as `qase.api_token`, or export it as `QASE_API_TOKEN`.

The Qase user who owns the token needs a role that can create projects. If it cannot, pre-create the target projects in Qase and name them exactly as the Zephyr projects are named, or map them explicitly with `projects.mapping`; the migration reuses an existing project rather than failing.

### Qase SCIM token (only for migrating users or groups)

Needed only when `users.create` or `groups.create` is `true`. Leave `qase.scim_token` empty and neither step runs; every migrated entity is then attributed to `users.default`.

1. Open `https://app.qase.io/workspace/scim` as a workspace owner.
2. Copy the SCIM token into `qase.scim_token`, or export it as `QASE_SCIM_TOKEN`.

There is no `scim_host` key. The SCIM endpoint is derived from `qase.host`, so a dedicated cluster is handled automatically.

> **Every user created over SCIM consumes a Qase seat.** `preflight.py` prints how many Zephyr users are active and how many are not, so you see the number before the run rather than on your next invoice. `users.only_active` defaults to `true` so deactivated Zephyr accounts are skipped.
>
> The run also prints the full list of users it is about to create and **waits for you to type `yes`**. There is no way to skip that prompt, so leave `users.create` at `false` for any unattended or scheduled run.

### Nothing needs to be created up front

This migration writes no source ids into Qase custom fields, so there are no fields for you to create before running. Zephyr test case ids are preserved as Qase case ids directly.

---

## 5. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Then edit `config.json`.

---

## 6. Configure

One file, `config.json`. Every key below is read by the code. It is not an exhaustive list of what the code reads; see **Keys not listed here** at the end of this section.

### `qase`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `api_token` | Yes | | Qase API token. `QASE_API_TOKEN` overrides it |
| `host` | No | `qase.io` | Your Qase host. Anything other than `qase.io` is treated as a dedicated cluster and the API, app and SCIM URLs are derived accordingly |
| `ssl` | No | `true` | Set to `false` only for an HTTP test instance |
| `scim_token` | No | `""` | SCIM token. Required for `users.create` and `groups.create`. `QASE_SCIM_TOKEN` overrides it |

### `zephyr`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `host` | Yes | | Server root, e.g. `https://zephyr.example.com`. No `/flex/html5` path |
| `api_token` | Yes | | Zephyr Enterprise API token. `ZEPHYR_ENTERPRISE_API_TOKEN` overrides it |
| `auth` | No | `bearer` | `bearer` or `basic` |
| `username` | No | `""` | Only for `auth: "basic"` |
| `password` | No | `""` | Only for `auth: "basic"`. `ZEPHYR_ENTERPRISE_PASSWORD` overrides it |
| `deduplicate_cases` | No | `true` | Create one Qase case per Zephyr test case. `false` creates one per placement |

### `projects`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `import_all` | No | `false` | Migrate every project on the server |
| `import` | No | `[]` | Zephyr project names to migrate. Matching is case-insensitive. Empty with `import_all: false` also means all |
| `exclude` | No | `[]` | Project names to skip. Always wins over `import` and `import_all` |
| `mapping` | No | `{}` | Zephyr project name to a specific Qase project code, e.g. `{"Payments Platform": "PAY"}` |

### `users`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `default` | Yes | | **The email address** of the Qase user who owns anything that cannot be attributed. A numeric Qase user id also works |
| `map` | No | `{}` | Zephyr user email (or username, when Zephyr has no email) to a **numeric Qase user id**, e.g. `{"old@corp.com": 42}`. Matched exactly, including case |
| `migrate` | No | `true` | Build the Zephyr-to-Qase user map at all. When `false`, everything is authored by `default` |
| `create` | No | `false` | Create missing users in Qase over SCIM. **Consumes a seat per user** |
| `only_active` | No | `true` | Skip Zephyr users whose account is disabled |

Author resolution order for every migrated entity: an explicit `users.map` entry, then a Qase user with the same email, then `users.default`.

### `groups`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `create` | No | `false` | Create Qase groups over SCIM. Requires `scim_token` |
| `name` | No | `Zephyr Migration` | Name of the root group |
| `name_prefix` | No | `""` | Prepended to every created group name |
| `from_user_group_sets` | No | `true` | Build groups from each Zephyr user's `groupSet` rather than one flat group |

### `cases`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `preserve_ids` | No | `true` | Send the Zephyr test case id as the Qase case id |
| `fields` | No | `[]` | Zephyr custom field names to migrate. Empty means all |
| `priority_map` | No | `{}` | Zephyr priority id or name to a **numeric Qase priority id**. Find the ids in Qase under `Settings > Fields > Priority`; they are workspace-specific, so slugs like `high` are not accepted here |
| `status_map` | No | `{}` | Zephyr `stateFlag` to a Qase case status slug (`actual`, `draft`, `deprecated`). Defaults to `{"0": "actual", "1": "draft"}` |

### `runs`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `created_after` | No | `0` | Epoch seconds. Executions older than this are skipped. `0` means all |
| `status_map` | No | `{}` | Zephyr execution status id to a Qase result status (`passed`, `failed`, `blocked`, `skipped`, `in_progress`, `untested`, `invalid`). Overlays the built-in map |

### `logging`

| Key | Required | Default | Meaning |
|---|---|---|---|
| `level` | No | `info` | `error`, `warn`, `info`, `verbose` or `debug`. Each level includes the ones above it |
| `write_to_file` | No | `true` | Also write to `logs/` |
| `dir` | No | `./logs` | Where the log file goes |

`error` and `warn` always reach the console whatever the level, so a run that quietly skipped data cannot look successful in the terminal.

### `prefix`

Optional string prepended to the log and statistics filenames. Useful for keeping several runs apart. Defaults to `zephyr-enterprise`.

### Keys not listed here

Retry counts and backoff against Qase are constants in the code, not configuration: a wrong value produces a slower or failed run and no customer has a basis on which to choose one.

The code also reads about thirty further keys under `migration.*` and `runs.*` that are **not supported configuration**. They are development toggles left from building the migration, every one of them already defaulted to the behaviour described in this README, and none has ever shipped in `config.example.json`. Do not set them: they are undocumented, unvalidated by `preflight.py`, and will be removed once this migration can be exercised against a Zephyr Enterprise server.

---

## 7. Validate

Always run this before migrating:

```bash
python preflight.py
```

It checks the config file, rejects placeholder values, verifies both APIs answer, confirms every project name in `projects.import` actually exists on the server, counts releases and folders per project, reports how many Zephyr users are active, and resolves `users.default` to a real Qase user.

A green run looks like this:

```
- Config -
  ✅ Config file ./config.json: parses OK
  ✅ qase.api_token (Qase API token)
  ✅ zephyr.api_token (Zephyr Enterprise API token)
  ✅ zephyr.host (Zephyr Enterprise base URL)
  ✅ projects.import: 1 project name(s): ['Sample Project']

- Zephyr Enterprise -
  ✅ GET project/lite: 4 project(s): ['Migration Perf Test', 'Migration to Qase', 'Sample Project', 'Sandbox']
  ✅ Project 'Sample Project': releases=2, folders=4

- Zephyr users -
  ✅ GET user/filter: 5 user(s): 4 active, 1 inactive

- Qase -
  ✅ Qase auth (GET /v1/project): 12 project(s) in workspace
  ✅ users.default: resolves to Qase user id 1

✅ Preflight passed, ready to run: python start.py
```

Exit code is `0` when everything passes and `1` when anything fails.

---

## 8. Run

```bash
python start.py                    # migrate
python start.py --dry-run          # read everything, write nothing
python start.py my-config.json     # use a different config file
```

`--dry-run` runs the whole extraction and mapping pipeline and logs every write it would make, so unmapped statuses, oversized attachments and missing fields all appear in the migration report without anything being created in Qase. `QASE_DRY_RUN=1` does the same.

Exit code is `0` on success and `1` on failure.

**Expected duration.** Measured against a Zephyr Enterprise instance on the same network:

| Volume | Duration |
|---|---|
| 320 cases across 2 projects, 3 releases, 7 runs, no attachments | about 2 minutes |

That is the only volume we have measured, so treat it as a rate rather than a table: roughly 150 cases a minute when attachments are not involved. Attachments dominate everything beyond that, because a project with thousands of files is bound by download and upload time rather than by case count. Qase is called through a pool limited to 250 requests per 12 seconds, which is the rate limit rather than a tunable.

---

## 9. What good output looks like

A healthy run prints one progress line per stage, redrawing in place, then the statistics and the migration report.

```
[Users] Loading users from Qase (SCIM)...
	✓ Building users map [5/5]
	✓ Importing projects [4/4]
	✓ Importing custom fields [2/2]
	↪ Importing project: Migration to Qase
	  ✓ [MTQ] Importing milestones [1/1]
	  ✓ [MTQ] Importing test cases [300/300]
	↪ Importing project: Sample Project
	  ✓ [SP] Importing milestones [2/2]
	  ✓ [SP] Importing test cases [19/19]
```

Suites and runs have no progress line of their own; they are reported in the statistics at the end.

Warnings and errors interrupt the progress lines and are always shown, in yellow and red:

```
	! [18:22:04][warn] [SP][Tests] Skip attachment fileId='a1b2c3d4e5f6': 43212345 bytes > max 33554432
	✗ [18:22:31][error] [Fields] Error creating custom field: Test Environment
```

Then the statistics:

```
------ Stats ------

{'projects': {'SP': {'title': 'Sample Project',
                     'zephyr-enterprise': {'suites': 0,
                                           'cases': 19,
                                           'runs': 0,
                                           'milestones': 2,
                                           'shared_steps': 0,
                                           'configurations': 0},
                     'qase': {'suites': 9,
                              'cases': 19,
                              'runs': 7,
                              'milestones': 2,
                              'shared_steps': 0,
                              'configurations': 0}}},
 'source': 'zephyr-enterprise',
 'attachments': {'zephyr-enterprise': 0, 'qase': 0},
 'users': {'zephyr-enterprise': 5, 'qase': 6},
 'custom_fields': {'zephyr-enterprise': 2, 'qase': 2}}
```

Read that as: 19 Zephyr cases became 19 Qase cases, and 2 releases became 2 milestones. The `suites` and `runs` source columns are always `0` and the `attachments` line is always `0`; see section 3.

Then the migration report, which is the part worth reading:

```
------ Migration report: 2 skipped/degraded item(s) ------

  [SP] · 1 item(s)
    ! [Tests] Skip attachment fileId='a1b2c3d4e5f6': 43212345 bytes > max 33554432

  [-] · 1 item(s)
    ✗ [Fields] Error creating custom field: Test Environment

Statistics written to stats/zephyr-enterprise_stats.json and stats/zephyr-enterprise_stats.xlsx
Full log: logs/zephyr-enterprise_zephyr_enterprise_20260826_182204.log
```

On a clean run it says so explicitly rather than printing nothing:

```
------ Migration report: no skipped or degraded items ------
```

**Counts alone can overstate success.** A run that skipped a thousand attachments and dropped a custom field still reports a large number of migrated cases. The report is generated from every warning and error the run produced, so anything skipped, truncated or defaulted appears there. If the report is long, the full list is in `stats/<prefix>_stats.json` and in the `Migration report` sheet of the XLSX.

---

## 10. Re-run and resume behaviour

**There is no resume.** A run that fails halfway leaves everything it already created in Qase, and starting again begins from the top.

What a second run against the same Qase project does:

| Entity | On a second run |
|---|---|
| Project | Reused. Matched by title, not recreated |
| Custom field | Reused. Matched by name. Custom fields are workspace-global in Qase |
| Group | Reused. Matched by name |
| Suite | **Duplicated.** Created every time |
| Milestone | **Duplicated.** Created every time |
| Test case | Usually not duplicated, because `cases.preserve_ids` sends the same Zephyr-derived id each time. With `preserve_ids: false` they duplicate |
| Test run | **Duplicated.** No deduplication of any kind |
| Result | **Duplicated**, along with its run |

**The safe way to re-run is an empty target project.** Delete the Qase project and start again, or migrate into a fresh one. Use `--dry-run` first to see the scale of what a re-run would create.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `This migration requires Python 3.11 or newer` | The importers use `asyncio.TaskGroup` | Create the virtualenv with Python 3.11+ |
| `Config file not found` | No `config.json` | `cp config.example.json config.json` and fill it in |
| Preflight: `looks like a placeholder` | The example values are still in place | Replace every `<...>` value |
| `Non-JSON from ... Body starts with '<!DOCTYPE html'` | `zephyr.host` points at the UI | Use the server root only, with no `/flex/html5` |
| `Empty body from ...` | Same cause as above | Same fix |
| Zephyr `401` | Token revoked, or issued for a different user | Regenerate it and re-run preflight |
| Zephyr `403` on some projects | The token's user cannot browse them | Use an account with read access to every project being migrated |
| Preflight: `Project 'X' not found on this server` | Name mismatch | Names must match Zephyr exactly, though case is ignored. Preflight lists the real names |
| Qase `403` on project creation | Token is member-level | Use an owner or admin token, or pre-create the projects and use `projects.mapping` |
| Bulk case create fails with `422` | A custom field exists in the workspace but is not scoped to this project | Custom fields are workspace-global in Qase. Open the field in Qase and add the target project to its scope |
| No projects imported | Everything filtered out | Check `projects.import` against the names preflight printed, and check `projects.exclude` |
| All cases attributed to one user | No Zephyr user matched a Qase user by email | Add explicit entries to `users.map`, or accept `users.default` |
| Steps missing on migrated cases | Zephyr returned no rows from `testcase/{id}/teststep` | Check the log at `verbose`. Cases with no step rows fall back to `externalId` |
| Attachments missing | Skipped for size, or the execution reported no files | Search the log for `Skip attachment` and for `attachmentCount` |
| Run finished but the report is long | Data really was skipped | Read `stats/<prefix>_stats.json`; every entry names the entity and the reason |
| The run hangs at a `yes` prompt | `users.create` is true, and the run stops to confirm the SCIM user creation | Answer it. **There is no bypass**, so an unattended or scheduled run will block here indefinitely. Set `users.create` to `false` for unattended runs |

For anything else, run at `verbose` or `debug`:

```json
"logging": { "level": "verbose" }
```

`verbose` adds request URIs and status codes. `debug` adds parameters and bodies, which is a lot of output but shows exactly what Zephyr returned. No token is ever written to the log at any level.

---

## 12. Getting help

Email **migrations@qase.io**.

Include:

- The version, which is the first line of the log file and the last line the run prints
- The Zephyr Enterprise version and whether it is behind a proxy or VPN
- Your `config.json` **with every token removed**
- The tail of the log file from `logs/`
- The statistics file from `stats/`
- The migration report as printed at the end of the run

The log and statistics files contain test case content and account identifiers, so send them only over a channel you are comfortable with. Delete `logs/`, `stats/` and `config.json` once the migration is signed off.

For a suspected security issue, do not email this address. See [SECURITY.md](SECURITY.md).

Every release is listed in [CHANGELOG.md](CHANGELOG.md).
