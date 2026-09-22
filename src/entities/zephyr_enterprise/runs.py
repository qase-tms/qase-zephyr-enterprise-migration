import asyncio
import datetime
import html
import os
import re
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ...service import QaseService, ZephyrEnterpriseService
from ...support import Logger, Mappings, ConfigManager as Config, Pools


# Zephyr files are buffered fully in memory before Qase upload; same cap as case attachments.
_MAX_RESULT_ATTACHMENT_BYTES = 32 * 1024 * 1024


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _unwrap_releases_list(raw) -> List[dict]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("results", "data", "releases", "releaseDTOs", "entities", "entity", "items"):
            v = raw.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
        if raw.get("id") is not None and (
            raw.get("name") is not None or raw.get("releaseName") is not None
        ):
            return [raw]
        for v in raw.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [x for x in v if isinstance(x, dict)]
    return []


def _zephyr_execution_testcase_id(ex: dict) -> Optional[int]:
    if not isinstance(ex, dict):
        return None
    tcr = ex.get("tcrTreeTestcase") or ex.get("tcrCatalogTreeTestcase")
    if isinstance(tcr, dict):
        tc = tcr.get("testcase")
        if isinstance(tc, dict):
            for key in ("id", "testcaseId"):
                if tc.get(key) is not None:
                    try:
                        return int(tc[key])
                    except (TypeError, ValueError):
                        pass
        for key in ("testcaseId", "testcaseVersionId", "testcase_id"):
            if tcr.get(key) is not None:
                try:
                    return int(tcr[key])
                except (TypeError, ValueError):
                    pass
    tc_top = ex.get("testcase")
    if isinstance(tc_top, dict):
        for key in ("id", "testcaseId"):
            if tc_top.get(key) is not None:
                try:
                    return int(tc_top[key])
                except (TypeError, ValueError):
                    pass
    for k in ("testcaseId", "testcaseid"):
        v = ex.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _zephyr_run_group_key(ex: dict, group_by: str) -> tuple:
    """Hashable key to split Zephyr executions into separate Qase runs within one release."""
    gb = (group_by or "cycle_phase").strip().lower()
    if gb == "release":
        return ("rel", 0)
    ltr = ex.get("lastTestResult") if isinstance(ex.get("lastTestResult"), dict) else {}
    cp = ex.get("cyclePhaseId")
    cid = ex.get("cycleId")
    if isinstance(ex.get("cycle"), dict):
        cid = cid or ex["cycle"].get("id")
    rts = ltr.get("releaseTestScheduleId")
    try:
        cp_i = int(cp) if cp is not None else 0
    except (TypeError, ValueError):
        cp_i = 0
    try:
        ci_i = int(cid) if cid is not None else 0
    except (TypeError, ValueError):
        ci_i = 0
    if gb == "phase_schedule":
        try:
            rs_i = int(rts) if rts is not None else 0
        except (TypeError, ValueError):
            rs_i = 0
        return ("ps", cp_i, rs_i)
    # cycle_phase (default): one Qase run per Zephyr cycle + phase when ids are present
    return ("cyc", ci_i, cp_i)


def _runs_config_untested_raw_ids(config: Config) -> Set[str]:
    """Raw Zephyr ``executionStatus`` ids that mean “not executed yet” (optional; see ``runs.zephyr_untested_execution_status_ids``)."""
    raw = config.get("runs.zephyr_untested_execution_status_ids")
    if raw is None:
        return set()
    if isinstance(raw, (int, float, str)):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    out: Set[str] = set()
    for x in raw:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.add(s)
    return out


def _zephyr_execution_keeps_qase_run_open(
    status_id: Optional[str],
    *,
    status_map: Dict[str, str],
    untested_raw_ids: Set[str],
) -> bool:
    """True if this execution should leave the Qase run open (in progress, not completed)."""
    if status_id is None or (isinstance(status_id, str) and not str(status_id).strip()):
        s = "0"
    else:
        s = str(status_id).strip()
    if s in untested_raw_ids:
        return True
    if (status_map or {}).get(s) in ("in_progress", "untested"):
        return True
    return False


def _cycle_id_for_run_title_prefetch(
    key: tuple,
    sample_ex: Optional[dict],
    group_by: str,
) -> Optional[int]:
    """Cycle id used for ``GET cycle/{{id}}`` in title resolution (for parallel prefetch)."""
    gb = (group_by or "cycle_phase").strip().lower()
    if gb == "release":
        return None
    cycle_id = None
    if key and key[0] == "cyc":
        _, ci, _ = key
        try:
            cycle_id = int(ci) if ci else None
        except (TypeError, ValueError):
            cycle_id = None
    if sample_ex and isinstance(sample_ex, dict):
        if not cycle_id:
            cid = sample_ex.get("cycleId")
            if cid is None and isinstance(sample_ex.get("cycle"), dict):
                cid = sample_ex["cycle"].get("id")
            if cid is not None:
                try:
                    cycle_id = int(cid)
                except (TypeError, ValueError):
                    pass
    if cycle_id is not None and cycle_id > 0:
        return cycle_id
    return None


