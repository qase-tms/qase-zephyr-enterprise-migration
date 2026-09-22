"""Preflight check, validate config and connectivity BEFORE running the migration.

Run:  python preflight.py [config.json]

Checks, in order:
  1. Config file parses; required keys present, no placeholder values, project
     selection unambiguous, status/priority overrides use real Qase slugs
  2. Zephyr Enterprise API auth works (GET project/lite) and every
     projects.import name exists on the server
  3. Per-project data sanity: releases and repository folders (a project with
     neither usually means the wrong name, not an API problem)
  4. Zephyr users: how many are active and how many are not, because
     users.create consumes a Qase seat per user
  5. Qase API auth works (GET /v1/project) and users.default resolves

Exit code 0 = all green; 1 = at least one failure.
"""

import sys

# asyncio.TaskGroup, used across the entity importers, is Python 3.11+.
if sys.version_info < (3, 11):
    sys.exit(
        f"This migration requires Python 3.11 or newer "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )

import requests

from src.support.config_manager import ConfigManager, ConfigError
from src.support.logger import Logger
from src.api.zephyr_enterprise import ZephyrEnterpriseApiClient
from src.exceptions.api import APIError

_PLACEHOLDER_MARKERS = ("<", ">", "your-", "YOUR_", "changeme", "xxxx")

# Qase slugs the mapping code can actually emit
_VALID_CASE_STATUS = {"actual", "draft", "deprecated"}
_VALID_RESULT_STATUS = {
    "passed", "failed", "blocked", "skipped", "in_progress", "untested", "invalid",
}

_results = []


def _report(name: str, ok: bool, detail: str = ""):
    icon = "✅" if ok else "❌"
    print(f"  {icon} {name}" + (f": {detail}" if detail else ""))
    _results.append(ok)


def _warn(name: str, detail: str = ""):
    print(f"  ⚠️  {name}" + (f": {detail}" if detail else ""))


