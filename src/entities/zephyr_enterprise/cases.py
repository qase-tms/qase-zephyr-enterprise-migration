import asyncio
import hashlib
import html
import json
import os
import re
import time
import traceback

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools

from .attachments import Attachments
from .zephyr_attachment_transform import (
    merge_zephyr_case_attachment_hashes,
    replace_zephyr_flex_urls_in_text,
)

from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from datetime import datetime

from qaseio.models import TestStepCreate, TestCasebulkCasesInner

from urllib.parse import quote

# Qase accepts int32 case ids in bulk, so preserved ids are bounded by that.
_MAX_QASE_CASE_ID = 2**31 - 1

# Zephyr downloads are fully buffered before Qase upload; skip oversized files (default 32 MiB).
_MAX_ATTACHMENT_UPLOAD_BYTES = 32 * 1024 * 1024


class _ZephyrAttachmentImport(NamedTuple):
    """Per-case attachment load bundle (Xray ``attachments_map`` + case ids + Qase hashes parity)."""

    case_level_hashes: List[str]
    case_level_file_ids: List[str]
    file_id_to_url: Dict[str, str]
    file_id_to_hash: Dict[str, str]
    file_id_to_label: Dict[str, str]


class Cases:
    @staticmethod
    def _zephyr_to_qase_case_id_for_bulk(zephyr_testcase_id: int) -> int:
        """Map Zephyr testcase id → Qase ``id`` for bulk create (``cases.preserve_ids``).

        Each bulk row carries its own ``id``, so the Zephyr-to-Qase mapping does
        not depend on ``result.ids`` or on parsing an HTTP response body.
        """
        oid = int(zephyr_testcase_id)
        if oid <= _MAX_QASE_CASE_ID:
            return oid
        hashed = int(hashlib.md5(str(oid).encode()).hexdigest()[:8], 16)
        return hashed % _MAX_QASE_CASE_ID

    def __init__(
            self,
            qase_service: QaseService,
            source_service: ZephyrEnterpriseService,
            logger: Logger,
            mappings: Mappings,
            config: Config,
            pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.pools = pools
        self.attachments = Attachments(self.qase, self.zephyr, self.logger, self.mappings, self.config, self.pools)
        self.total = 0
        self.logger.divider()

        self.project = None
        self._zephyr_discovery_logs_left = 0

    def import_cases(self, project: dict):
        return asyncio.run(self.import_cases_async(project))

    async def import_cases_async(self, project: dict):
        self.project = project
        code = project["code"]
        suite_map = self.mappings.suites.get(code, {})
        suite_ids = sorted(suite_map.keys(), key=Cases._suite_sort_key)
        sample = suite_ids[:25]
        more = f" (+{len(suite_ids) - len(sample)} more)" if len(suite_ids) > len(sample) else ""
        self.logger.log(
            f'[{code}][Tests] Starting case import: {len(suite_ids)} Zephyr tree node(s) in suite map; '
            f'ids (sample): {sample}{more}'
        )
        self._zephyr_seen_keys = set()
        self._sent_to_qase_keys = set()
        raw_log_n = self.config.get("migration.log_zephyr_case_samples_total")
        self._zephyr_discovery_logs_left = 3 if raw_log_n is None else int(raw_log_n)
        if self._zephyr_discovery_logs_left > 0:
            self.logger.log(
                f'[{code}][ZephyrDiscovery] Will log up to {self._zephyr_discovery_logs_left} sample case '
                f'payload(s); tune migration.log_zephyr_case_samples_total (0=off). '
                f'Detail GET: {bool(self.config.get("migration.log_fetch_testcase_detail_with_samples"))}; '
                f'attachment probe: {bool(self.config.get("migration.log_probe_attachment_api_on_samples", False))}.'
            )

        # Deterministic order so dedup picks the SAME winning placement on every
        # run. When a Zephyr testcase is linked into more than one repository
        # folder, the first suite processed is the one that creates the Qase case
        # (and owns its preserved id / suite placement); later placements are
        # skipped via ``_sent_to_qase_keys``. Iterating ``suite_map`` in dict
        # (Zephyr-response) order made that winner non-deterministic across runs.
        # Ascending sort: real repository suite ids before synthetic release ids
        # (>= 1_000_000, which skip Qase create anyway).
        for suite_id in suite_ids:
            await self.import_cases_for_suite(suite_id)

    @staticmethod
    def _suite_sort_key(suite_id):
        """Stable ordering key for suite ids (numeric first, then string fallback)."""
        try:
            return (0, int(suite_id))
        except (TypeError, ValueError):
            return (1, str(suite_id))

    async def import_cases_for_suite(self, suite_id):
        await self.process_cases(suite_id)

    @staticmethod
    def _zephyr_case_identity(case) -> Optional[tuple]:
        """Stable id for dedupe + _sent_to_qase_keys (testcase id when present, else catalog node id)."""
        if not isinstance(case, dict):
            return None
        tc = case.get("testcase")
        if isinstance(tc, dict):
            for key in ("id", "testcaseVersionId", "tcvId"):
                v = tc.get(key)
                if v is not None:
                    return ("tc", key, int(v) if isinstance(v, (int, float)) and v == int(v) else v)
        nid = case.get("id")
        if nid is not None:
            return ("node", int(nid) if isinstance(nid, (int, float)) and nid == int(nid) else nid)
        return None

    def _partition_cases_for_stats_and_qase(self, cases: List, suite_id: int):
        """Return (stats_increment, cases_to_send_to_qase, skipped_already_in_qase).

        When ``zephyr.deduplicate_cases`` is true (default), a Zephyr case that
        appears in multiple places (repository tree + one or more release phases) is
        created in Qase only once — later placements are skipped. Set it to false to
        migrate every placement, producing one Qase case per repo/release occurrence
        (duplicates expected).
        """
        code = self.project["code"]
        dedupe = bool(self.config.get("zephyr.deduplicate_cases", True))

        stats_increment = 0
        for c in cases:
            k = Cases._zephyr_case_identity(c)
            if not dedupe or k is None:
                stats_increment += 1
            elif k not in self._zephyr_seen_keys:
                self._zephyr_seen_keys.add(k)
                stats_increment += 1

        pending = []
        skipped_sent = 0
        nokey = 0
        for c in cases:
            k = Cases._zephyr_case_identity(c)
            if k is None:
                nokey += 1
                pending.append(c)
                continue
            if dedupe and k in self._sent_to_qase_keys:
                skipped_sent += 1
                continue
            pending.append(c)

        if nokey:
            self.logger.log(
                f'[{code}][Tests] suite_id={suite_id}: {nokey} row(s) without testcase id in payload — '
                f'cannot dedupe by id; duplicates may still occur',
                "warn",
            )
        return stats_increment, pending, skipped_sent

    async def process_cases(self, suite_id: int):
        code = self.project["code"]
        try:
            cases = await self.pools.source(self.zephyr.get_cases_for_suite, suite_id)
            if not isinstance(cases, list):
                self.logger.log(
                    f'[{code}][Tests] suite_id={suite_id}: Zephyr returned type {type(cases).__name__!r}, '
                    f'expected list — check get_cases_for_suite / API shape',
                    'warn',
                )
                n_raw = len(cases) if hasattr(cases, "__len__") else "n/a"
                self.logger.log(f'[{code}][Tests] suite_id={suite_id}: len/repr hint: len={n_raw!r}', 'warn')
                cases = []

            raw_n = len(cases)
            stats_inc, cases, skipped_in_qase = self._partition_cases_for_stats_and_qase(cases, suite_id)
            self.mappings.stats.add_entity_count(code, "cases", "zephyr-enterprise", stats_inc)
            self.logger.log(
                f'[{code}][Tests] suite_id={suite_id}: Zephyr returned {raw_n} row(s); '
                f'+{stats_inc} toward unique identity count in summary; '
                f'{len(cases)} candidate row(s) for Qase '
                f'({skipped_in_qase} skipped as already sent earlier this run)'
            )

            if suite_id >= 1000000:
                self.logger.log(
                    f'[{code}][Tests] suite_id={suite_id}: skipping Qase create (synthetic release / parent id ≥ 1000000)'
                )
                return len(cases)

            if not cases:
                return 0

            qase_suite_id = self._get_suite_id(suite_id)
            self.logger.log(
                f'[{code}][Tests] suite_id={suite_id}: mapped Qase suite_id={qase_suite_id!r} '
                f'(None may cause Qase API validation errors)'
            )

            self.logger.print_status("[" + code + "] Importing test cases", self.total, self.total + len(cases), 1)
            self.logger.log(f'[{code}][Tests] Preparing {len(cases)} case(s) for Qase bulk (Zephyr suite {suite_id})')
            data = await self._prepare_cases(cases, suite_id)
            if not data:
                self.logger.log(
                    f'[{code}][Tests] suite_id={suite_id}: _prepare_cases returned empty; nothing sent to Qase',
                    "warn",
                )
                self.total = self.total + len(cases)
                self.logger.print_status("[" + code + "] Importing test cases", self.total, self.total, 1)
                return len(cases)

            ok, qase_case_ids = await self.pools.qs(self.qase.create_cases, code, data)
            use_req_ids = bool(self.config.get("cases.preserve_ids", True))
            if ok:
                for c in cases:
                    k = Cases._zephyr_case_identity(c)
                    if k is not None:
                        self._sent_to_qase_keys.add(k)
                created = sum(1 for x in (qase_case_ids or []) if x is not None)
                self.mappings.stats.add_entity_count(code, "cases", "qase", created)
                level = "info" if created == len(data) else "warn"
                self.logger.log(
                    f'[{code}][Tests] suite_id={suite_id}: Qase create_cases created {created}/{len(data)} case(s)',
                    level,
                )
                if use_req_ids:
                    self.logger.log(
                        f'[{code}][Tests] suite_id={suite_id}: Zephyr→Qase case ids from bulk payload '
                        f'``id`` (``cases.preserve_ids``); '
                        f'{len(cases)} testcase(s) mapped before relying on API response body'
                    )
                else:
                    ids_list = list(qase_case_ids or [])
                    for j, c in enumerate(cases):
                        qid = ids_list[j] if j < len(ids_list) else None
                        if qid is None:
                            continue
                        tc0 = c.get("testcase") if isinstance(c, dict) else None
                        if isinstance(tc0, dict) and tc0.get("id") is not None:
                            try:
                                ztid = int(tc0["id"])
                                self.mappings.register_zephyr_testcase_qase_case_id(code, ztid, int(qid))
                            except (TypeError, ValueError):
                                pass
                    n_ok = sum(
                        1
                        for j in range(len(cases))
                        if j < len(ids_list) and ids_list[j] is not None
                    )
                    if n_ok < len(cases):
                        self.logger.log(
                            f'[{code}][Tests] suite_id={suite_id}: only {n_ok} Qase id(s) resolved '
                            f'for {len(cases)} case(s); runs import may miss some executions',
                            "warn",
                        )
            else:
                self.logger.log(
                    f'[{code}][Tests] suite_id={suite_id}: Qase create_cases returned False for {len(data)} case(s) '
                    f'(see [Qase][Cases] lines in log)',
                    "error",
                )
            self.total = self.total + len(cases)
            self.logger.print_status("[" + code + "] Importing test cases", self.total, self.total, 1)
            return len(cases)
        except Exception as e:
            self.logger.log(f"[{code}][Tests] Error processing cases for suite {suite_id}: {e}", "error")
            self.logger.log(f"[{code}][Tests] Traceback for suite {suite_id}:\n{traceback.format_exc()}", "error")
            return 0

    def _merge_tc_with_detail_payload(self, tc: dict, detail: dict) -> dict:
        """Fill missing testcase fields from Zephyr ``GET testcase/...`` JSON."""
        out = dict(tc)
        if not isinstance(detail, dict):
            return out
        inner = detail.get("testcase")
        if not isinstance(inner, dict):
            inner = detail
        for k, v in inner.items():
            if k not in out or out[k] in (None, "", [], {}):
                out[k] = v
        return out

    async def _prefetch_zephyr_testcase_details(
        self, code: str, suite_id: int, cases: List
    ) -> List:
        """Tree/list payloads often omit manual steps and priority; detail usually has them."""
        n = len(cases)
        out: List = [None] * n
        if not bool(self.config.get("migration.fetch_zephyr_detail_for_steps", True)):
            return out

        async def fetch_detail(idx: int, vid: int):
            try:
                return idx, await self.pools.source(self.zephyr.get_testcase_detail, vid)
            except Exception as e:
                self.logger.log(
                    f'[{code}][Tests] testcase detail fetch failed idx={idx} id={vid}: {e}',
                    "warn",
                )
                return idx, None

        jobs: List[tuple] = []
        for i, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            tc0 = case.get("testcase") or {}
            if not isinstance(tc0, dict):
                continue
            vid = tc0.get("testcaseVersionId") or tc0.get("id") or case.get("id")
            if vid is None:
                continue
            try:
                iv = int(vid)
            except (TypeError, ValueError):
                continue
            jobs.append((i, iv))

        if not jobs:
            return out

        self.logger.log(
            f'[{code}][Tests] Suite {suite_id}: prefetch Zephyr testcase detail for '
            f'{len(jobs)}/{n} case(s) (steps / priority on full testcase)'
        )
        pairs = await asyncio.gather(*[fetch_detail(i, vid) for i, vid in jobs])
        for idx, detail in pairs:
            if isinstance(detail, dict):
                out[idx] = detail
        return out

    async def _prefetch_zephyr_teststep_rows(
        self, code: str, suite_id: int, cases: List
    ) -> List[List[dict]]:
        """One ``GET testcase/{id}/teststep`` per case, fan-out over the source pool.

        The testcase detail endpoint on recent Zephyr Enterprise builds omits
        the step array entirely, so without this prefetch every case whose
        step list is not embedded in the tree row ends up with ``steps=[]``
        (previously masked only for a few legacy single-version cases where
        the externalId fallback happened to populate something).

        We key the request on the **inner** ``testcase.id`` from the tree
        row: passing the tree-node / TCRTT id (``case['id']``) returns an
        empty body or HTTP 500 on this endpoint.
        """
        n = len(cases)
        out: List[List[dict]] = [[] for _ in range(n)]
        if not bool(self.config.get("migration.fetch_zephyr_testcase_teststep", True)):
            return out

        jobs: List[tuple] = []
        for i, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            tc0 = case.get("testcase") or {}
            if not isinstance(tc0, dict):
                continue
            raw_id = tc0.get("id")
            if raw_id is None:
                continue
            try:
                tcid = int(raw_id)
            except (TypeError, ValueError):
                continue
            jobs.append((i, tcid))

        if not jobs:
            return out

        self.logger.log(
            f'[{code}][Tests] Suite {suite_id}: prefetch Zephyr testcase teststep for '
            f'{len(jobs)}/{n} case(s) (manual steps endpoint)'
        )

        async def fetch_steps(idx: int, tcid: int):
            try:
                rows = await self.pools.source(
                    self.zephyr.get_testcase_teststep, tcid
                )
            except Exception as e:
                self.logger.log(
                    f'[{code}][Tests] teststep fetch failed idx={idx} testcase_id={tcid}: {e}',
                    "warn",
                )
                return idx, []
            return idx, rows or []

        pairs = await asyncio.gather(*[fetch_steps(i, tcid) for i, tcid in jobs])
        for idx, rows in pairs:
            if isinstance(rows, list):
                out[idx] = [r for r in rows if isinstance(r, dict)]
        return out

    async def _prefetch_zephyr_attachment_lists(
        self, code: str, suite_id: int, cases: List
    ) -> List:
        """One Zephyr ``attachment/list`` per testcase, run concurrently (bounded by source thread pool)."""
        n_cases = len(cases)
        meta_by_idx: List = [None] * n_cases

        to_fetch: List[Tuple[int, Optional[int], Optional[int]]] = []
        for i, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            tc0 = case.get("testcase") or {}
            if not isinstance(tc0, dict):
                tc0 = {}
            tc_id = None
            node_id = None
            if tc0.get("id") is not None:
                try:
                    tc_id = int(tc0["id"])
                except (TypeError, ValueError):
                    pass
            if case.get("id") is not None:
                try:
                    node_id = int(case["id"])
                except (TypeError, ValueError):
                    pass
            if tc_id is not None or node_id is not None:
                to_fetch.append((i, tc_id, node_id))

        if not to_fetch:
            return meta_by_idx

        self.logger.log(
            f'[{code}][Tests] Suite {suite_id}: prefetch Zephyr attachment/list for '
            f'{len(to_fetch)}/{n_cases} case(s) in parallel (see per-id timings below)'
        )

        async def fetch_one(idx: int, tc_id: Optional[int], node_id: Optional[int]) -> tuple:
            t_list = time.monotonic()
            case_row = cases[idx] if idx < len(cases) else None
            tc_att = {}
            if isinstance(case_row, dict):
                tc_att = case_row.get("testcase") or {}
            if not isinstance(tc_att, dict):
                tc_att = {}
            # Tree/list payloads often omit ``attachmentCount``; it appears after ``GET testcase/…``.
            # Treating missing as 0 skipped v3 + legacy list for every such case (case-tab files never fetched).
            ac_raw = tc_att.get("attachmentCount")
            ac_num: Optional[int] = None
            if ac_raw is not None:
                try:
                    ac_num = int(ac_raw)
                except (TypeError, ValueError):
                    ac_num = None
            always_list = bool(
                self.config.get("migration.zephyr_attachment_list_always_fetch", False)
            )
            if always_list:
                skip_list = False
            elif ac_num is None:
                skip_list = False
            else:
                skip_list = ac_num <= 0
            extended = bool(
                self.config.get("migration.zephyr_attachment_extended_list_probe", False)
            ) and (ac_num is not None and ac_num > 0)
            try:
                meta = await self.pools.source(
                    self.zephyr.get_attachments_case_merged,
                    tc_id,
                    node_id,
                    extended_probe=extended,
                    skip_list_fetch=skip_list,
                )
            except Exception as e:
                dt = time.monotonic() - t_list
                self.logger.log(
                    f'[{code}][Tests] Zephyr attachment/list failed after {dt:.1f}s for '
                    f'testcase_id={tc_id!r} catalog_node_id={node_id!r}: {e}',
                    "warn",
                )
                return idx, None
            dt = time.monotonic() - t_list
            n_att = len(
                ((meta or {}).get("attachments") or [])
                if isinstance(meta, dict)
                else []
            )
            suffix = ""
            if skip_list:
                suffix = (
                    " (attachment/list skipped: attachmentCount==0 in tree payload; "
                    "inline fileId in text still works)"
                )
            elif extended:
                suffix = " (extended attachment probe)"
            self.logger.log(
                f'[{code}][Tests] testcase_id={tc_id!r} node_id={node_id!r}: Zephyr attachment/list '
                f'done in {dt:.1f}s ({n_att} row(s) in list){suffix}'
            )
            return idx, meta

        pairs = await asyncio.gather(
            *[fetch_one(i, tc, nid) for i, tc, nid in to_fetch],
        )
        for idx, meta in pairs:
            meta_by_idx[idx] = meta
        return meta_by_idx

    _FLEX_FILE_UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    @staticmethod
    def _attachment_rows_from_testcase_detail(detail: Optional[dict]) -> List[dict]:
        """Rows from ``GET testcase/...`` when ``attachment/list`` omits case-tab files (Zephyr build-dependent)."""
        if not isinstance(detail, dict):
            return []
        inner = detail.get("testcase")
        if not isinstance(inner, dict):
            inner = detail
        out: List[dict] = []
        for container in (detail, inner):
            if not isinstance(container, dict):
                continue
            for key in (
                "genericAttachments",
                "attachments",
                "testcaseAttachments",
                "attachmentList",
                "genericAttachmentList",
            ):
                v = container.get(key)
                if not isinstance(v, list):
                    continue
                for row in v:
                    if isinstance(row, dict):
                        out.append(row)
        return out

    @classmethod
    def _flex_row_from_dict_if_attachment_like(cls, d: dict) -> Optional[dict]:
        """True when ``d`` looks like a Zephyr flex attachment row (avoids random UUID fields)."""
        if not isinstance(d, dict):
            return None
        raw_fid = d.get("fileId") or d.get("file_id") or d.get("refId") or d.get("ref_id")
        fid = str(raw_fid).strip() if raw_fid is not None else ""
        if not fid or not cls._FLEX_FILE_UUID_RE.match(fid):
            return None
        markers = (
            "originalFileName",
            "fileName",
            "attachmentName",
            "genericAttachmentId",
            "attachmentId",
            "contentType",
            "mimeType",
            "fileSize",
            "size",
            "tempPath",
        )
        if not any(
            k in d and d.get(k) not in (None, "", [], {})
            for k in markers
        ):
            name = d.get("name") or d.get("title")
            if not (isinstance(name, str) and "." in name and len(name) < 260):
                return None
        return d

    @classmethod
    def _flex_attachment_rows_deep_scan(
        cls, detail: Optional[dict], max_depth: int = 16
    ) -> List[dict]:
        """Walk testcase detail JSON; Zephyr often nests case-tab files where shallow keys miss."""
        if not isinstance(detail, dict):
            return []
        seen_f: set = set()
        out: List[dict] = []

        def walk(obj, depth: int) -> None:
            if depth > max_depth:
                return
            if isinstance(obj, dict):
                hit = cls._flex_row_from_dict_if_attachment_like(obj)
                if hit is not None:
                    fkey = str(
                        hit.get("fileId")
                        or hit.get("file_id")
                        or hit.get("refId")
                        or hit.get("ref_id")
                        or ""
                    ).strip()
                    if fkey and fkey not in seen_f:
                        seen_f.add(fkey)
                        out.append(hit)
                for v in obj.values():
                    walk(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, depth + 1)

        walk(detail, 0)
        return out

    def _zlist_union_detail_attachments(
        self, zlist: List, detail: Optional[dict]
    ) -> List:
        extra: List[dict] = []
        seen_extra: set = set()
        for row in (
            self._attachment_rows_from_testcase_detail(detail)
            + self._flex_attachment_rows_deep_scan(detail)
        ):
            fid = self._zephyr_attachment_download_id(row)
            if fid:
                if fid in seen_extra:
                    continue
                seen_extra.add(fid)
            extra.append(row)
        if not extra:
            return zlist
        out = list(zlist or [])
        seen = {
            self._zephyr_attachment_download_id(x)
            for x in out
            if isinstance(x, dict) and self._zephyr_attachment_download_id(x)
        }
        for row in extra:
            fid = self._zephyr_attachment_download_id(row)
            if fid:
                if fid in seen:
                    continue
                seen.add(fid)
            out.append(row)
        return out

    async def _prepare_cases(self, cases: List, suite_id: int) -> List:
        results = []
        code = self.project["code"]
        n_cases = len(cases)

        # Three independent, read-only prefetch passes. Run them concurrently so
        # they share the source thread pool instead of each waiting for the prior
        # to finish — cuts the per-suite read phase from sum-of-three to
        # max-of-three. Each returns its own per-index list (no shared mutable
        # state), so concurrency is safe; the pool's worker cap bounds load.
        attachment_meta_by_idx, detail_by_idx, teststep_by_idx = await asyncio.gather(
            self._prefetch_zephyr_attachment_lists(code, suite_id, cases),
            self._prefetch_zephyr_testcase_details(code, suite_id, cases),
            self._prefetch_zephyr_teststep_rows(code, suite_id, cases),
        )

        for i, case in enumerate(cases):
            attachment_import: Optional[_ZephyrAttachmentImport] = None
            tc0 = {}
            aid = None
            title = None
            if isinstance(case, dict):
                tc0 = case.get("testcase") or {}
                if not isinstance(tc0, dict):
                    tc0 = {}
                aid = tc0.get("id") or case.get("id")
                title = tc0.get("name")

            if isinstance(case, dict):
                attachment_meta = attachment_meta_by_idx[i]
                zlist = list(((attachment_meta or {}).get("attachments") or []))
                detail_for_att = (
                    detail_by_idx[i] if i < len(detail_by_idx) else None
                )
                zlist = self._zlist_union_detail_attachments(zlist, detail_for_att)
                embedded_ids: List[str] = self._file_ids_from_zephyr_html(
                    self._all_zephyr_rich_html_for_embed_scan(tc0, case)
                )
                work_estimate = set(embedded_ids or [])
                for item in zlist:
                    f = self._zephyr_attachment_download_id(item)
                    if f:
                        work_estimate.add(f)
                n_files = len(work_estimate)
                self.logger.log(
                    f'[{code}][Tests] Suite {suite_id}: case {i + 1}/{n_cases} '
                    f'testcase_id={aid!r} title={title!r} — '
                    f'{n_files} file(s) to transfer (Zephyr download + Qase upload)'
                )
                if n_files == 0:
                    self.logger.log(
                        f'[{code}][Tests] Suite {suite_id}: case {i + 1}/{n_cases} '
                        f'no files to download (list empty, no embedded fileId in description)'
                    )
                    ac = 0
                    try:
                        ac = int(tc0.get("attachmentCount") or 0)
                    except (TypeError, ValueError):
                        ac = 0
                    if ac > 0:
                        self.logger.log(
                            f'[{code}][Tests] Zephyr attachmentCount={ac} for '
                            f'testcase_id={aid!r} but no file id from v3 attachment API, '
                            f'legacy attachment/list, extended probes, or testcase detail JSON. '
                            f'Check Zephyr v3 attachment API and token scope (see ZephyrEnterpriseApiClient).',
                            "warn",
                        )
                try:
                    attachment_import = await self._zephyr_attachments_import(
                        code, zlist, embedded_ids
                    )
                except Exception as e:
                    self.logger.log(
                        f'[{code}][Tests] Attachment upload pipeline failed: {e}',
                        "warn",
                    )
            try:
                case_eff = case
                ts_rows = teststep_by_idx[i] if i < len(teststep_by_idx) else []
                detail_payload = detail_by_idx[i] if i < len(detail_by_idx) else None
                if isinstance(case, dict) and (detail_payload is not None or ts_rows):
                    tc_base = dict(case.get("testcase") or {})
                    if detail_payload is not None:
                        tc_base = self._merge_tc_with_detail_payload(tc_base, detail_payload)
                    if ts_rows:
                        # Win over any legacy step array — the dedicated step
                        # endpoint is authoritative for this Zephyr build.
                        tc_base["_zephyr_teststep_rows"] = list(ts_rows)
                    case_eff = {
                        **case,
                        "testcase": tc_base,
                        "_zephyr_detail_prefetched": detail_payload is not None,
                    }
                results.append(
                    self._prepare_case(case_eff, suite_id, attachment_import=attachment_import)
                )
            except Exception as e:
                title = None
                if isinstance(case, dict):
                    tc = case.get("testcase")
                    if isinstance(tc, dict):
                        title = tc.get("name")
                keys = list(case.keys()) if isinstance(case, dict) else None
                self.logger.log(
                    f'[{code}][Tests] _prepare_case failed: suite_id={suite_id} index={i}/{len(cases)} '
                    f'title={title!r} case_top_keys={keys}: {e}',
                    "error",
                )
                raise
        return results

    def _all_zephyr_rich_html_for_embed_scan(self, tc: dict, case: dict) -> str:
        """Concatenate rich HTML from description, pre/post, and manual steps (for embedded ``fileId`` scan)."""
        parts: List[str] = []
        for src in (tc, case):
            if not isinstance(src, dict):
                continue
            for key_group in (self._DESC_KEYS, self._PRECOND_KEYS, self._POSTCOND_KEYS):
                for k in key_group:
                    v = src.get(k)
                    if isinstance(v, str) and v.strip():
                        parts.append(v)
        if isinstance(tc, dict):
            for key in self._ZEPHYR_STEP_LIST_KEYS:
                v = tc.get(key)
                if not isinstance(v, list):
                    continue
                for row in v:
                    if not isinstance(row, dict):
                        continue
                    for k in (
                        "step",
                        "description",
                        "plainStepText",
                        "plainText",
                        "text",
                        "action",
                        "stepText",
                        "stepDetail",
                        "notes",
                        "expectedResult",
                        "expectedResults",
                        "expected",
                        "result",
                        "expectedOutcome",
                        "data",
                        "testData",
                        "inputData",
                        "stepData",
                    ):
                        x = row.get(k)
                        if isinstance(x, str) and x.strip():
                            parts.append(x)
                break
        return html.unescape("\n".join(parts))

    def _file_ids_from_zephyr_html(self, raw_html: str) -> List[str]:
        if not raw_html or not isinstance(raw_html, str):
            return []
        h = html.unescape(raw_html)
        found = re.findall(r"(?:fileId|fileid)=([A-Za-z0-9_.-]+)", h)
        return list(dict.fromkeys(found))

    @staticmethod
    def _zephyr_attachment_suggested_name(item: dict) -> str:
        if not isinstance(item, dict):
            return "zephyr-attachment.bin"
        for k in ("originalFileName", "fileName", "name", "title", "attachmentName"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return "zephyr-attachment.bin"

    @staticmethod
    def _zephyr_attachment_flex_file_id(item: dict) -> Optional[str]:
        """Id usable with Zephyr ``/flex/download?fileId=…`` (matches repository heuristics)."""
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

    @staticmethod
    def _zephyr_attachment_download_id(item: dict) -> Optional[str]:
        """Id to pass to ``/flex/download``: prefer UUID-style keys, else numeric row ids (many Zephyr builds)."""
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
        for k in ("genericAttachmentId", "attachmentId", "id", "dataId", "data_id"):
            v = item.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        return None

    def _zephyr_attachment_row_is_field_level(self, row: dict) -> bool:
        """True when Zephyr marks the row as a custom-field / rich-text column attachment.

        By default only **explicit** ids (``customFieldId``, ``fieldId``, ``cfId``) count.
        Enable ``migration.zephyr_attachment_field_level_type_heuristic`` for extra type-string
        matching — avoid substrings like bare ``description``, which often appear on **case-level**
        rows in Zephyr and previously dropped all case attachments from Qase.
        """
        if not isinstance(row, dict):
            return False
        if row.get("customFieldId") or row.get("fieldId") or row.get("cfId"):
            return True
        if not bool(
            self.config.get("migration.zephyr_attachment_field_level_type_heuristic", False)
        ):
            return False
        t = str(
            row.get("genericAttachmentType")
            or row.get("attachmentType")
            or row.get("type")
            or ""
        )
        if re.search(
            r"\b(richtext|rich[\s_-]*text|customfield|custom[\s_-]*field)\b",
            t,
            re.I,
        ):
            return True
        return False

    def _zephyr_attachment_should_include_as_case_level(self, item: dict, fid: str) -> bool:
        """True if this list row should add a Qase case attachment (not a custom-field / rich-text row).

        Default **true** for ``migration.zephyr_case_attachment_list_all_as_case_level``: many Zephyr builds
        set ``customFieldId`` / ``fieldId`` on normal case-tab files; excluding those rows dropped all
        case-level hashes. Set the flag **false** to
        restore strict field-level exclusion.
        """
        if not fid:
            return False
        if bool(
            self.config.get(
                "migration.zephyr_case_attachment_list_all_as_case_level",
                True,
            )
        ):
            return True
        if self._zephyr_attachment_row_is_field_level(item):
            return False
        return True

    def _zephyr_case_level_list_row_for_fid(self, zlist: List, fid: str) -> bool:
        for row in zlist or []:
            if not isinstance(row, dict):
                continue
            if Cases._zephyr_attachment_download_id(row) != fid:
                continue
            if self._zephyr_attachment_row_is_field_level(row):
                continue
            return True
        return False

    def _zephyr_field_level_list_row_for_fid(self, zlist: List, fid: str) -> bool:
        for row in zlist or []:
            if not isinstance(row, dict):
                continue
            if Cases._zephyr_attachment_download_id(row) != fid:
                continue
            if self._zephyr_attachment_row_is_field_level(row):
                return True
        return False

    def _extract_rich_field(
        self,
        tc: dict,
        case: dict,
        keys: tuple,
        fid_to_url: Dict[str, str],
        fid_to_hash: Dict[str, str],
        fid_to_label: Dict[str, str],
        inline_hash_collector: List[str],
    ) -> tuple:
        m = fid_to_url or {}
        hm = fid_to_hash or {}
        lm = fid_to_label or {}
        for src, label in ((tc, "testcase"), (case, "node")):
            if not isinstance(src, dict):
                continue
            for k in keys:
                t = self._coerce_text_value(src.get(k))
                if t:
                    resolved, hs = replace_zephyr_flex_urls_in_text(
                        t, m, hm, lm
                    )
                    inline_hash_collector.extend(hs)
                    return self._maybe_strip_html(resolved), f"{label}.{k}"
        return "", ""

    @staticmethod
    def _safe_upload_filename(name: str) -> str:
        n = (name or "file.bin").replace("\\", "_").replace("/", "_").strip() or "file.bin"
        return n[:200]

    async def _zephyr_attachments_import(
        self,
        project_code: str,
        zephyr_items: List,
        embedded_file_ids: List[str],
    ) -> _ZephyrAttachmentImport:
        """Upload each distinct file once; case hashes from list rows + embedded-only when allowed."""
        code = self.project["code"]
        max_bytes = _MAX_ATTACHMENT_UPLOAD_BYTES
        embedded_set = set(embedded_file_ids or [])
        zlist = list(zephyr_items or [])
        supplement_embed = bool(
            self.config.get("migration.zephyr_supplement_case_attachments_from_embedded", True)
        )

        work_map: Dict[str, str] = {}
        for fid in embedded_set:
            if fid:
                work_map.setdefault(fid, f"zephyr-{fid[:12]}.bin")
        for item in zlist:
            fid = self._zephyr_attachment_download_id(item)
            if fid:
                work_map.setdefault(fid, self._zephyr_attachment_suggested_name(item))

        work_all = list(work_map.items())
        total = len(work_all)
        if total == 0:
            return _ZephyrAttachmentImport([], [], {}, {}, {})

        uploaded: Dict[str, dict] = {}

        # Reuse files already uploaded earlier this run (keyed by Zephyr fileId).
        # The same fileId recurs across cases/placements; without this, each
        # recurrence paid a full Zephyr download + a throttled Qase upload of
        # identical bytes. Seeding ``uploaded`` from the cache lets the case-level
        # linking below still pick the file up (upload once, link many).
        work: List[tuple] = []
        for fid, default_name in work_all:
            cached = self.mappings.attachments_map.get(str(fid))
            if cached and cached.get("hash"):
                uploaded[fid] = {
                    "hash": cached["hash"],
                    "url": cached.get("url") or "",
                    "filename": cached.get("filename") or default_name,
                }
            else:
                work.append((fid, default_name))
        n_fetch = len(work)
        if total - n_fetch:
            self.logger.log(
                f'[{code}][Tests] Attachments: reusing {total - n_fetch}/{total} '
                f'already-uploaded file(s) this run (skip re-download/upload)'
            )

        async def download_one(idx: int, fid: str, default_name: str):
            self.logger.log(
                f'[{code}][Tests] Attachment {idx}/{n_fetch} fileId={fid!r} (Zephyr download)',
            )
            try:
                content, remote_name = await self.pools.source(
                    self.zephyr.download_attachment_by_file_id, fid
                )
                return (fid, default_name, content, remote_name, None)
            except Exception as e:
                self.logger.log(
                    f'[{code}][Tests] Zephyr download failed fileId={fid!r}: {e}',
                    "warn",
                )
                return (fid, default_name, None, None, e)

        downloaded = await asyncio.gather(
            *[
                download_one(idx, fid, default_name)
                for idx, (fid, default_name) in enumerate(work, start=1)
            ]
        )

        for fid, default_name, content, remote_name, err in downloaded:
            if err is not None or content is None:
                continue
            if len(content) > max_bytes:
                self.logger.log(
                    f'[{code}][Tests] Skip attachment fileId={fid!r}: {len(content)} bytes > max {max_bytes}',
                    "warn",
                )
                continue
            fname = self._safe_upload_filename(remote_name or default_name)
            res = None
            try:
                self.logger.log(
                    f'[{code}][Tests] Qase upload {fname!r} (fileId={fid!r})',
                )
                res = await self.pools.qs(
                    self.qase.upload_attachment,
                    project_code,
                    (fname, content),
                )
            except Exception as e:
                self.logger.log(
                    f'[{code}][Tests] Qase upload failed for {fname!r}: {e}',
                    "warn",
                )
            if res is None or not res:
                if res is not None:
                    self.logger.log(
                        f'[{code}][Tests] Qase upload returned empty for {fname!r}',
                        "warn",
                    )
                continue
            h = res.get("hash")
            if not h:
                self.logger.log(
                    f'[{code}][Tests] Qase upload response missing hash: {res!r}'[:500],
                    "warn",
                )
                continue
            u = res.get("url") or res.get("link") or ""
            uploaded[fid] = {"hash": h, "url": u, "filename": fname}
            # Xray-style global registry (filled at load); keyed by Zephyr flex file id
            self.mappings.attachments_map[str(fid)] = {
                "filename": fname,
                "hash": h,
                "url": u or "",
            }

        file_id_to_hash = {
            k: v["hash"] for k, v in uploaded.items() if v.get("hash")
        }
        file_id_to_url = {
            k: v["url"]
            for k, v in uploaded.items()
            if isinstance(v.get("url"), str) and v["url"]
        }
        file_id_to_label = {
            k: os.path.basename((v.get("filename") or "attachment").strip() or "attachment")
            for k, v in uploaded.items()
        }

        case_hashes: List[str] = []
        case_fids: List[str] = []
        seen_h: set = set()
        seen_f: set = set()
        for item in zlist:
            fid = self._zephyr_attachment_download_id(item)
            if not fid or fid not in uploaded:
                continue
            if not self._zephyr_attachment_should_include_as_case_level(item, fid):
                continue
            h = uploaded[fid].get("hash")
            if h and h not in seen_h:
                seen_h.add(h)
                case_hashes.append(h)
            if fid not in seen_f:
                seen_f.add(fid)
                case_fids.append(fid)

        # Embedded-only (or attachment/list empty): still attach to the case unless a field-level list row
        # claims this file — mirrors “no duplicate case tab” without dropping uploads entirely.
        if supplement_embed:
            for fid in embedded_set:
                if not fid or fid not in uploaded:
                    continue
                h = uploaded[fid].get("hash")
                if not h or h in seen_h:
                    continue
                if self._zephyr_case_level_list_row_for_fid(zlist, fid):
                    continue
                if self._zephyr_field_level_list_row_for_fid(zlist, fid):
                    continue
                seen_h.add(h)
                case_hashes.append(h)
                if fid not in seen_f:
                    seen_f.add(fid)
                    case_fids.append(fid)

        return _ZephyrAttachmentImport(
            case_hashes,
            case_fids,
            file_id_to_url,
            file_id_to_hash,
            file_id_to_label,
        )

    def _zephyr_ms_to_dt_str(self, ms: int) -> str:
        return str(datetime.fromtimestamp(round(ms / 1000)))

    _DESC_KEYS = (
        "description",
        "detail",
        "details",
        "objective",
        "summary",
        "notes",
        "testObjective",
        "testObjectiveRichText",
        "detailedDescription",
        "richDescription",
        "plainDescription",
        "comment",
        "comments",
    )
    _PRECOND_KEYS = (
        "preCondition",
        "preConditions",
        "precondition",
        "pre_conditions",
        "prerequisite",
        "preRequisite",
        "preText",
        "preRequisites",
    )
    _POSTCOND_KEYS = (
        "postCondition",
        "postConditions",
        "postcondition",
        "post_conditions",
    )
    # ``_zephyr_teststep_rows`` is injected from ``GET testcase/{id}/teststep``
    # (see :meth:`_prefetch_zephyr_teststep_rows`). Keep it first so any legacy
    # step list that might exist in tree/detail is only consulted when the
    # dedicated step endpoint has nothing to offer.
    _ZEPHYR_STEP_LIST_KEYS = (
        "_zephyr_teststep_rows",
        "manualTestSteps",
        "manualTeststeps",
        "testSteps",
        "testCaseSteps",
        "testcaseSteps",
        "testcaseTestSteps",
        "steps",
        "detailTestSteps",
        "testScriptSteps",
        "stepCollection",
    )

    def _safe_json_snippet(self, obj, max_len: int = 4000) -> str:
        try:
            s = json.dumps(obj, default=str, ensure_ascii=False, indent=2)
        except TypeError:
            s = repr(obj)
        if len(s) > max_len:
            return s[: max_len - 20] + "\n... [truncated]"
        return s

    def _coerce_text_value(self, v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            for k in ("text", "html", "value", "content", "data", "plainText"):
                x = v.get(k)
                if isinstance(x, str) and x.strip():
                    return x.strip()
            return ""
        if isinstance(v, list):
            parts = [self._coerce_text_value(x) for x in v]
            return "\n".join(p for p in parts if p)
        return str(v).strip()

    def _maybe_strip_html(self, text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        plain = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", plain).strip()

    def _extract_description(self, tc: dict, case: dict) -> tuple:
        for src, label in ((tc, "testcase"), (case, "node")):
            if not isinstance(src, dict):
                continue
            for k in self._DESC_KEYS:
                t = self._coerce_text_value(src.get(k))
                if t:
                    return self._maybe_strip_html(t), f"{label}.{k}"
        return "", ""

    def _extract_preconditions(self, tc: dict, case: dict) -> tuple:
        for src, label in ((tc, "testcase"), (case, "node")):
            if not isinstance(src, dict):
                continue
            for k in self._PRECOND_KEYS:
                t = self._coerce_text_value(src.get(k))
                if t:
                    return self._maybe_strip_html(t), f"{label}.{k}"
        return "", ""

    def _extract_postconditions(self, tc: dict, case: dict) -> tuple:
        for src, label in ((tc, "testcase"), (case, "node")):
            if not isinstance(src, dict):
                continue
            for k in self._POSTCOND_KEYS:
                t = self._coerce_text_value(src.get(k))
                if t:
                    return self._maybe_strip_html(t), f"{label}.{k}"
        return "", ""

    def _zephyr_step_action_expected(self, row: dict) -> tuple:
        """Return ``(action, expected, data)`` from a Zephyr manual-step row.

        ``data`` matches the Zephyr UI "Test Data" column (``step.data`` on the
        ``/testcase/{id}/teststep`` endpoint) and is passed through to Qase's
        ``TestStepCreate.data`` so step inputs stop getting silently dropped.
        Older call sites that only unpack two values still work with Python's
        star-unpacking (``a, e = ...``) because tuples of length 3 destructure
        to two names with a trailing ``ValueError``; callers here always use
        three-name unpacking. Legacy fallback keys are preserved.
        """
        if not isinstance(row, dict):
            return "", "", ""
        action = self._coerce_text_value(
            row.get("step")
            or row.get("description")
            or row.get("plainStepText")
            or row.get("plainText")
            or row.get("text")
            or row.get("action")
            or row.get("stepText")
            or row.get("stepDetail")
            or row.get("notes")
        )
        expected = self._coerce_text_value(
            row.get("expectedResult")
            or row.get("expectedResults")
            or row.get("expected")
            or row.get("result")
            or row.get("expectedOutcome")
        )
        data = self._coerce_text_value(
            row.get("data")
            or row.get("testData")
            or row.get("inputData")
            or row.get("stepData")
        )
        return action, expected, data

    def _extract_zephyr_steps(
        self,
        tc: dict,
        case: dict,
        fid_to_url: Optional[Dict[str, str]] = None,
        fid_to_hash: Optional[Dict[str, str]] = None,
        fid_to_label: Optional[Dict[str, str]] = None,
        inline_hash_collector: Optional[List[str]] = None,
    ) -> List[TestStepCreate]:
        rows_raw: List = []
        for key in self._ZEPHYR_STEP_LIST_KEYS:
            v = tc.get(key)
            if isinstance(v, list) and len(v) > 0:
                rows_raw = v
                break
        steps: List[TestStepCreate] = []
        pos = 1
        m = fid_to_url or {}
        hm = fid_to_hash or {}
        lm = fid_to_label or {}
        for row in rows_raw:
            action, expected, data = self._zephyr_step_action_expected(row)
            if m or hm or lm:
                action, ha = replace_zephyr_flex_urls_in_text(action, m, hm, lm)
                expected, he = replace_zephyr_flex_urls_in_text(expected, m, hm, lm)
                data, hd = replace_zephyr_flex_urls_in_text(data, m, hm, lm)
                if inline_hash_collector is not None:
                    inline_hash_collector.extend(ha)
                    inline_hash_collector.extend(he)
                    inline_hash_collector.extend(hd)
            action = self._maybe_strip_html(action)
            expected = self._maybe_strip_html(expected)
            data = self._maybe_strip_html(data)
            if action or expected or data:
                if not action:
                    action = "—"
                step_kwargs = {
                    "action": action,
                    "expected_result": expected or "",
                    "position": pos,
                }
                if data:
                    step_kwargs["data"] = data
                steps.append(TestStepCreate(**step_kwargs))
                pos += 1
        if steps:
            return steps
        if not bool(self.config.get("migration.zephyr_external_id_as_expected_steps", True)):
            return steps
        ext = tc.get("externalId")
        if ext is None and isinstance(case, dict):
            ext = case.get("externalId")
        if not isinstance(ext, str) or not ext.strip():
            return steps
        raw_parts = [p.strip() for p in ext.split(";") if p.strip()]
        if not raw_parts:
            return steps
        # Previously we skipped single-value externalId when "semicolon only" — that left many cases with no steps.
        semicolon_only = bool(self.config.get("migration.zephyr_external_id_only_when_semicolon", False))
        if semicolon_only and len(raw_parts) < 2:
            return steps
        for p in raw_parts:
            p, hs = replace_zephyr_flex_urls_in_text(p, m, hm, lm)
            if inline_hash_collector is not None:
                inline_hash_collector.extend(hs)
            p = self._maybe_strip_html(p)
            steps.append(
                TestStepCreate(
                    action=f"Step {len(steps) + 1}",
                    expected_result=p,
                    position=len(steps) + 1,
                )
            )
        return steps

    def _zephyr_priority_for_qase(self, tc: dict) -> Optional[int]:
        raw = tc.get("priority")
        if raw is None or raw == "":
            return None
        id_map = self.config.get("cases.priority_map") or {}
        if isinstance(id_map, dict) and id_map:
            zkey = None
            if isinstance(raw, int):
                zkey = str(raw)
            elif isinstance(raw, str) and raw.strip().isdigit():
                zkey = raw.strip()
            if zkey is not None:
                mapped = id_map.get(zkey)
                if mapped is None:
                    for mk, mv in id_map.items():
                        if str(mk).strip() == zkey:
                            mapped = mv
                            break
                if mapped is not None:
                    try:
                        return int(mapped)
                    except (TypeError, ValueError):
                        pass
        if isinstance(raw, int):
            m = self.mappings.qase_priority_keys_to_id
            if raw > 0 and str(raw) in m:
                return m[str(raw)]
            return raw if raw > 0 else None
        s = str(raw).strip().lower()
        if not s:
            return None
        m = self.mappings.qase_priority_keys_to_id
        if s in m:
            return m[s]
        aliases = self.config.get("cases.priority_map") or {}
        if isinstance(aliases, dict):
            v = aliases.get(raw) or aliases.get(s) or aliases.get(str(raw).strip())
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return None

    def _zephyr_status_for_qase(self, tc: dict, case: dict) -> Optional[int]:
        m = self.mappings.qase_case_status_keys_to_id
        for src in (tc, case):
            if not isinstance(src, dict):
                continue
            for key in ("status", "statusName", "workflowStatus", "approvalStatus", "testcaseStatus"):
                v = src.get(key)
                if isinstance(v, str) and v.strip():
                    s = v.strip().lower()
                    if s in m:
                        return m[s]
        if not isinstance(case, dict):
            return None
        sf = case.get("stateFlag")
        if sf is None:
            return None
        sm = self.config.get("cases.status_map") or {}
        if not sm:
            sm = {"0": "actual", "1": "draft"}
        try:
            sf_key = str(int(sf))
        except (TypeError, ValueError):
            sf_key = str(sf)
        slug = sm.get(sf_key) or sm.get(str(sf))
        if isinstance(slug, str) and slug.strip():
            key = slug.strip().lower()
            return m.get(key)
        return None

    def _zephyr_tags_for_qase(self, tc: dict, case: dict) -> List[str]:
        def _tag(s: str) -> str:
            return html.unescape(s.strip())

        out: List[str] = []
        for src in (tc, case):
            if not isinstance(src, dict):
                continue
            tag_s = src.get("tag")
            if isinstance(tag_s, str) and tag_s.strip():
                for piece in re.split(r"\s+", tag_s.strip()):
                    if piece:
                        out.append(_tag(piece))
            for key in ("tags", "labels", "labelNames"):
                v = src.get(key)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            out.append(_tag(item))
                        elif isinstance(item, dict):
                            n = item.get("name") or item.get("label") or item.get("title")
                            if isinstance(n, str) and n.strip():
                                out.append(_tag(n))
                elif isinstance(v, str) and v.strip():
                    for piece in re.split(r"[,;]", v):
                        if piece.strip():
                            out.append(_tag(piece))
        if bool(self.config.get("migration.zephyr_requirement_ids_as_tags", False)):
            for src in (tc, case):
                if not isinstance(src, dict):
                    continue
                for rid in (src.get("requirementIds") or src.get("requirementIdsNew") or []):
                    if rid is not None and str(rid).strip():
                        out.append(f"req:{rid}")
        return list(dict.fromkeys(out))

    def _zephyr_author_user_id(self, tc: dict) -> int:
        for k in ("creatorId", "writerId", "lastUpdaterId"):
            v = tc.get(k)
            if isinstance(v, int) and v > 0:
                return self.mappings.get_user_id(v)
        v = tc.get("creatorId")
        if isinstance(v, int):
            return self.mappings.get_user_id(v)
        return self.mappings.get_user_id(0)

    def _maybe_log_zephyr_case_discovery(self, case, suite_id: int) -> None:
        if self._zephyr_discovery_logs_left <= 0:
            return
        if not isinstance(case, dict):
            return
        self._zephyr_discovery_logs_left -= 1
        code = self.project["code"]
        tc = case.get("testcase") if isinstance(case.get("testcase"), dict) else {}
        title = tc.get("name", "?")
        self.logger.log(
            f'[{code}][ZephyrDiscovery] sample suite_id={suite_id} title={title!r} — '
            f'node keys={list(case.keys())} | testcase keys={list(tc.keys())}'
        )
        self.logger.log(f'[{code}][ZephyrDiscovery] case JSON (truncated):\n{self._safe_json_snippet(case, 4000)}')

        if self.config.get("migration.log_fetch_testcase_detail_with_samples"):
            vid = tc.get("testcaseVersionId") or tc.get("id") or case.get("id")
            if vid is not None:
                detail = self.zephyr.get_testcase_detail(int(vid))
                self.logger.log(
                    f'[{code}][ZephyrDiscovery] GET testcase/{vid!r} response (truncated):\n'
                    f'{self._safe_json_snippet(detail, 4000) if detail else "null / request failed"}'
                )

        if self.config.get("migration.log_probe_attachment_api_on_samples", False):
            aid = tc.get("id") or case.get("id")
            if aid is not None:
                try:
                    att = self.zephyr.get_attachments_case(int(aid))
                except Exception as e:
                    self.logger.log(
                        f'[{code}][ZephyrDiscovery] attachment probe failed for itemId={aid!r}: {e}',
                        "warn",
                    )
                else:
                    uri = att.get("_zephyr_attachment_uri")
                    items = att.get("attachments") or []
                    self.logger.log(
                        f'[{code}][ZephyrDiscovery] attachment list probe itemId={aid!r} uri={uri!r} count={len(items)}'
                    )
                    if items and isinstance(items[0], dict):
                        self.logger.log(
                            f'[{code}][ZephyrDiscovery] first attachment keys={list(items[0].keys())}'
                        )

    def _merge_tc_from_zephyr_detail(self, tc: dict, case: Optional[dict] = None) -> dict:
        case = case or {}
        out = dict(tc)
        if case.get("_zephyr_detail_prefetched"):
            return out
        if not self.config.get("migration.enrich_case_from_zephyr_detail"):
            return out
        vid = out.get("testcaseVersionId") or out.get("id")
        if vid is None:
            return out
        detail = self.zephyr.get_testcase_detail(int(vid))
        if not isinstance(detail, dict):
            return out
        return self._merge_tc_with_detail_payload(out, detail)

    def _prepare_case(
        self,
        case,
        suite_id,
        attachment_import: Optional[_ZephyrAttachmentImport] = None,
    ):
        self._maybe_log_zephyr_case_discovery(case, suite_id)

        tc = self._merge_tc_from_zephyr_detail(dict(case["testcase"]), case)
        created_ms = int(tc["createDatetime"])
        modified_ms = int(tc.get("lastModifiedOn", created_ms))
        # Qase validates updated_at >= created_at; Zephyr can report lastModified < create (import / clock skew).
        if modified_ms < created_ms:
            modified_ms = created_ms

        fid_to_url: Dict[str, str] = (
            attachment_import.file_id_to_url if attachment_import else {}
        ) or {}
        fid_to_hash: Dict[str, str] = (
            attachment_import.file_id_to_hash if attachment_import else {}
        ) or {}
        fid_to_label: Dict[str, str] = (
            attachment_import.file_id_to_label if attachment_import else {}
        ) or {}
        inline_hashes: List[str] = []
        description, _desc_src = self._extract_rich_field(
            tc,
            case,
            self._DESC_KEYS,
            fid_to_url,
            fid_to_hash,
            fid_to_label,
            inline_hashes,
        )
        preconditions, _pre_src = self._extract_rich_field(
            tc,
            case,
            self._PRECOND_KEYS,
            fid_to_url,
            fid_to_hash,
            fid_to_label,
            inline_hashes,
        )
        postconditions, _post_src = self._extract_rich_field(
            tc,
            case,
            self._POSTCOND_KEYS,
            fid_to_url,
            fid_to_hash,
            fid_to_label,
            inline_hashes,
        )
        steps = self._extract_zephyr_steps(
            tc,
            case,
            fid_to_url=fid_to_url,
            fid_to_hash=fid_to_hash,
            fid_to_label=fid_to_label,
            inline_hash_collector=inline_hashes,
        )

        case_hashes = (
            list(attachment_import.case_level_hashes) if attachment_import else []
        )
        if attachment_import and self.config.get(
            "migration.zephyr_merge_inline_attachment_hashes_into_case", False
        ):
            case_hashes = merge_zephyr_case_attachment_hashes(
                case_hashes, inline_hashes
            )
        # Do not list the same file on the case tab if it is already inlined in text.
        if bool(
            self.config.get("migration.zephyr_skip_case_attachment_if_inline", True)
        ):
            inline_set = {h for h in inline_hashes if h}
            if inline_set:
                case_hashes = [h for h in case_hashes if h not in inline_set]
        data = {
            "title": tc["name"],
            "created_at": self._zephyr_ms_to_dt_str(created_ms),
            "updated_at": self._zephyr_ms_to_dt_str(modified_ms),
            "author_id": self._zephyr_author_user_id(tc),
            "steps": steps,
            "attachments": [str(h) for h in case_hashes if h],
            "is_flaky": 0,
            "custom_field": {},
            "suite_id": self._get_suite_id(suite_id),
        }
        if description:
            data["description"] = description
        if preconditions:
            data["preconditions"] = preconditions
        if postconditions:
            data["postconditions"] = postconditions

        pr = self._zephyr_priority_for_qase(tc)
        if pr is not None:
            data["priority"] = pr
        st = self._zephyr_status_for_qase(tc, case if isinstance(case, dict) else {})
        if st is not None:
            data["status"] = st
        tags = self._zephyr_tags_for_qase(tc, case if isinstance(case, dict) else {})
        if tags:
            data["tags"] = tags

        if bool(self.config.get("migration.zephyr_import_custom_field_values", True)):
            data = self._import_custom_fields_for_case(
                case=case if isinstance(case, dict) else {},
                tc=tc if isinstance(tc, dict) else {},
                data=data,
                attachment_import=attachment_import,
            )

        # data = self._set_type(case=case, data=data)
        # data = self._set_refs(case=case, data=data)
        # data = self._set_milestone(case=case, data=data, code=self.project['code'])

        if bool(self.config.get("cases.preserve_ids", True)):
            try:
                ztid = int(tc["id"])
                rid = Cases._zephyr_to_qase_case_id_for_bulk(ztid)
                data["id"] = rid
                self.mappings.register_zephyr_testcase_qase_case_id(
                    self.project["code"], ztid, rid
                )
            except (TypeError, ValueError) as e:
                self.logger.log(
                    f'[{self.project["code"]}][Tests] could not set bulk case ``id`` '
                    f'(zephyr_qase_case_id_in_request): {e}',
                    "warn",
                )

        return TestCasebulkCasesInner(**data)
    # Done
    def _set_refs(self, case:dict, data: dict):
        if self.mappings.refs_id and case['refs'] and self.config.get('tests.refs.enable'):
            string = str(case['refs'])
            url = str(self.config.get('refs.url'))
            if string.startswith('http'):
                data['custom_field'][str(self.mappings.refs_id)] = quote(string, safe="/:")
            elif url != '':
                if not url.endswith('/'):
                    string = string + '/'
                string = url + string
                data['custom_field'][str(self.mappings.refs_id)] = quote(string, safe="/:")
        return data
    
    async def _get_attachments_for_case(self, case: dict, data: dict) -> dict:
        self.logger.log(f'[{self.project["code"]}][Tests] Getting attachments for case {case["title"]}')
        try:
            attachments = await self.pools.source(self.zephyr.get_attachments_case, case['id'])
        except Exception as e:
            self.logger.log(f'[{self.project["code"]}][Tests] Failed to get attachments for case {case["title"]}: {e}', 'error')
            return data
        if not attachments or "attachments" not in attachments:
            return data
        self.logger.log(f'[{self.project["code"]}][Tests] Found {len(attachments["attachments"])} attachments for case {case["title"]}')
        for attachment in attachments['attachments']:
            try:
                id = attachment['id']
                if 'data_id' in attachment:
                    id = attachment['data_id']
                if id in self.mappings.attachments_map:
                    data['attachments'].append(self.mappings.attachments_map[id]['hash'])
            except Exception as e:
                self.logger.log(f'[{self.project["code"]}][Tests] Failed to get attachment for case {case["title"]}: {e}', 'error')
        return data
    
    def _text_for_qase_custom_field(
        self,
        text: str,
        attachment_import: Optional[_ZephyrAttachmentImport],
    ) -> str:
        if not text:
            return ""
        if attachment_import is not None:
            resolved, _hs = replace_zephyr_flex_urls_in_text(
                text,
                attachment_import.file_id_to_url,
                attachment_import.file_id_to_hash,
                attachment_import.file_id_to_label,
            )
            return self._maybe_strip_html(resolved)
        return self._maybe_strip_html(text)

    def _zephyr_custom_field_from_value_row(self, item: dict) -> Optional[dict]:
        fid = item.get("fieldId")
        if fid is not None:
            try:
                f = self.mappings.custom_fields_by_zephyr_id.get(int(fid))
                if f:
                    return f
            except (TypeError, ValueError):
                pass
        for key in (item.get("fieldName"), item.get("displayName")):
            if key and key in self.mappings.custom_fields:
                return self.mappings.custom_fields[key]
        return None

    @staticmethod
    def _raw_value_from_zephyr_cf_row(item: dict) -> object:
        for k in ("values", "selectedValues", "multiValues"):
            v = item.get(k)
            if isinstance(v, list) and len(v) > 0:
                return v
            if v is not None and v != "":
                return v
        v = item.get("value")
        if v is not None and v != "":
            return v
        tv = item.get("textValue")
        if tv is not None and tv != "":
            return tv
        return None

    def _apply_zephyr_custom_field_value(
        self,
        custom_field: dict,
        raw: object,
        data: dict,
        attachment_import: Optional[_ZephyrAttachmentImport],
    ) -> None:
        qid = custom_field.get("qase_id")
        if qid is None:
            return
        qkey = str(qid)
        tid = custom_field.get("type_id")
        if tid in (6, 12):
            coerced = raw
            if tid == 12 and isinstance(raw, str) and "," in raw:
                coerced = [x.strip() for x in raw.split(",") if x.strip()]
            val = self._validate_custom_field_values(custom_field, coerced)
            if val is None:
                return
            try:
                if isinstance(val, list):
                    data["custom_field"][qkey] = ",".join(str(int(v) + 1) for v in val)
                else:
                    data["custom_field"][qkey] = str(int(val) + 1)
            except (TypeError, ValueError) as e:
                self.logger.log(
                    f'[{self.project["code"]}][Tests] Custom field '
                    f'{custom_field.get("name")!r} could not map select value {raw!r}: {e}',
                    "warn",
                )
            return
        if raw is None:
            return
        st = self._text_for_qase_custom_field(str(raw), attachment_import)
        if st:
            data["custom_field"][qkey] = st

    def _import_custom_fields_for_case(
        self,
        case: dict,
        tc: dict,
        data: dict,
        attachment_import: Optional[_ZephyrAttachmentImport] = None,
    ) -> dict:
        """Map Zephyr testcase custom fields (``customFieldValues``, ``customProperties``) onto Qase bulk row."""
        filled: set = set()

        for item in tc.get("customFieldValues") or []:
            if not isinstance(item, dict):
                continue
            cf = self._zephyr_custom_field_from_value_row(item)
            if not cf:
                continue
            qid = cf.get("qase_id")
            if qid is None:
                continue
            qkey = str(qid)
            raw = self._raw_value_from_zephyr_cf_row(item)
            if raw is None:
                continue
            self._apply_zephyr_custom_field_value(
                cf, raw, data, attachment_import
            )
            if qkey in data.get("custom_field", {}):
                filled.add(qkey)

        for src in (tc, case):
            if not isinstance(src, dict):
                continue
            props = src.get("customProperties")
            if not isinstance(props, dict):
                continue
            for fname, pval in props.items():
                if not fname or pval is None or pval == "":
                    continue
                cf = self.mappings.custom_fields.get(fname)
                if not cf or not cf.get("qase_id"):
                    continue
                qkey = str(cf["qase_id"])
                if qkey in filled or qkey in data.get("custom_field", {}):
                    continue
                self._apply_zephyr_custom_field_value(
                    cf, pval, data, attachment_import
                )
                if qkey in data.get("custom_field", {}):
                    filled.add(qkey)

        for field_name in case:
            if not field_name.startswith("custom_"):
                continue
            suffix = field_name[len("custom_") :]
            if suffix not in self.mappings.custom_fields or not case[field_name]:
                continue
            custom_field = self.mappings.custom_fields[suffix]
            qid = custom_field.get("qase_id")
            if qid is None:
                continue
            qkey = str(qid)
            if qkey in filled or qkey in data.get("custom_field", {}):
                continue
            self._apply_zephyr_custom_field_value(
                custom_field, case[field_name], data, attachment_import
            )
            if qkey in data.get("custom_field", {}):
                filled.add(qkey)

        if self.mappings.step_fields:
            for field_name in case:
                if not field_name.startswith("custom_"):
                    continue
                suffix = field_name[len("custom_") :]
                if suffix not in self.mappings.step_fields or not case[field_name]:
                    continue
                steps = []
                i = 1
                for step in case[field_name]:
                    action = self._text_for_qase_custom_field(
                        str(step.get("content", "")), attachment_import
                    )
                    expected = self._text_for_qase_custom_field(
                        str(step.get("expected", "")), attachment_import
                    )
                    action = action.strip()
                    expected = expected.strip()
                    if action != "" or (action == "" and expected != ""):
                        if action == "" or action == " ":
                            action = "No action"
                        steps.append(
                            TestStepCreate(
                                action=action,
                                expected_result=expected,
                                position=i,
                            )
                        )
                        i += 1
                    else:
                        self.logger.log(
                            f'[{self.project["code"]}][Tests] Case has invalid step {step}',
                            "warn",
                        )
                data["steps"] = steps
        return data
    
    # Done. Method validates if custom field value exists (skip)
    def _validate_custom_field_values(self, custom_field: dict, value: Union[str, List]) -> Optional[Union[str, list]]: 
        if len(custom_field['configs']) > 0 and 'options' in custom_field['configs'][0] and 'items' in custom_field['configs'][0]['options'] and len(custom_field['configs'][0]['options']['items']) > 0:
            values = self.__split_values(custom_field['configs'][0]['options']['items'])
            if type(value) == str or type(value) == int:
                if str(value) not in values.keys():
                    self.logger.log(f'[{self.project["code"]}][Tests] Custom field {custom_field["name"]} has invalid value {value}', 'warn')
                    return None
            elif type(value) == list:
                filtered_values = []
                for item in value:
                    if str(item) in values.keys():
                        filtered_values.append(item)
                    else:
                        self.logger.log(f'[{self.project["code"]}][Tests] Custom field {custom_field["name"]} has invalid value {value}', 'warn')
                if len(filtered_values) == 0:
                    return None
                else:
                    return filtered_values
            return value
        return None
    
    def __split_values(self, string: str, delimiter: str = ',') -> dict:
        items = string.split('\n')  # split items into a list
        result = {}
        for item in items:
            if item != '' and item != None:
                key, value = item.split(delimiter)
                result[key] = value
        return result
    
    # Done
    def _set_priority(self, case: dict, data: dict) -> dict:
        data['priority'] = self.mappings.priorities[case['priority_id']] if case['priority_id'] in self.mappings.priorities else 1
        return data
    
    # Done
    def _set_type(self, case: dict, data: dict) -> dict:
        data['type'] = self.mappings.types[case['type_id']] if case['type_id'] in self.mappings.types else 1
        return data
    
    def _set_status(self, case: dict, data: dict) -> dict:
        # Not used yet for Zephyr Enterprise
        return data
        data['status'] = self.mappings.case_statuses[case['status_id']] if case['status_id'] in self.mappings.case_statuses else 1
        return data
    
    # Done
    def _get_suite_id(self, suite_id: Optional[int] = None) -> int:
        if (suite_id and suite_id in self.mappings.suites[self.project['code']]):
            return self.mappings.suites[self.project['code']][suite_id]
        return None
    
    def _set_milestone(self, case: dict, data: dict, code: str) -> dict:
        if case['milestone_id'] and code in self.mappings.milestones and case['milestone_id'] in self.mappings.milestones[code]:
            data['milestone_id'] = self.mappings.milestones[code][case['milestone_id']]
        return data