def _zephyr_execution_id(ex: dict) -> Optional[int]:
    """Top-level execution row id (== ``releaseTestScheduleId``) used as ``itemid`` for v3 attachments."""
    if not isinstance(ex, dict):
        return None
    for key in ("id", "executionId"):
        v = ex.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    ltr = ex.get("lastTestResult") if isinstance(ex.get("lastTestResult"), dict) else {}
    rts = ltr.get("releaseTestScheduleId") if isinstance(ltr, dict) else None
    if rts is not None:
        try:
            return int(rts)
        except (TypeError, ValueError):
            pass
    return None


def _zephyr_execution_attachment_count(ex: dict) -> int:
    """Best-effort ``attachmentCount`` from the advancesearch row (top-level or nested ``lastTestResult``)."""
    best = 0
    for src in (ex, ex.get("lastTestResult") if isinstance(ex, dict) else None):
        if not isinstance(src, dict):
            continue
        for key in ("attachmentCount", "attachmentsCount", "attachment_count"):
            try:
                n = int(src.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if n > best:
                best = n
    return best


def _zephyr_attachment_flex_file_id(item: dict) -> Optional[str]:
    """Flex ``fileId`` UUID for ``/flex/download``; v3 attachment rows use ``refId``."""
    if not isinstance(item, dict):
        return None
    for k in ("refId", "ref_id", "fileId", "file_id", "uuid"):
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


def _zephyr_attachment_suggested_name(item: dict) -> str:
    if not isinstance(item, dict):
        return "zephyr-result-attachment.bin"
    for k in ("originalFileName", "fileName", "name", "title", "attachmentName"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "zephyr-result-attachment.bin"


def _safe_upload_filename(name: str) -> str:
    """Strip any path components and characters Qase rejects in attachment filenames."""
    s = (name or "").strip() or "zephyr-result-attachment.bin"
    s = os.path.basename(s)
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s)
    return s[:255] or "zephyr-result-attachment.bin"


def _zephyr_execution_tester_id(ex: dict) -> int:
    """Resolve Zephyr user id who executed / owns the result (field names vary by API payload)."""
    if not isinstance(ex, dict):
        return 0
    ltr = ex.get("lastTestResult") if isinstance(ex.get("lastTestResult"), dict) else {}
    for v in (
        ex.get("testerId"),
        ex.get("executedBy"),
        ex.get("executedById"),
        ex.get("assigneeId"),
        ex.get("assignedTo"),
        ex.get("assignmentUserId"),
        ex.get("userId"),
        ltr.get("testerId"),
        ltr.get("executedBy"),
        ltr.get("executedById"),
        ltr.get("userId"),
    ):
        if v is None:
            continue
        try:
            i = int(v)
            if i > 0:
                return i
        except (TypeError, ValueError):
            continue
    return 0


def _qase_run_author_zephyr_id(canonical_rows: List[dict], ex_rows: List[dict]) -> int:
    """Zephyr user id for Qase ``run.author_id``: majority tester in final rows, else first execution row."""
    counts: Counter = Counter()
    for r in canonical_rows:
        try:
            zid = int(r.get("created_by") or 0)
        except (TypeError, ValueError):
            zid = 0
        if zid > 0:
            counts[zid] += 1
    if counts:
        zid, _n = counts.most_common(1)[0]
        return int(zid)
    for ex in ex_rows or []:
        t = _zephyr_execution_tester_id(ex if isinstance(ex, dict) else {})
        if t > 0:
            return t
    return 0


def _latest_result_row_per_zephyr_test_id(results_payload: List[dict]) -> List[dict]:
    """Zephyr can return multiple execution rows per testcase; use latest ``created_on`` for run state."""
    best: Dict[int, Tuple[int, dict]] = {}
    for r in results_payload:
        tid = r.get("test_id")
        if tid is None:
            continue
        try:
            tid_i = int(tid)
        except (TypeError, ValueError):
            continue
        co = int(r.get("created_on") or 0)
        prev = best.get(tid_i)
        if prev is None or co >= prev[0]:
            best[tid_i] = (co, r)
    return [t[1] for t in best.values()]


def _bucket_executions_by_zephyr_run(
    executions: List[dict], group_by: str
) -> List[Tuple[tuple, List[dict]]]:
    buckets: Dict[tuple, List[dict]] = defaultdict(list)
    for ex in executions:
        if not isinstance(ex, dict):
            continue
        k = _zephyr_run_group_key(ex, group_by)
        buckets[k].append(ex)
    return sorted(buckets.items(), key=lambda kv: kv[0])


def _normalize_cycle_name_for_run_title(cname: str) -> str:
    """Use ``1`` / ``2`` in titles when Zephyr names are ids or ``Cycle 1``-style (no ``Cycle`` prefix)."""
    s = (cname or "").strip()
    if not s:
        return s
    if s in ("1", "2"):
        return s
    low = s.lower()
    if low.startswith("cycle ") and len(s) > 6:
        rest = s[6:].strip()
        if rest in ("1", "2"):
            return rest
    return s


def _format_zephyr_qase_run_title(
    rname: str,
    cname: str,
    pname: str,
    *,
    schedule_id: Optional[int] = None,
) -> str:
    """``[Release][Cycle] - Phase`` with optional `` - schedule N`` (no Zephyr suffix)."""
    cname = _normalize_cycle_name_for_run_title(cname or "")
    pname = (pname or "").strip()
    if cname and pname:
        t = f"[{rname}][{cname}] - {pname}"
    elif cname:
        t = f"[{rname}][{cname}]"
    elif pname:
        t = f"[{rname}] - {pname}"
    else:
        return ""
    if schedule_id is not None:
        t = f"{t} - schedule {schedule_id}"
    return t


def _qase_run_title_for_zephyr_group(
    rname: str,
    key: tuple,
    group_by: str,
    index: int,
    n_groups: int,
) -> str:
    gb = (group_by or "cycle_phase").strip().lower()
    if gb == "release" or n_groups <= 1:
        return f"[{rname}]"
    if gb == "phase_schedule" and key and key[0] == "ps":
        _, cp, rs = key
        return f"[{rname}] - phase {cp}, schedule {rs}"
    if gb == "cycle_phase" and key and key[0] == "cyc":
        _, ci, cp = key
        if ci:
            try:
                ci_n = int(ci)
            except (TypeError, ValueError):
                ci_n = None
            cycle_seg = str(ci_n) if ci_n in (1, 2) else f"cycle {ci}"
            return f"[{rname}] - {cycle_seg}, phase {cp}"
        if cp:
            return f"[{rname}] - phase {cp}"
    return f"[{rname}] [{index + 1}/{n_groups}]"


def _phase_name_from_cycle_detail(detail: dict, phase_id: int) -> str:
    """Match ``cyclePhases[].id`` to phase ``name`` from ``GET cycle/{{id}}``."""
    if not detail or not phase_id:
        return ""
    try:
        want = int(phase_id)
    except (TypeError, ValueError):
        return ""
    for ph in detail.get("cyclePhases") or []:
        if not isinstance(ph, dict):
            continue
        try:
            pid = int(ph.get("id"))
        except (TypeError, ValueError):
            continue
        if pid == want:
            return str(ph.get("name") or "").strip()
    return ""


def _phase_labels_from_cycles_list(cycles: List[dict]) -> Dict[int, Tuple[str, str]]:
    """Map ``cyclePhaseId`` → (cycle name, phase name) from ``get_cycles_for_release`` / cycle list API."""
    out: Dict[int, Tuple[str, str]] = {}
    for cyc in cycles or []:
        if not isinstance(cyc, dict):
            continue
        cname = str(cyc.get("name") or "").strip()
        for ph in cyc.get("cyclePhases") or []:
            if not isinstance(ph, dict):
                continue
            try:
                pid = int(ph.get("id"))
            except (TypeError, ValueError):
                continue
            pname = str(ph.get("name") or "").strip()
            out[pid] = (cname, pname)
    return out


def _zephyr_timestamp_seconds(v) -> Optional[int]:
    """Coerce Zephyr date fields (epoch ms or locale strings like ``08/23/2023``) to Unix seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        vi = int(v)
        return vi // 1000 if vi > 10_000_000_000 else vi
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit():
            vi = int(s)
            return vi // 1000 if vi > 10_000_000_000 else vi
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(s, fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


class Runs:
    """Import Zephyr test executions as Qase runs + bulk results (per release)."""

    DEFAULT_EXEC_STATUS_TO_QASE = {
        "1": "passed",
        "2": "failed",
        "3": "in_progress",
        "4": "blocked",
        # Zephyr "Unexecuted" (0) and the legacy "5" slot: treated the same as in_progress —
        # the result row is dropped from the Qase bulk (case stays Untested in the run UI)
        # and the Qase run is left open instead of being marked completed.
        "0": "untested",
        "5": "untested",
    }

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrEnterpriseService,
        logger: Logger,
        mappings: Mappings,
        config: Config,
        project: dict,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.config = config
        self.logger = logger
        self.mappings = mappings
        self.project = project
        self.pools = pools

        self.created_after = self.config.get("runs.created_after")
        self._zephyr_cycle_detail_cache: Dict[int, Optional[dict]] = {}
        self._release_phase_labels: Dict[int, Tuple[str, str]] = {}
        self.logger.divider()

    def import_runs(self) -> Mappings:
        return asyncio.run(self.import_runs_async())

    async def _get_zephyr_cycle_detail(self, cycle_id: Optional[int]) -> Optional[dict]:
        """Cached ``GET cycle/{{cycle_id}}`` for cycle + phase display names."""
        if cycle_id is None:
            return None
        try:
            cid = int(cycle_id)
        except (TypeError, ValueError):
            return None
        if cid <= 0:
            return None
        if cid in self._zephyr_cycle_detail_cache:
            return self._zephyr_cycle_detail_cache[cid]
        raw = await self.pools.source(self.zephyr.get_cycle, cid)
        if isinstance(raw, dict) and raw.get("id") is not None:
            self._zephyr_cycle_detail_cache[cid] = raw
            return raw
        self._zephyr_cycle_detail_cache[cid] = None
        return None

    async def _resolve_qase_run_title(
        self,
        rname: str,
        key: tuple,
        group_by: str,
        index: int,
        n_groups: int,
        sample_ex: Optional[dict],
    ) -> str:
        """Build Qase run title using Zephyr cycle name + phase name when possible."""
        gb = (group_by or "cycle_phase").strip().lower()
        if gb == "release":
            return f"[{rname}]"

        cycle_id = None
        phase_id = None
        sched_id = None

        if key and key[0] == "cyc":
            _, ci, cp = key
            try:
                cycle_id = int(ci) if ci else None
            except (TypeError, ValueError):
                cycle_id = None
            try:
                phase_id = int(cp) if cp else None
            except (TypeError, ValueError):
                phase_id = None
        elif key and key[0] == "ps":
            _, cp, rs = key
            try:
                phase_id = int(cp) if cp else None
            except (TypeError, ValueError):
                phase_id = None
            try:
                sched_id = int(rs) if rs else None
            except (TypeError, ValueError):
                sched_id = None

        if sample_ex and isinstance(sample_ex, dict):
            if not cycle_id:
                cid = sample_ex.get("cycleId")
                if cid is None and isinstance(sample_ex.get("cycle"), dict):
                    cid = sample_ex["cycle"].get("id")
                if cid is not None:
                    try:
                        cycle_id = int(cid)
                    except (TypeError, ValueError):
                        pass
            if not phase_id:
                pe = sample_ex.get("cyclePhaseId")
                if pe is not None:
                    try:
                        phase_id = int(pe)
                    except (TypeError, ValueError):
                        pass

        detail = await self._get_zephyr_cycle_detail(cycle_id) if cycle_id else None
        cname = str((detail or {}).get("name") or "").strip()
        pname = _phase_name_from_cycle_detail(detail or {}, phase_id or 0)

        if (not cname or not pname) and phase_id:
            try:
                pid = int(phase_id)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                pair = (getattr(self, "_release_phase_labels", None) or {}).get(pid)
                if pair:
                    oc, op = pair
                    if not cname and oc:
                        cname = oc
                    if not pname and op:
                        pname = op

        sched_kw = None
        if gb == "phase_schedule" and sched_id is not None:
            sched_kw = int(sched_id)

        if cname or pname:
            t = _format_zephyr_qase_run_title(
                rname, cname, pname, schedule_id=sched_kw
            )
            if t:
                return t

        return _qase_run_title_for_zephyr_group(rname, key, group_by, index, n_groups)

    async def _gather_executions_for_release(
        self,
        code: str,
        zpid: int,
        rid_int: int,
        page_size: int,
    ) -> Tuple[List[dict], str]:
        """Collect schedules: default is per-release ``advancesearch`` (execution entity); optional hybrid/cycle/release."""
        mode = (self.config.get("runs.zephyr_execution_fetch") or "advancesearch").strip().lower()
        rp = page_size if page_size > 0 else 100
        seen_ids: Set[int] = set()
        out: List[dict] = []
        parts: List[str] = []

        def add_batch(batch: List[dict], label: str) -> int:
            n = 0
            for x in batch:
                if not isinstance(x, dict):
                    continue
                eid = x.get("id")
                if eid is not None:
                    try:
                        ek = int(eid)
                    except (TypeError, ValueError):
                        ek = None
                    if ek is not None:
                        if ek in seen_ids:
                            continue
                        seen_ids.add(ek)
                out.append(x)
                n += 1
            if n and label:
                parts.append(f"{label}:{n}")
            return n

        if mode in ("hybrid", "cycle", "all"):
            cycles = await self.pools.source(
                self.zephyr.get_cycles_for_release, zpid, rid_int
            )
            if not isinstance(cycles, list):
                cycles = []
            ccount = 0
            for c in cycles:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id")
                if cid is None:
                    continue
                try:
                    cid_i = int(cid)
                except (TypeError, ValueError):
                    continue
                phases = c.get("cyclePhases") or c.get("phases") or []
                for ph in phases:
                    if not isinstance(ph, dict) or ph.get("id") is None:
                        continue
                    try:
                        pid_i = int(ph["id"])
                    except (TypeError, ValueError):
                        continue
                    batch = await self.pools.source(
                        self.zephyr.get_executions_by_cycle_phase, cid_i, pid_i
                    )
                    rows_cp = [x for x in (batch or []) if isinstance(x, dict)]
                    if rows_cp:
                        ccount += add_batch(rows_cp, "")
            if cycles:
                parts.insert(0, f"cycles:{len(cycles)}")
            if ccount:
                parts.append(f"cycle_exec_rows:{ccount}")

        want_release = mode in ("hybrid", "release", "all")
        if want_release and mode != "cycle" and mode != "advancesearch":
            offset = 0
            prev_head_id = None
            rtotal = 0
            while True:
                batch = await self.pools.source(
                    self.zephyr.search_executions_for_release,
                    rid_int,
                    zpid,
                    None,
                    offset,
                    rp,
                )
                if not batch:
                    break
                rows = [x for x in batch if isinstance(x, dict)]
                head_id = rows[0].get("id") if rows else None
                if (
                    head_id is not None
                    and head_id == prev_head_id
                    and offset > 0
                ):
                    self.logger.log(
                        f"[{code}][Runs] release execution list ignored offset (duplicate page); stop",
                        "warn",
                    )
                    break
                prev_head_id = head_id
                rtotal += add_batch(rows, "")
                if rp <= 0 or len(rows) < rp or len(rows) > rp:
                    break
                offset += len(rows)
            if rtotal:
                parts.append(f"release_exec:{rtotal}")

        want_search = mode in ("advancesearch", "all") or (
            mode == "hybrid" and not out
        )
        if want_search and mode != "cycle" and mode != "release":
            fr = 0
            adv_cap = self.config.get("runs.advancesearch_max_results")
            if adv_cap is not None:
                try:
                    mr = max(int(adv_cap), 1)
                except (TypeError, ValueError):
                    mr = max(rp, 100) if page_size > 0 else 100
            else:
                mr = max(rp, 100) if page_size > 0 else 100
            stotal = 0
            while True:
                batch = await self.pools.source(
                    self.zephyr.search_executions_advancesearch, rid_int, fr, mr
                )
                rows = [x for x in (batch or []) if isinstance(x, dict)]
                if not rows:
                    break
                stotal += add_batch(rows, "")
                if len(rows) < mr:
                    break
                fr += len(rows)
            if stotal:
                parts.append(f"advancesearch:{stotal}")

        summary = ", ".join(parts) if parts else mode
        return out, summary

    async def _prepare_execution_attachments(
        self,
        code: str,
        rname: str,
        ex_rows: List[dict],
    ) -> Dict[int, List[str]]:
        """For one Qase run group: fetch v3 result attachments per execution, download each unique
        file once, upload to Qase, return ``{execution_id: [qase_hash, ...]}``.

        Skip strategy:
          - Executions with ``attachmentCount==0`` are not probed (saves ~99% of network calls
            on typical Zephyr datasets).
          - Files are deduped by Zephyr ``refId`` within the run group, so the same attachment
            referenced by multiple results uploads only once.

        Endpoint: only ``/v3/attachment?itemid=<execution.id>&type=releaseTestSchedule``.
        Probe-confirmed: legacy ``attachment/list`` shapes 404 for executions, so they are skipped.
        """
        out: Dict[int, List[str]] = {}
        if not ex_rows:
            return out

        ids_with_files: List[int] = []
        seen_ids: Set[int] = set()
        for ex in ex_rows:
            if not isinstance(ex, dict):
                continue
            ac = _zephyr_execution_attachment_count(ex)
            if ac <= 0:
                continue
            eid = _zephyr_execution_id(ex)
            if eid is None or eid in seen_ids:
                continue
            seen_ids.add(eid)
            ids_with_files.append(eid)

        if not ids_with_files:
            return out

        self.logger.log(
            f"[{code}][Runs] Release {rname!r}: prefetch result attachments for "
            f"{len(ids_with_files)} execution(s) with attachmentCount>0"
        )

        async def _list_one(eid: int) -> Tuple[int, List[dict]]:
            try:
                rows = await self.pools.source(
                    self.zephyr.get_attachments_for_execution, eid
                )
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Failed to list result attachments for execution_id={eid}: {e}",
                    "warn",
                )
                return eid, []
            return eid, [r for r in (rows or []) if isinstance(r, dict)]

        listings = await asyncio.gather(*[_list_one(eid) for eid in ids_with_files])

        exec_to_fids: Dict[int, List[str]] = {}
        unique_files: Dict[str, str] = {}
        for eid, rows in listings:
            fids: List[str] = []
            for r in rows:
                fid = _zephyr_attachment_flex_file_id(r)
                if not fid:
                    continue
                if fid not in fids:
                    fids.append(fid)
                unique_files.setdefault(fid, _zephyr_attachment_suggested_name(r))
            if fids:
                exec_to_fids[eid] = fids

        if not unique_files:
            return out

        self.logger.log(
            f"[{code}][Runs] Release {rname!r}: {len(unique_files)} unique result file(s) to download + upload"
        )

        async def _download_one(fid: str, default_name: str):
            try:
                content, remote_name = await self.pools.source(
                    self.zephyr.download_attachment_by_file_id, fid
                )
                return fid, default_name, content, remote_name, None
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Zephyr result download failed fileId={fid!r}: {e}",
                    "warn",
                )
                return fid, default_name, None, None, e

        downloaded = await asyncio.gather(
            *[_download_one(fid, name) for fid, name in unique_files.items()]
        )

        async def _upload_one(fid: str, default_name: str, content: bytes, remote_name: Optional[str]):
            fname = _safe_upload_filename(remote_name or default_name)
            try:
                res = await self.pools.qs(
                    self.qase.upload_attachment, code, (fname, content)
                )
            except Exception as e:
                self.logger.log(
                    f"[{code}][Runs] Qase upload failed for result file {fname!r} (fileId={fid!r}): {e}",
                    "warn",
                )
                return fid, None
            if not res:
                self.logger.log(
                    f"[{code}][Runs] Qase upload returned empty for result file {fname!r}",
                    "warn",
                )
                return fid, None
            h = res.get("hash")
            if not h:
                self.logger.log(
                    f"[{code}][Runs] Qase upload response missing hash for {fname!r}: {res!r}"[:500],
                    "warn",
                )
                return fid, None
            return fid, h

        upload_jobs = []
        for fid, default_name, content, _remote_name, err in downloaded:
            if err is not None or content is None:
                continue
            if len(content) > _MAX_RESULT_ATTACHMENT_BYTES:
                self.logger.log(
                    f"[{code}][Runs] Skip result attachment fileId={fid!r}: "
                    f"{len(content)} bytes > max {_MAX_RESULT_ATTACHMENT_BYTES}",
                    "warn",
                )
                continue
            upload_jobs.append(
                _upload_one(fid, default_name, content, _remote_name)
            )

        uploaded = await asyncio.gather(*upload_jobs) if upload_jobs else []
        fid_to_hash: Dict[str, str] = {fid: h for fid, h in uploaded if h}

        if not fid_to_hash:
            return out

        for eid, fids in exec_to_fids.items():
            hashes = [fid_to_hash[f] for f in fids if f in fid_to_hash]
            if hashes:
                out[eid] = hashes

        self.logger.log(
            f"[{code}][Runs] Release {rname!r}: result attachments ready for "
            f"{len(out)} execution(s) ({sum(len(v) for v in out.values())} attachment ref(s))"
        )
        return out

    async def import_runs_async(self) -> Mappings:
        code = self.project["code"]
        zpid = self.project["zephyr_id"]

        tc_map: Dict[int, int] = self.mappings.zephyr_tc_id_to_qase_case_id.get(code, {}) or {}
        if not tc_map:
            self.logger.log(
                f"[{code}][Runs] No Zephyr testcase → Qase case id map (import cases first). "
                f"Skipping runs.",
                "warn",
            )
            return self.mappings

        releases_raw = await self.pools.source(self.zephyr.get_releases, zpid)
        releases = _unwrap_releases_list(releases_raw)
        if not releases:
            self.logger.log(
                f"[{code}][Runs] No releases parsed for Zephyr project id={zpid} "
                f"(raw type={type(releases_raw).__name__}). "
                f"If releases exist in Zephyr, check ``GET release/project/{zpid}`` response shape.",
                "warn",
            )
            return self.mappings

        self.logger.log(
            f"[{code}][Runs] {len(releases)} release(s), {len(tc_map)} Zephyr testcase id(s) mapped to Qase cases"
        )
        self._zephyr_cycle_detail_cache.clear()
        self._release_phase_labels = {}

        status_map = dict(self.DEFAULT_EXEC_STATUS_TO_QASE)
        for k, v in (self.mappings.result_statuses or {}).items():
            if v:
                status_map[str(k)] = str(v)
        for k, v in (self.config.get("runs.status_map") or {}).items():
            if v:
                status_map[str(k).strip()] = str(v).strip().lower()

        untested_raw_ids = _runs_config_untested_raw_ids(self.config)

        page_size = int(self.config.get("runs.page_size", 100) or 100)
        if page_size < 0:
            page_size = 0

        for rel in releases:
            rid = rel.get("id")
            rname = rel.get("name") or f"release-{rid}"
            if rid is None:
                continue
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue

            group_by = (self.config.get("runs.zephyr_run_group_by") or "cycle_phase").strip().lower()
            milestone_id = None
            mm = self.mappings.milestones.get(code, {})
            if mm:
                milestone_id = mm.get(rid_int, mm.get(rid))

            # ``release`` grouping does not need cycle/phase labels — skip an extra Zephyr round-trip.
            if group_by == "release":
                self._release_phase_labels = {}
                t0 = time.perf_counter()
                executions, fetch_summary = await self._gather_executions_for_release(
                    code, zpid, rid_int, page_size
                )
                self.logger.log(
                    f"[{code}][Runs] Release {rname!r} (id={rid}): "
                    f"execution fetch only — {_ms_since(t0):.0f}ms ({fetch_summary})"
                )
            else:
                async def _timed_cycles():
                    t0 = time.perf_counter()
                    raw = await self.pools.source(
                        self.zephyr.get_cycles_for_release, zpid, rid_int
                    )
                    return raw, _ms_since(t0)

                async def _timed_executions():
                    t0 = time.perf_counter()
                    out = await self._gather_executions_for_release(
                        code, zpid, rid_int, page_size
                    )
                    return out, _ms_since(t0)

                (cycles_raw, ms_cycles), ((executions, fetch_summary), ms_exec) = await asyncio.gather(
                    _timed_cycles(),
                    _timed_executions(),
                )
                self.logger.log(
                    f"[{code}][Runs] Release {rname!r} (id={rid}): "
                    f"Zephyr parallel fetch — get_cycles_for_release={ms_cycles:.0f}ms, "
                    f"executions ({fetch_summary})={ms_exec:.0f}ms "
                    f"(wall-clock ≈ max of the two)"
                )
                cycles_for_rel = cycles_raw if isinstance(cycles_raw, list) else []
                self._release_phase_labels = _phase_labels_from_cycles_list(cycles_for_rel)

            if not executions:
                self.logger.log(
                    f"[{code}][Runs] Release {rname!r} (id={rid}): no execution rows "
                    f"(fetch={fetch_summary}). See log line ``[Zephyr][advancesearch]`` above "
                    f"for response shape; set ``runs.log_zephyr_executions`` true for full JSON. "
                    f"Try ``runs.advancesearch_append_zql_word`` true or "
                    f"``runs.zephyr_execution_fetch`` ``hybrid`` if this server needs other APIs."
                )
                continue

            self.logger.log(
                f"[{code}][Runs] Release {rname!r} (id={rid}): {len(executions)} schedule row(s) "
                f"({fetch_summary})"
            )

            if self.created_after:
                try:
                    ca = int(self.created_after)
                except (TypeError, ValueError):
                    ca = 0
                if ca > 0:
                    filtered = []
                    for ex in executions:
                        ltr = (
                            ex.get("lastTestResult")
                            if isinstance(ex.get("lastTestResult"), dict)
                            else {}
                        )
                        ed = ltr.get("executionDate") or ltr.get("createDatetime")
                        try:
                            if ed is not None and int(ed) // 1000 < ca:
                                continue
                        except (TypeError, ValueError):
                            pass
                        filtered.append(ex)
                    executions = filtered
                    if not executions:
                        continue

            run_groups = _bucket_executions_by_zephyr_run(executions, group_by)
            self.logger.log(
                f"[{code}][Runs] Release {rname!r} (id={rid}): "
                f"{len(run_groups)} Qase run(s) from Zephyr (runs.zephyr_run_group_by={group_by!r})"
            )

            prefetch_ids: Set[int] = set()
            for gk, rows in run_groups:
                ex0 = rows[0] if rows else None
                pc = _cycle_id_for_run_title_prefetch(gk, ex0, group_by)
                if pc is not None:
                    prefetch_ids.add(pc)
            if prefetch_ids:
                await asyncio.gather(
                    *[self._get_zephyr_cycle_detail(cid) for cid in prefetch_ids]
                )

            pending_complete: List[dict] = []
            merged_result_statuses = {
                **(self.mappings.result_statuses or {}),
                **{str(k): v for k, v in status_map.items()},
            }
            parallel_run_groups = max(
                1, min(64, int(self.config.get("runs.qase_parallel_run_groups", 8) or 8))
            )
            run_jobs: List[dict] = []

            for gi, (_gkey, ex_rows) in enumerate(run_groups):
                results_payload: List[dict] = []
                qase_case_ids: List[int] = []
                seen_q: set = set()
                min_ts: Optional[int] = None
                max_ts: Optional[int] = None

                exec_attachment_hashes = await self._prepare_execution_attachments(
                    code, rname, ex_rows
                )

                for ex in ex_rows:
                    ztid = _zephyr_execution_testcase_id(ex)
                    if ztid is None or ztid not in tc_map:
                        continue
                    qcid = int(tc_map[ztid])
                    if qcid not in seen_q:
                        seen_q.add(qcid)
                        qase_case_ids.append(qcid)

                    ltr = (
                        ex.get("lastTestResult")
                        if isinstance(ex.get("lastTestResult"), dict)
                        else {}
                    )
                    st_raw = (
                        ex.get("executionStatus")
                        or ltr.get("executionStatus")
                        or ltr.get("status")
                        or ex.get("status")
                    )
                    st_key = str(st_raw).strip() if st_raw is not None else "0"

                    ex_ms = (
                        ltr.get("executionDate")
                        or ltr.get("createDatetime")
                        or ex.get("createDatetime")
                    )
                    created_on = _zephyr_timestamp_seconds(ex.get("assignmentDate"))
                    if created_on is None:
                        created_on = _zephyr_timestamp_seconds(ex_ms)
                    if created_on is None:
                        created_on = int(time.time())
                    min_ts = created_on if min_ts is None else min(min_ts, created_on)
                    max_ts = created_on if max_ts is None else max(max_ts, created_on)

                    elapsed_sec = 0
                    at = ex.get("actualTime")
                    if at is not None:
                        try:
                            ai = int(at)
                            elapsed_sec = ai // 1000 if ai > 1000 else ai
                        except (TypeError, ValueError):
                            elapsed_sec = 0

                    comment = ""
                    for k in ("comment", "notes", "executionNotes"):
                        v = ex.get(k)
                        if v is None and isinstance(ltr, dict):
                            v = ltr.get(k)
                        if isinstance(v, str) and v.strip():
                            comment = html.unescape(v.strip())
                            break

                    created_by = _zephyr_execution_tester_id(ex)

                    row_payload: dict = {
                        "test_id": ztid,
                        "status_id": st_key,
                        "created_on": created_on,
                        "elapsed": elapsed_sec,
                        "comment": comment,
                        "created_by": created_by,
                    }
                    eid_for_row = _zephyr_execution_id(ex)
                    if eid_for_row is not None:
                        hashes = exec_attachment_hashes.get(eid_for_row)
                        if hashes:
                            row_payload["attachments"] = list(hashes)
                    results_payload.append(row_payload)

                if not results_payload or not qase_case_ids:
                    missing_ids: List[int] = []
                    unresolved = 0
                    for ex in ex_rows:
                        ztid = _zephyr_execution_testcase_id(ex)
                        if ztid is None:
                            unresolved += 1
                        elif ztid not in tc_map:
                            missing_ids.append(ztid)
                    label = await self._resolve_qase_run_title(
                        rname,
                        _gkey,
                        group_by,
                        gi,
                        len(run_groups),
                        ex_rows[0] if ex_rows else None,
                    )
                    if unresolved == len(ex_rows):
                        self.logger.log(
                            f"[{code}][Runs] {label}: {len(ex_rows)} Zephyr "
                            f"execution row(s) but testcase id could not be read "
                            f"(expected tcrTreeTestcase.testcase.id / testcaseId). "
                            f"Set ``runs.log_zephyr_executions`` true for raw advancesearch JSON."
                        )
                    else:
                        sample = sorted(set(missing_ids))[:12]
                        more = (
                            f" (+{len(set(missing_ids)) - len(sample)} more)"
                            if len(set(missing_ids)) > len(sample)
                            else ""
                        )
                        self.logger.log(
                            f"[{code}][Runs] {label}: Zephyr returned "
                            f"{len(ex_rows)} execution row(s) but none mapped to imported Qase cases "
                            f"(Zephyr testcase id(s) not in import map; sample: {sample}{more}). "
                            f"Re-import cases for this project or align testcase ids."
                        )
                    continue

                c_on = min_ts or int(time.time())
                z_on = max_ts or c_on
                if z_on < c_on:
                    z_on = c_on
                run_title = await self._resolve_qase_run_title(
                    rname,
                    _gkey,
                    group_by,
                    gi,
                    len(run_groups),
                    ex_rows[0] if ex_rows else None,
                )
                canonical_rows = _latest_result_row_per_zephyr_test_id(results_payload)
                run_zephyr_author = _qase_run_author_zephyr_id(canonical_rows, ex_rows)
                has_open_execution = any(
                    _zephyr_execution_keeps_qase_run_open(
                        r.get("status_id"),
                        status_map=status_map,
                        untested_raw_ids=untested_raw_ids,
                    )
                    for r in canonical_rows
                )
                should_complete = not has_open_execution
                run_dict = {
                    "name": run_title,
                    "description": html.unescape(str(rel.get("description") or "")),
                    "created_on": c_on,
                    "completed_on": z_on,
                    # Completion is applied via POST /run/{code}/{id}/complete after bulk results.
                    "is_completed": False,
                    "author_id": self.mappings.get_user_id(run_zephyr_author),
                }

                cases_map = {
                    ztid: int(tc_map[ztid]) for ztid in set(r["test_id"] for r in results_payload)
                }
                run_jobs.append(
                    {
                        "run_dict": run_dict,
                        "results_payload": results_payload,
                        "qase_case_ids": qase_case_ids,
                        "cases_map": cases_map,
                        "should_complete": should_complete,
                        "run_title": run_title,
                    }
                )

            if run_jobs:
                sem = asyncio.Semaphore(parallel_run_groups)

                async def _upload_one_run(job: dict):
                    async with sem:
                        qid = await self.pools.qs(
                            self.qase.create_run,
                            job["run_dict"],
                            code,
                            job["qase_case_ids"],
                            milestone_id,
                        )
                        if not qid:
                            return {"ok": False, "job": job}
                        await self.pools.qs(
                            self.qase.send_bulk_results,
                            job["run_dict"],
                            job["results_payload"],
                            qid,
                            code,
                            self.mappings,
                            job["cases_map"],
                            merged_result_statuses,
                        )
                        return {"ok": True, "job": job, "qase_run_id": int(qid)}

                upload_results = await asyncio.gather(
                    *[_upload_one_run(j) for j in run_jobs],
                    return_exceptions=True,
                )
                for up in upload_results:
                    if isinstance(up, BaseException):
                        self.logger.log(
                            f"[{code}][Runs] parallel run upload error: {up!r}",
                            "error",
                        )
                        continue
                    if not up.get("ok"):
                        self.logger.log(
                            f"[{code}][Runs] create_run failed for {up['job']['run_title']!r}",
                            "error",
                        )
                        continue
                    job = up["job"]
                    qase_run_id = up["qase_run_id"]
                    self.mappings.stats.add_entity_count(code, "runs", "qase", 1)
                    if not job["should_complete"]:
                        self.logger.log(
                            f"[{code}][Runs] Run {qase_run_id} ← {job['run_title']!r}: "
                            f"{len(job['results_payload'])} result(s), {len(job['qase_case_ids'])} case(s) in run — in progress"
                        )
                    else:
                        pending_complete.append(
                            {
                                "run_id": int(qase_run_id),
                                "title": job["run_title"],
                                "n_res": len(job["results_payload"]),
                                "n_cases": len(job["qase_case_ids"]),
                            }
                        )

            if pending_complete:
                comp_results = await asyncio.gather(
                    *[
                        self.pools.qs(self.qase.complete_run, code, p["run_id"])
                        for p in pending_complete
                    ],
                    return_exceptions=True,
                )
                for p, ok in zip(pending_complete, comp_results):
                    if isinstance(ok, BaseException):
                        self.logger.log(
                            f"[{code}][Runs] Run {p['run_id']} ← {p['title']!r}: "
                            f"{p['n_res']} result(s), {p['n_cases']} case(s) in run — "
                            f"POST /run/{code}/{p['run_id']}/complete error: {ok!r}",
                            "error",
                        )
                        continue
                    if ok:
                        state = "completed (POST /run/.../complete)"
                    else:
                        state = (
                            "complete endpoint failed (run may stay open in Qase — "
                            f"POST /run/{code}/{p['run_id']}/complete)"
                        )
                    self.logger.log(
                        f"[{code}][Runs] Run {p['run_id']} ← {p['title']!r}: "
                        f"{p['n_res']} result(s), {p['n_cases']} case(s) in run — {state}"
                    )

        return self.mappings