def _looks_placeholder(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def _project_names(raw) -> list:
    out = []
    for p in raw or []:
        if isinstance(p, dict):
            name = p.get("name")
        else:
            name = p
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "./config.json"

    print("\n- Config -")
    config = ConfigManager(config_file=config_path)
    try:
        config.load_config()
    except ConfigError as e:
        _report(f"Config file {config_path}", False, str(e))
        return _finish()
    _report(f"Config file {config_path}", True, "parses OK")

    required = {
        "qase.api_token": "Qase API token",
        "zephyr.api_token": "Zephyr Enterprise API token",
        "zephyr.host": "Zephyr Enterprise base URL",
    }
    config_ok = True
    for key, label in required.items():
        value = str(config.get(key) or "").strip()
        if not value:
            _report(f"{key} ({label})", False, "missing/empty")
            config_ok = False
        elif _looks_placeholder(value):
            _report(f"{key} ({label})", False, f"looks like a placeholder: {value[:40]!r}")
            config_ok = False
        else:
            _report(f"{key} ({label})", True)

    host = str(config.get("zephyr.host") or "").strip()
    if host and "/flex/html5" in host.lower():
        _report(
            "zephyr.host", False,
            "points at the UI. Use the server root only "
            "(https://zephyr.example.com), with no /flex/html5 path",
        )
        config_ok = False

    auth_mode = str(config.get("zephyr.auth") or "bearer").strip().lower()
    if auth_mode not in ("bearer", "basic"):
        _report("zephyr.auth", False, f"must be 'bearer' or 'basic', got {auth_mode!r}")
        config_ok = False
    elif auth_mode == "basic":
        creds = [
            str(config.get(k) or "").strip()
            for k in ("zephyr.username", "zephyr.password")
        ]
        if not all(creds):
            _report(
                "zephyr.auth", False,
                "set to 'basic' but zephyr.username / zephyr.password are not both set",
            )
            config_ok = False
        else:
            _report("zephyr.auth", True, "basic")

    import_all = bool(config.get("projects.import_all"))
    projects = [
        p for p in _project_names(config.get("projects.import")) if not _looks_placeholder(p)
    ]
    exclude = _project_names(config.get("projects.exclude"))

    if import_all:
        _report(
            "projects.import_all", True,
            "every Zephyr Enterprise project on the server"
            + (f", excluding {exclude}" if exclude else ""),
        )
    elif not projects:
        _report(
            "projects.import", False,
            "empty, list the Zephyr project names to migrate "
            "(or set projects.import_all: true)",
        )
        config_ok = False
    else:
        overlap = sorted({p.lower() for p in projects} & {p.lower() for p in exclude})
        if overlap:
            _report(
                "projects.import", False,
                f"{overlap} appear in BOTH projects.import and projects.exclude, "
                f"remove them from one side",
            )
            config_ok = False
        else:
            _report("projects.import", True, f"{len(projects)} project name(s): {projects}")

    # cases.priority_map values are numeric Qase priority ids: the mapping code
    # calls int() on them and silently ignores anything else.
    priority_map = config.get("cases.priority_map") or {}
    if not isinstance(priority_map, dict):
        _report("cases.priority_map", False, f"must be an object, got {type(priority_map).__name__}")
        config_ok = False
    else:
        bad = {}
        for k, v in priority_map.items():
            try:
                int(v)
            except (TypeError, ValueError):
                bad[k] = v
        if bad:
            _report(
                "cases.priority_map", False,
                f"values must be numeric Qase priority ids, got {bad}. "
                f"Find them in Qase under Settings > Fields > Priority.",
            )
            config_ok = False
        elif priority_map:
            _report("cases.priority_map", True, f"{len(priority_map)} override(s)")

    for cfg_key, valid, label in (
        ("cases.status_map", _VALID_CASE_STATUS, "Qase case status"),
        ("runs.status_map", _VALID_RESULT_STATUS, "Qase result status"),
    ):
        mapping = config.get(cfg_key) or {}
        if not isinstance(mapping, dict):
            _report(cfg_key, False, f"must be an object, got {type(mapping).__name__}")
            config_ok = False
            continue
        bad = {k: v for k, v in mapping.items() if str(v).strip().lower() not in valid}
        if bad:
            _report(cfg_key, False, f"invalid {label} slug(s): {bad}, valid: {sorted(valid)}")
            config_ok = False
        elif mapping:
            _report(cfg_key, True, f"{len(mapping)} override(s)")

    created_after = config.get("runs.created_after")
    if created_after and not str(created_after).isdigit():
        _report("runs.created_after", False, f"must be epoch seconds, got {created_after!r}")
        config_ok = False

    if config.get("users.create") and not str(config.get("qase.scim_token") or "").strip():
        _report(
            "users.create", False,
            "is true but qase.scim_token is empty; creating users in Qase needs the SCIM token",
        )
        config_ok = False

    level = str(config.get("logging.level") or "info").strip().lower()
    if level not in Logger.LEVELS and level not in Logger._ALIASES:
        _warn(
            f"logging.level {level!r} is not recognised",
            f"falling back to 'info'; valid: {sorted(Logger.LEVELS)}",
        )

    if not config_ok:
        return _finish()

    logger = Logger(level="error", write_to_file=False)

    # ---------------- Zephyr Enterprise ----------------
    print("\n- Zephyr Enterprise -")
    client = ZephyrEnterpriseApiClient(
        base_url=host,
        token=str(config.get("zephyr.api_token") or ""),
        logger=logger,
        max_retries=1,
        backoff_factor=1,
        auth_type=auth_mode,
        basic_username=config.get("zephyr.username"),
        basic_password=config.get("zephyr.password"),
    )
    try:
        raw = client.get("project/lite")
        server_projects = {
            str(p.get("name")).strip(): p
            for p in (raw or [])
            if isinstance(p, dict) and p.get("name")
        }
        _report(
            "GET project/lite", True,
            f"{len(server_projects)} project(s): {sorted(server_projects)[:10]}"
            + (" ..." if len(server_projects) > 10 else ""),
        )
    except (APIError, requests.exceptions.RequestException) as e:
        _report("GET project/lite", False, f"{host} -> {str(e)[:250]}")
        _warn(
            "Hint",
            "401/403 here usually means the token was revoked or the user cannot "
            "browse projects. A non-JSON body usually means zephyr.host points at "
            "the UI rather than the server root.",
        )
        return _finish()

    lower_server = {k.lower(): k for k in server_projects}
    unknown_excludes = sorted(p for p in exclude if p.lower() not in lower_server)
    if unknown_excludes:
        _warn(
            f"projects.exclude name(s) not on the server: {unknown_excludes}",
            "harmless, but check for typos",
        )

    if import_all:
        excluded_lower = {p.lower() for p in exclude}
        projects = [k for k in sorted(server_projects) if k.lower() not in excluded_lower]
        if not projects:
            _report(
                "Resolved project list", False,
                "projects.import_all resolved to zero projects "
                + (f"(everything excluded: {exclude})" if exclude else
                   "(no projects on this server)"),
            )
            return _finish()
        _report("Resolved project list", True, f"{len(projects)} project(s): {projects}")
    else:
        excluded_lower = {p.lower() for p in exclude}
        projects = [p for p in projects if p.lower() not in excluded_lower]

    for name in projects:
        real = lower_server.get(name.lower())
        if real is None:
            _report(
                f"Project {name!r}", False,
                "not found on this server; names are matched exactly as Zephyr shows them",
            )
            continue
        pid = server_projects[real].get("id")
        counts = {}
        for label, path in (
            ("releases", f"release/project/{pid}"),
            ("folders", f"testcasetree/projectrepository/{pid}"),
        ):
            try:
                data = client.get(path)
                counts[label] = len(data) if isinstance(data, list) else "?"
            except APIError:
                counts[label] = "ERR"
        _report(f"Project {real!r}", True, ", ".join(f"{k}={v}" for k, v in counts.items()))
        if counts.get("folders") == 0:
            _warn(f"Project {real!r} has no repository folders", "nothing to migrate as suites")

    # ---------------- Zephyr users ----------------
    print("\n- Zephyr users -")
    try:
        raw_users = client.get("user/filter?includeDashboardUser=true")
        users = raw_users if isinstance(raw_users, list) else []
        if not users and isinstance(raw_users, dict):
            for k in ("data", "users", "entities", "results", "items", "userDTOs"):
                if isinstance(raw_users.get(k), list):
                    users = raw_users[k]
                    break
        active = sum(1 for u in users if isinstance(u, dict) and u.get("accountEnabled", True))
        inactive = len(users) - active
        _report("GET user/filter", True, f"{len(users)} user(s): {active} active, {inactive} inactive")
        if config.get("users.create"):
            billable = active if config.get("users.only_active") is not False else len(users)
            _warn(
                "users.create is true",
                f"up to {billable} user(s) may be created in Qase via SCIM. "
                f"Each one consumes a Qase seat.",
            )
    except (APIError, requests.exceptions.RequestException) as e:
        _warn("GET user/filter failed", f"{str(e)[:200]} (users will fall back to users.default)")

    # ---------------- Qase ----------------
    print("\n- Qase -")
    from src.service.qase import qase_api_url, qase_scim_host, is_dedicated_cluster

    qase_host = str(config.get("qase.host") or "qase.io")
    api_url = qase_api_url(config)
    if is_dedicated_cluster(qase_host):
        _report(
            "Qase host", True,
            f"{qase_host} treated as a dedicated cluster: {api_url}, SCIM at {qase_scim_host(config)}",
        )
    try:
        resp = requests.get(
            f"{api_url}/v1/project",
            headers={"Token": str(config.get("qase.api_token"))},
            params={"limit": 1},
            timeout=(15, 30),
        )
        if resp.status_code == 200 and (resp.json() or {}).get("status"):
            total = ((resp.json().get("result") or {}).get("total")) or 0
            _report("Qase auth (GET /v1/project)", True, f"{total} project(s) in workspace")
        else:
            _report(
                "Qase auth (GET /v1/project)", False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return _finish()
    except requests.exceptions.RequestException as e:
        _report("Qase auth (GET /v1/project)", False, str(e)[:200])
        return _finish()

    from src.service.qase import QaseService

    try:
        qase = QaseService(config, logger)
        user_id = qase.resolve_user_id(config.get("users.default"))
        _report("users.default", True, f"resolves to Qase user id {user_id}")
    except ValueError as e:
        _report("users.default", False, str(e))
    except Exception as e:
        _report("users.default", False, f"could not be checked: {e!r}")

    return _finish()


def _finish():
    failed = _results.count(False)
    print()
    if failed:
        print(f"❌ Preflight FAILED, {failed} check(s) failed. Fix the items above before migrating.")
        sys.exit(1)
    print("✅ Preflight passed, ready to run: python start.py")
    sys.exit(0)


if __name__ == "__main__":
    main()
