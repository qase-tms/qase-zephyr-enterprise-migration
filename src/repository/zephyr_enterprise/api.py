import json
from urllib.parse import quote

from ...api.zephyr_enterprise import ZephyrEnterpriseApiClient
from ...exceptions.api import APIError


def _normalize_execution_schedule_row(item) -> dict:
    """Flatten ``releaseTestSchedule`` wrappers (e.g. ``/execution/expanded``-style rows)."""
    if not isinstance(item, dict):
        return {}
    inner = item.get("releaseTestSchedule")
    if isinstance(inner, dict):
        merged = dict(inner)
        for k, v in item.items():
            if k != "releaseTestSchedule" and k not in merged:
                merged[k] = v
        return merged
    return item


def _unwrap_execution_schedule_payload(raw) -> list:
    """Normalize GET ``/execution`` or ``/execution/user/project`` JSON (Apiary Execution group)."""
    if raw is None:
        return []
    candidates = None
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        for key in ("results", "data", "entities", "items", "schedules", "testSchedules"):
            r = raw.get(key)
            if isinstance(r, list):
                candidates = r
                break
    if not candidates:
        return []
    out = []
    for item in candidates:
        row = _normalize_execution_schedule_row(item)
        if row:
            out.append(row)
    return out


def _advancesearch_flatten_results(raw) -> list:
    """``advancesearch`` may return ``[{results: [...]}]`` or ``{results: [...]}``."""
    if raw is None:
        return []
    if isinstance(raw, list):
        merged = []
        for block in raw:
            if isinstance(block, dict):
                rs = block.get("results")
                if isinstance(rs, list):
                    merged.extend(x for x in rs if isinstance(x, dict))
        if merged:
            return merged
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        rs = raw.get("results")
        if isinstance(rs, list):
            return [x for x in rs if isinstance(x, dict)]
    return []


def _preview_json_for_log(obj, limit: int = 1600) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        s = repr(obj)
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


def _summarize_advancesearch_raw(raw) -> str:
    """One-line description of JSON shape (for logs when parse yields no rows)."""
    if raw is None:
        return "raw=None"
    try:
        if isinstance(raw, list):
            n = len(raw)
            if n == 0:
                return "raw=[]"
            b0 = raw[0]
            if isinstance(b0, dict):
                rs = b0.get("results")
                rlen = len(rs) if isinstance(rs, list) else None
                dict_count = sum(1 for x in rs if isinstance(x, dict)) if isinstance(rs, list) else 0
                return (
                    f"raw=list[{n}] block0 keys={list(b0.keys())[:14]} "
                    f"resultSize={b0.get('resultSize')!r} len(results)={rlen} "
                    f"dict_rows_in_results={dict_count} type={b0.get('type')!r}"
                )
            return f"raw=list[{n}] first_elem={type(b0).__name__}"
        if isinstance(raw, dict):
            return f"raw=dict keys={list(raw.keys())[:18]}"
        return f"raw={type(raw).__name__}"
    except Exception as e:
        return f"summarize_error={e!r}"


def _unwrap_cycles_list(raw) -> list:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("results", "data", "cycles", "items", "entities", "cycle"):
            v = raw.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
    return []


def _execution_list_query_tail(
    offset: int,
    pagesize: int,
    cycle_phase_id: int = None,
    *,
    include_anyone_user: bool = True,
) -> str:
    parts = [
        f"offset={int(offset)}",
        f"pagesize={int(pagesize)}",
        "order=orderId",
        "isascorder=true",
    ]
    if include_anyone_user:
        parts.append("includeanyoneuser=true")
    if cycle_phase_id is not None:
        parts.append(f"cyclephaseid={int(cycle_phase_id)}")
    return "&".join(parts)


def _unwrap_zephyr_case_list(response) -> list:
    """Zephyr may return a bare list or an object like ``{"nodes": [...]}``."""
    if not response:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ("nodes", "data", "testcasenodes", "testCases", "results", "entities", "items"):
            v = response.get(key)
            if isinstance(v, list):
                return v
        if "testcase" in response:
            return [response]
    return []


class ZephyrEnterpriseApiRepository:
    def __init__(self, client: ZephyrEnterpriseApiClient):
        self.client = client
    
    def get_all_users(self):
        return
    
    def get_users(self, limit = 250, offset = 0):
        return self.client.get('user/filter?includeDashboardUser=true')
    
    def get_groups(self, limit = 250, offset = 0):
        """Legacy no-op. Prefer :meth:`search_external_groups` or ``groupSet`` on ``user/filter`` users."""
        return None

    def search_external_groups(self, name: str = "", pagesize: int = 100):
        """LDAP/Crowd directory groups (not Zephyr internal QA/Guest rows on users).

        SmartBear documents:
        ``GET {server}/flex/services/rest/v3/externalGroup/search?name=&pagesize=``.
        Omit ``name`` to list up to ``pagesize`` matches.

        Not used by the migration: directory-group membership is not exposed on
        the user object the way ``groupSet`` is, so Qase groups are built from
        ``groupSet`` instead (``groups.from_user_group_sets``). Kept for custom
        scripts and future extension.
        """
        q = quote((name or "").strip(), safe="")
        try:
            ps = int(pagesize)
        except (TypeError, ValueError):
            ps = 100
        ps = max(1, min(500, ps))
        return self.client.get_server_path(
            f"flex/services/rest/v3/externalGroup/search?name={q}&pagesize={ps}"
        )
    
    def get_case_types(self):
        return
    
    def get_result_statuses(self):
        return
    
    def get_case_statuses(self):
        return
    
    def get_priorities(self):
        return
    
    def get_case_custom_fields(self):
        return self.client.get('field/entity/TestCase')
    
    def get_case_system_fields(self):
        return self.client.get('field/entity/TestCase?includsystemfield=true')
    
    def get_configurations(self, project_id: int):
        return
    
    def get_children(self, tree_id: int) -> list:
        return self.client.get(f'testcasetree/hierarchy/{tree_id}')
    
    def get_projects(self, limit = 250, offset = 0):
        return self.client.get('project/lite')
    
    def get_suite(self, tree_id: int):
        return self.client.get(f'testcasetree/{tree_id}')
    
    def get_releases(self, project_id: int):
        return self.client.get(f'release/project/{project_id}')
    
    def get_suites_by_release(self, release_id: int):
        return self.client.get(f'testcasetree/phases/{release_id}')
    
    def get_cases_for_suite(self, suite_id: int) -> list:
        raw = self.client.get(f"testcase/nodes?treeids={suite_id}")
        cases = _unwrap_zephyr_case_list(raw)
        if cases:
            return cases
        offset = 0
        pagesize = 250
        merged = []
        while True:
            page_raw = self.client.get(
                f"testcase/tree/{suite_id}?offset={offset}&pagesize={pagesize}"
            )
            page = _unwrap_zephyr_case_list(page_raw)
            if not page:
                break
            merged.extend(page)
            if len(page) < pagesize:
                break
            offset += pagesize
        return merged
    
    def get_suites(self, project_id, offset = 0, limit = 100):
        return
    
    def get_sections(self, project_id: int, limit: int = 100, offset: int = 0, suite_id: int = 0):
        return
    
    def get_shared_steps(self, project_id: int, limit: int = 250, offset: int = 0):
        return
    
    def get_runs(self, project_id: int, suite_id: int = 0, created_after: int = 0, limit: int = 250, offset: int = 0):
        return

    def _get_execution_json(self, uri: str, *, log_failures: bool = True):
        try:
            return self.client.get(uri)
        except APIError as e:
            if log_failures:
                lg = getattr(self.client, "logger", None)
                if lg:
                    lg.log(
                        f"[Zephyr][Execution] GET failed — {e!s} — {uri[:200]}",
                        "warn",
                    )
            return None

    def get_cycles_for_release(self, project_id: int, release_id: int) -> list:
        """Cycles for a release.

        **Order matters:** ``GET cycle/release/{{id}}`` (Apiary) is usually fast. Some servers
        respond very slowly or retry for minutes on ``cycle?projectId=…&releaseId=…`` — that
        was tried first historically and dominated wall time. We try ``cycle/release`` first,
        then the query-param variants for older servers.
        """
        rid = int(release_id)
        pid = int(project_id)
        for uri in (
            f"cycle/release/{rid}",
            f"cycle?projectId={pid}&releaseId={rid}",
            f"cycle?projectid={pid}&releaseid={rid}",
        ):
            try:
                raw = self.client.get(uri)
            except APIError:
                continue
            rows = _unwrap_cycles_list(raw)
            if rows:
                return rows
        return []

    def get_cycle(self, cycle_id: int):
        """``GET cycle/{{id}}`` — cycle name, ``cyclePhases`` (phase names), dates."""
        try:
            raw = self.client.get(f"cycle/{int(cycle_id)}")
            return raw if isinstance(raw, dict) else None
        except APIError:
            return None

    def get_executions_by_cycle_phase(self, cycle_id: int, phase_id: int) -> list:
        """``GET execution?cycleId=&phaseId=`` (docs) and Apiary-style ``cyclephaseid``."""
        uris = (
            f"execution?cycleId={int(cycle_id)}&phaseId={int(phase_id)}",
            f"execution?cycleid={int(cycle_id)}&phaseid={int(phase_id)}",
            f"execution?cyclephaseid={int(phase_id)}",
        )
        for uri in uris:
            raw = self._get_execution_json(uri, log_failures=False)
            rows = _unwrap_execution_schedule_payload(raw)
            if rows:
                return rows
        return []

    def search_executions_advancesearch(
        self,
        release_id: int,
        firstresult: int,
        maxresults: int,
        word: str = "*",
        *,
        append_zql_word: bool = False,
        log_diagnostics: bool = False,
    ) -> list:
        """GET ``advancesearch?entitytype=execution&releaseid=...&firstresult=...&maxresults=...``.

        Zephyr often returns ``[{ "results": [...], "resultSize": n, "type": "testSchedule" }]``.
        Set ``append_zql_word=True`` to add ``&zql=false&word=...`` (legacy / some servers).
        """
        base = (
            f"advancesearch?entitytype=execution&releaseid={int(release_id)}"
            f"&firstresult={int(firstresult)}&maxresults={int(maxresults)}"
        )
        if append_zql_word:
            w = str(word if word is not None else "*")
            uri = f"{base}&zql=false&word={quote(w)}"
        else:
            uri = base
        raw = self._get_execution_json(uri, log_failures=True)
        if raw is None:
            return []
        rows = _advancesearch_flatten_results(raw)
        lg = getattr(self.client, "logger", None)
        if lg:
            if log_diagnostics:
                lg.log(
                    f"[Zephyr][advancesearch] GET {uri} → parsed_rows={len(rows)} "
                    f"raw={_preview_json_for_log(raw)}",
                    "info",
                )
            elif int(firstresult) == 0 and not rows:
                lg.log(
                    f"[Zephyr][advancesearch] releaseid={int(release_id)} "
                    f"firstresult=0 → 0 rows after parse. {_summarize_advancesearch_raw(raw)}",
                    "info",
                )
        return rows

    def search_executions_for_release(
        self,
        release_id: int,
        project_id: int = None,
        cycle_phase_id: int = None,
        offset: int = 0,
        pagesize: int = 100,
        list_scope: str = None,
    ) -> list:
        """Paged release test schedules (executions) per Apiary ``GET /execution`` or ``/execution/user/project``."""
        scope = (list_scope or "auto").strip().lower()
        if scope == "project":
            prefer_project = project_id is not None
        elif scope == "release":
            prefer_project = False
        else:
            prefer_project = project_id is not None

        def try_uris(tail: str) -> list:
            rows = []
            if prefer_project and project_id is not None:
                u = (
                    f"execution/user/project?projectid={int(project_id)}"
                    f"&releaseid={int(release_id)}&{tail}"
                )
                rows = _unwrap_execution_schedule_payload(self._get_execution_json(u))
            if not rows:
                u2 = f"execution?releaseid={int(release_id)}&{tail}"
                rows = _unwrap_execution_schedule_payload(self._get_execution_json(u2))
            return rows

        # Apiary: stable paging often needs order + isascorder; some servers reject includeanyoneuser.
        tail_inc = _execution_list_query_tail(
            offset, pagesize, cycle_phase_id, include_anyone_user=True
        )
        tail_no_inc = _execution_list_query_tail(
            offset, pagesize, cycle_phase_id, include_anyone_user=False
        )

        rows = try_uris(tail_inc)
        if not rows:
            rows = try_uris(tail_no_inc)

        # ``pagesize=0`` returns all rows (Apiary). Only safe at offset 0 (else we'd duplicate the full set each page).
        if not rows and int(pagesize) != 0 and int(offset) == 0:
            tail0 = _execution_list_query_tail(
                0, 0, cycle_phase_id, include_anyone_user=True
            )
            rows = try_uris(tail0)
            if not rows:
                tail0b = _execution_list_query_tail(
                    0, 0, cycle_phase_id, include_anyone_user=False
                )
                rows = try_uris(tail0b)

        if not rows:
            min_parts = [f"offset={int(offset)}", f"pagesize={int(pagesize)}"]
            if cycle_phase_id is not None:
                min_parts.append(f"cyclephaseid={int(cycle_phase_id)}")
            min_tail = "&".join(min_parts)
            if prefer_project and project_id is not None:
                u = (
                    f"execution/user/project?projectid={int(project_id)}"
                    f"&releaseid={int(release_id)}&{min_tail}"
                )
                rows = _unwrap_execution_schedule_payload(self._get_execution_json(u))
            if not rows:
                u2 = f"execution?releaseid={int(release_id)}&{min_tail}"
                rows = _unwrap_execution_schedule_payload(self._get_execution_json(u2))

        return rows
    
    def get_results(self, run_id: int, limit: int = 250, offset: int = 0):
        return
    
    def get_attachment(self, attachment):
        return
    
    def get_attachments_list(self):
        return

    def get_testcase_detail(self, testcase_version_id: int):
        """Best-effort full testcase JSON (paths vary by Zephyr version).

        ``max_retries=0, backoff_factor=0`` is deliberate (same rationale as
        :meth:`get_testcase_teststep`): this is enrichment, not load-bearing. A
        version that 404s the primary path, or returns 204/500 on a tree-node id,
        must NOT burn ``backoff_factor * 2**attempt`` sleeps per case — with the
        client defaults (``max_retries=5, backoff_factor=5``) that is ~155s of
        pure backoff per failing case and was the dominant cost stalling large
        migrations. One attempt per path; fall through to ``None`` so the case
        still imports (steps/priority degrade gracefully).
        """
        for path in (
            f"testcase/{testcase_version_id}",
            f"testcase/detail/{testcase_version_id}",
        ):
            try:
                return self.client.get(path, max_retries=0, backoff_factor=0)
            except APIError:
                continue
        return None

    def get_testcase_teststep(self, testcase_id: int) -> list:
        """Manual test-step rows for a testcase (Zephyr UI: Test Case → Test Steps).

        ``GET /flex/services/rest/latest/testcase/{id}/teststep`` → Zephyr returns
        ``{id, tcId, maxId, steps: [...], testcaseVersionId, ...}``. Each step row
        has ``step`` (action), ``data`` (input), ``result`` (expected), ``orderId``.

        Important: pass the base ``testcase.id`` from the tree payload's inner
        ``testcase`` dict, **not** the tree-node / TCRTT id. Tree-node ids often
        return HTTP 204 (no content) or 500 — both silently swallowed here so a
        missing step-collection never breaks import. The legacy testcase-detail
        endpoint does not include step arrays at all, which is why steps were
        previously absent for any case whose ``testcase.id`` differs from the
        tree-node id (e.g. multi-version cases).

        ``max_retries=0`` is deliberate: :class:`ZephyrEnterpriseApiClient.send_request`
        loops on anything with ``status_code > 201`` (including 204), sleeping
        ``backoff_factor * 2**attempt`` between tries. With the default
        ``backoff_factor=5`` and ``max_retries=7`` that would stall the import
        for ≈21 min per empty step collection; a single pass is enough here.
        """
        try:
            tcid = int(testcase_id)
        except (TypeError, ValueError):
            return []
        try:
            raw = self.client.get(
                f"testcase/{tcid}/teststep",
                max_retries=0,
                backoff_factor=0,
            )
        except APIError:
            return []
        if not isinstance(raw, dict):
            return []
        rows = raw.get("steps")
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def try_fetch_testcase_attachments(self, item_id: int):
        """Returns ``(uri, raw_dict)`` for diagnostics, or ``(None, None)`` when no rows.

        ``raw_dict`` uses ``attachments`` so :meth:`_normalize_attachment_payload` consumers work.
        """
        uri, rows = self._collect_attachment_list_for_item_id(
            item_id, extended_probe=False
        )
        if not rows:
            return None, None
        return uri, {"attachments": rows}

    def _attachment_list_get_and_merge(
        self,
        uri: str,
        merged: list,
        seen_flex: set,
        seen_no_flex: set,
    ) -> None:
        try:
            raw = self.client.get(
                uri,
                read_timeout=self.client.attachment_list_read_timeout,
                max_retries=self.client.attachment_list_max_retries,
                backoff_factor=self.client.attachment_list_backoff_factor,
            )
            for a in self._normalize_attachment_payload(raw):
                if not isinstance(a, dict):
                    continue
                flex_id = self._attachment_flex_file_id(a)
                if flex_id:
                    if flex_id in seen_flex:
                        continue
                    seen_flex.add(flex_id)
                else:
                    row_sig = (a.get("id"), a.get("name"))
                    if row_sig in seen_no_flex:
                        continue
                    seen_no_flex.add(row_sig)
                merged.append(a)
        except APIError:
            pass

    def _collect_attachment_list_core(self, item_id: int) -> tuple:
        """Merge v3 ``/rest/v3/attachment`` (UI parity) then legacy ``latest/attachment/list``."""
        merged: list = []
        seen_flex: set = set()
        seen_no_flex: set = set()
        last_uri = None
        for itype in ("testcase", "tcrcatalogtree"):
            try:
                v3_rows = self.client.fetch_v3_attachment_list(item_id, itype)
            except APIError:
                v3_rows = []
            if v3_rows:
                last_uri = f"{self.client.attachment_v3_path}?itemid={item_id}&type={itype}"
            for a in v3_rows:
                if not isinstance(a, dict):
                    continue
                flex_id = self._attachment_flex_file_id(a)
                if flex_id:
                    if flex_id in seen_flex:
                        continue
                    seen_flex.add(flex_id)
                else:
                    row_sig = (a.get("id"), a.get("name"))
                    if row_sig in seen_no_flex:
                        continue
                    seen_no_flex.add(row_sig)
                merged.append(a)
        # The v3 endpoint is the Zephyr UI's source of truth for case-tab files. When
        # it already returned rows, the legacy ``attachment/list`` shapes are redundant
        # (and on v3-capable servers they 404). Only fall back to legacy when v3 found
        # nothing, so v3-capable servers skip up to 4 extra requests per case.
        if not merged:
            for item_type in ("testcase", "tcrcatalogtree"):
                for uri in (
                    f"attachment/list?itemId={item_id}&itemType={item_type}",
                    f"attachment/list?itemType={item_type}&itemId={item_id}",
                ):
                    last_uri = uri
                    self._attachment_list_get_and_merge(
                        uri, merged, seen_flex, seen_no_flex
                    )
        return last_uri, merged

    def _collect_attachment_list_extended(self, item_id: int) -> tuple:
        """Slow, opt-in: extra ``itemType`` values + alternate URLs (often 404; use sparingly)."""
        merged: list = []
        seen_flex: set = set()
        seen_no_flex: set = set()
        last_uri = None
        for item_type in (
            "testcasenode",
            "TestCaseNode",
            "TESTCASE",
            "TCRCATALOGTREE",
            "TCR",
            "tcr",
        ):
            for uri in (
                f"attachment/list?itemId={item_id}&itemType={item_type}",
                f"attachment/list?itemType={item_type}&itemId={item_id}",
            ):
                last_uri = uri
                self._attachment_list_get_and_merge(
                    uri, merged, seen_flex, seen_no_flex
                )
        for uri in (
            f"testcase/{item_id}/attachments",
            f"testcase/{item_id}/attachment",
            f"testcase/attachment?testcaseId={item_id}",
        ):
            last_uri = uri
            self._attachment_list_get_and_merge(uri, merged, seen_flex, seen_no_flex)
        return last_uri, merged

    def _collect_attachment_list_for_item_id(
        self, item_id: int, *, extended_probe: bool = False
    ) -> tuple:
        """GET ``attachment/list`` for one id. Use ``extended_probe`` only when troubleshooting."""
        last_uri, merged = self._collect_attachment_list_core(item_id)
        if extended_probe and not merged:
            lu, extra = self._collect_attachment_list_extended(item_id)
            last_uri = lu or last_uri
            merged = extra
        return last_uri, merged

    def _normalize_attachment_payload(self, raw) -> list:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in (
                "attachments",
                "data",
                "results",
                "items",
                "genericAttachments",
                "attachmentList",
                "rows",
                "values",
            ):
                v = raw.get(key)
                if isinstance(v, list):
                    return v
        return []

    @staticmethod
    def _attachment_flex_file_id(item: dict):
        """UUID used by ``/flex/download?fileId=…`` (avoid generic numeric ``id``)."""
        if not isinstance(item, dict):
            return None
        for k in ("fileId", "file_id", "refId", "ref_id", "uuid"):
            v = item.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        for k in ("genericAttachmentId", "attachmentId"):
            v = item.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s and not s.isdigit():
                return s
        return None

    def get_attachments_case(self, case_id: int):
        uri, items = self._collect_attachment_list_for_item_id(
            case_id, extended_probe=False
        )
        return {"attachments": items, "_zephyr_attachment_uri": uri}

    def get_attachments_case_merged(
        self,
        testcase_id: int = None,
        catalog_node_id: int = None,
        *,
        extended_probe: bool = False,
        skip_list_fetch: bool = False,
    ):
        """Merge ``attachment/list`` for testcase id and catalog node id (Zephyr varies which id has case-level files)."""
        merged = []
        seen_flex: set = set()
        seen_no_flex: set = set()
        last_uri = None
        if skip_list_fetch:
            return {"attachments": [], "_zephyr_attachment_uri": None}
        ids_ordered = []
        for x in (testcase_id, catalog_node_id):
            if x is None:
                continue
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi not in ids_ordered:
                ids_ordered.append(xi)
        # ``ids_ordered`` is testcase_id first, then catalog_node_id. Query the
        # testcase id and ONLY fall back to the catalog node id if it found
        # nothing. Querying ``itemid=<node_id>&type=testcase`` when the testcase
        # id already returned files both double-counts and MIS-ATTRIBUTES:
        # tree-node ids and testcase ids share one integer space, so a case's
        # node id frequently equals a *different* testcase's id, and Zephyr then
        # returns that other case's attachment (confirmed: node 1532 → testcase
        # 1532's file). The node-id query stays only as a fallback for older
        # Zephyr layouts that store case files under the node id.
        for iid in ids_ordered:
            uri, rows = self._collect_attachment_list_for_item_id(
                iid, extended_probe=extended_probe
            )
            last_uri = uri or last_uri
            for a in rows:
                if not isinstance(a, dict):
                    continue
                flex_id = self._attachment_flex_file_id(a)
                if flex_id:
                    if flex_id in seen_flex:
                        continue
                    seen_flex.add(flex_id)
                else:
                    row_sig = (a.get("id"), a.get("name"))
                    if row_sig in seen_no_flex:
                        continue
                    seen_no_flex.add(row_sig)
                merged.append(a)
            if merged:
                break
        return {"attachments": merged, "_zephyr_attachment_uri": last_uri}

    def get_attachments_for_execution(self, execution_id: int) -> list:
        """Result-attachment files for an execution row (Zephyr UI: Test Run \u2192 result attachments).

        Hits only ``/v3/attachment?itemid=<execution.id>&type=releaseTestSchedule``
        (camelCase, case-sensitive). Probe-confirmed: legacy ``attachment/list`` shapes
        404 for executions, so skipping them avoids ``2 retries \u00d7 backoff`` per call.
        """
        try:
            rows = self.client.fetch_v3_attachment_list(
                int(execution_id), "releaseTestSchedule"
            )
        except APIError:
            return []
        return [r for r in (rows or []) if isinstance(r, dict)]

    def download_attachment_by_file_id(self, file_id: str) -> tuple:
        """Download file bytes via Zephyr ``/flex/download`` (used by UI / rich text)."""
        fid = str(file_id).strip()
        last_err = None
        for rel in (
            f"flex/download?action=download&fileId={fid}",
            f"flex/download?fileId={fid}",
        ):
            try:
                return self.client.download_flex_asset(rel)
            except APIError as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise APIError(f"Could not download attachment fileId={fid!r}")

    def get_test(self, test_id: int):
        return
    
    def get_tests(self, run_id: int, limit: int = 250, offset: int = 0):
        return
    
    def get_plans(self, project_id: int, limit: int = 250, offset: int = 0):
        return
    
    def get_plan(self, plan_id: int):
        return
    
    def get_milestones(self, project_id: int, limit: int = 250, offset: int = 0):
        return self.client.get(f'release/paged/project/{str(project_id)}?pagesize={limit}&offset={offset}')
    
    def get_root_suites(self, project_id: int, limit: int = 100, offset: int = 0):
        return self.client.get(f'testcasetree/projectrepository/{project_id}')
    
    def get_cases(self, suite_id: int = 0, limit: int = 250, offset: int = 0):
        return self.client.get(f'testcase/tree/{suite_id}?offset={offset}&pagesize={limit}')