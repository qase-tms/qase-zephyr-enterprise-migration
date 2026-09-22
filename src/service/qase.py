from ..support import ConfigManager, Logger

import certifi
import io
import json
import mimetypes
import os
import time
import urllib3.exceptions

from qaseio.api_client import ApiClient
from qaseio.configuration import Configuration
from qaseio.api.authors_api import AuthorsApi
from qaseio.api.custom_fields_api import CustomFieldsApi
from qaseio.api.system_fields_api import SystemFieldsApi
from qaseio.api.projects_api import ProjectsApi
from qaseio.api.suites_api import SuitesApi
from qaseio.api.runs_api import RunsApi
from qaseio.api.results_api import ResultsApi
from qaseio.api.attachments_api import AttachmentsApi
from qaseio.api.milestones_api import MilestonesApi
from qaseio.api.configurations_api import ConfigurationsApi
from qaseio.api.shared_steps_api import SharedStepsApi
from qaseio.api.cases_api import CasesApi

from qaseio.models import (
    Bulk200Response,
    TestCasebulk,
    SuiteCreate,
    MilestoneCreate,
    CustomFieldCreate,
    CustomFieldCreateValueInner,
    ProjectCreate,
    RunCreate,
    ResultcreateBulk,
    ConfigurationCreate,
    ConfigurationGroupCreate,
    SharedStepCreate,
    SharedStepContentCreate,
)

import traceback
from datetime import datetime

from qaseio.exceptions import ApiException


# The public cloud. Any other host is a dedicated cluster, which Qase serves at
# api-<host> and app-<host> rather than api.<host> and app.<host>. That fact is
# implied by qase.host, so there is no separate flag for it.
_PUBLIC_CLOUD_HOST = 'qase.io'


def is_dedicated_cluster(host: str) -> bool:
    return bool(host) and str(host).strip().lower() != _PUBLIC_CLOUD_HOST


def qase_api_url(config) -> str:
    """Base URL for the Qase REST API, derived from qase.host."""
    host = str(config.get('qase.host') or _PUBLIC_CLOUD_HOST).strip()
    scheme = 'http://' if config.get('qase.ssl') is False else 'https://'
    delimiter = '-' if is_dedicated_cluster(host) else '.'
    return f'{scheme}api{delimiter}{host}'


def qase_scim_host(config) -> str:
    """Hostname serving the SCIM API, derived from qase.host."""
    host = str(config.get('qase.host') or _PUBLIC_CLOUD_HOST).strip()
    delimiter = '-' if is_dedicated_cluster(host) else '.'
    return f'app{delimiter}{host}'


# Retry is not configuration: a customer has no basis on which to tune it and
# exposing it invites a worse run.
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_SEC = 2.0


class QaseService:
    # Max results per create_result_bulk call.
    #
    # Qase lowered the server-side bulk-results limit from 2000 to 200 to smooth
    # load spikes. The generated client still validates client-side at 2000, so
    # it will happily build a larger body that the API then rejects, and a single
    # oversized run used to lose every one of its results that way. Sit at the
    # new limit instead.
    #
    # The cost is extra HTTP calls, not extra data, and Qase writes were never
    # the bottleneck here; source reads are.
    _RESULT_BULK_CHUNK = 200

    def __init__(self, config: ConfigManager, logger: Logger):
        self.config = config
        self.logger = logger

        configuration = Configuration()
        configuration.api_key['TokenAuth'] = config.get('qase.api_token')
        configuration.host = f'{qase_api_url(config)}/v1'
        configuration.ssl_ca_cert = certifi.where()

        self.client = ApiClient(configuration)

    def _retry_io(self, label: str, fn):
        """Retry Qase SDK calls on transient TCP / TLS failures (connection reset, broken pipe, etc.)."""
        max_retries = _RETRY_MAX_ATTEMPTS
        backoff = _RETRY_BACKOFF_SEC
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except ApiException:
                raise
            except (
                urllib3.exceptions.ProtocolError,
                urllib3.exceptions.ReadTimeoutError,
                ConnectionError,
                TimeoutError,
                BrokenPipeError,
                OSError,
            ) as e:
                last_exc = e
                if attempt >= max_retries:
                    break
                delay = min(backoff * (2 ** attempt), 60.0)
                self.logger.log(
                    f'[{label}] transient I/O (attempt {attempt + 1}/{max_retries + 1}): {e!r}; '
                    f'retry in {delay:.1f}s',
                    "warn",
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _get_users(self, limit=100, offset=0):
        try:
            api_instance = AuthorsApi(self.client)
            # Get all authors.
            api_response = api_instance.get_authors(limit=limit, offset=offset, type="user")
            if api_response.status and api_response.result.entities:
                return api_response.result.entities
        except ApiException as e:
            self.logger.log("Exception when calling AuthorsApi->get_authors: %s\n" % e)

    def get_all_users(self, limit=100):
        offset = 0
        while True:
            result = self._get_users(limit, offset)
            yield result
            offset += limit
            if len(result) < limit:
                break

    def get_all_users_sync(self, limit=100):
        offset = 0
        result = []
        while True:
            result = result.extend(self._get_users(limit, offset))
            offset += limit
            if len(result) < limit:
                break
        return result

    def resolve_user_id(self, value):
        """Resolve ``users.default`` to a Qase user id.

        Accepts a numeric id or an email address. Asking a customer to find a
        numeric Qase user id is the single most common setup question this
        migration generates, so an email is the documented form.
        """
        if value is None:
            raise ValueError(
                "'users.default' is not set. Set it to the email address of the Qase "
                "user who should own migrated cases and runs, or to their numeric id."
            )
        if isinstance(value, bool):
            raise ValueError(
                f"'users.default' must be an email address or a numeric Qase user id, got {value!r}"
            )
        if isinstance(value, int):
            return value

        text = str(value).strip()
        if not text:
            raise ValueError(
                "'users.default' is empty. Set it to the email address of the Qase "
                "user who should own migrated cases and runs, or to their numeric id."
            )
        if text.isdigit():
            return int(text)
        if '@' not in text:
            raise ValueError(
                f"'users.default' is set to {text!r}, which is neither an email address "
                f"nor a numeric Qase user id."
            )

        for page in self.get_all_users():
            for user in page or []:
                user = user.to_dict() if hasattr(user, 'to_dict') else user
                if str(user.get('email', '')).lower() == text.lower():
                    return user['id']

        raise ValueError(
            f"'users.default' is set to {text!r} but no Qase user has that email address. "
            f"Invite them to the Qase workspace first, or use their numeric id."
        )

    def get_case_custom_fields(self):
        self.logger.log('Getting custom fields from Qase')
        try:
            def call():
                api_instance = CustomFieldsApi(self.client)
                api_response = api_instance.get_custom_fields(entity='case', limit=100)
                if api_response.status and api_response.result.entities:
                    return api_response.result.entities
                return []

            return self._retry_io("Qase CustomFieldsApi->get_custom_fields", call)
        except ApiException as e:
            self.logger.log("Exception when calling CustomFieldsApi->get_custom_fields: %s\n" % e)
        except (
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.ReadTimeoutError,
            ConnectionError,
            TimeoutError,
            BrokenPipeError,
            OSError,
        ) as e:
            self.logger.log(
                f"Exception when calling CustomFieldsApi->get_custom_fields (network, after retries): {e!r}",
                "error",
            )
        return None

    def create_custom_field(self, data) -> int:
        try:
            api_instance = CustomFieldsApi(self.client)
            # Create a custom field.
            api_response = api_instance.create_custom_field(custom_field_create=CustomFieldCreate(**data))
            if not api_response.status:
                self.logger.log('Error creating custom field: ' + data['title'])
            else:
                self.logger.log('Custom field created: ' + data['title'])
                return api_response.result.id
        except ApiException as e:
            self.logger.log('Exception when calling CustomFieldsApi->create_custom_field: %s\n' % e)
        return 0

    def create_configuration_group(self, project_code, title):
        try:
            api_instance = ConfigurationsApi(self.client)
            # Create a custom field.
            api_response = api_instance.create_configuration_group(
                code=project_code,
                configuration_group_create=ConfigurationGroupCreate(title=title)
            )
            if not api_response.status:
                self.logger.log('Error creating configuration group: ' + title)
            else:
                self.logger.log('Configuration group created: ' + title)
                return api_response.result.id
        except ApiException as e:
            self.logger.log('Exception when calling CustomFieldsApi->create_configuration_group: %s\n' % e)
        return 0

    def create_configuration(self, project_code, title, group_id):
        try:
            api_instance = ConfigurationsApi(self.client)
            # Create a custom field.
            api_response = api_instance.create_configuration(
                code=project_code,
                configuration_create=ConfigurationCreate(title=title, group_id=group_id)
            )
            if not api_response.status:
                self.logger.log('Error creating configuration: ' + title)
            else:
                self.logger.log('Configuration created: ' + title)
                return api_response.result.id
        except ApiException as e:
            self.logger.log('Exception when calling CustomFieldsApi->create_configuration: %s\n' % e)
        return 0

    def get_system_fields(self):
        try:
            def call():
                api_instance = SystemFieldsApi(self.client)
                api_response = api_instance.get_system_fields()
                if api_response.status and api_response.result:
                    return api_response.result
                return []

            return self._retry_io("Qase SystemFieldsApi->get_system_fields", call)
        except ApiException as e:
            self.logger.log("Exception when calling SystemFieldsApi->get_system_fields: %s\n" % e)
        except (
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.ReadTimeoutError,
            ConnectionError,
            TimeoutError,
            BrokenPipeError,
            OSError,
        ) as e:
            self.logger.log(
                f"Exception when calling SystemFieldsApi->get_system_fields (network, after retries): {e!r}",
                "error",
            )
        return []

    @staticmethod
    def __get_default_value(field):
        if 'configs' in field:
            if len(field['configs']) > 0:
                if 'options' in field['configs'][0]:
                    if 'default_value' in field['configs'][0]['options']:
                        return field['configs'][0]['options']['default_value']
        return None

    @staticmethod
    def __split_values(string: str, delimiter: str = ',') -> dict:
        items = string.split('\n')  # split items into a list
        result = {}
        for item in items:
            if item == '':
                continue
            key, value = item.split(delimiter)  # split each item into a key and a value
            result[key] = value
        return result

    def get_projects(self, limit=100, offset=0):
        try:
            api_instance = ProjectsApi(self.client)
            # Get all projects.
            api_response = api_instance.get_projects(limit, offset)
            if api_response.status and api_response.result:
                return api_response.result
        except ApiException as e:
            self.logger.log("Exception when calling ProjectsApi->get_projects: %s\n" % e)

    def create_project(self, title, description, code, group_id=None):
        api_instance = ProjectsApi(self.client)

        data = {
            'title': title,
            'code': code,
            'description': description if description else "",
            'settings': {
                'runs': {
                    'auto_complete': False,
                }
            }
        }

        if group_id is not None:
            data['group'] = group_id

        self.logger.log(f'Creating project: {title} [{code}]')
        try:
            api_response = api_instance.create_project(
                project_create=ProjectCreate(**data)
            )
            self.logger.log(f'Project was created: {api_response.result.code}')
            return True
        except ApiException as e:
            try:
                error = json.loads(e.body) if e.body else {}
            except (ValueError, TypeError):
                error = {}
            error_fields = error.get('errorFields') or []
            if (
                error.get('status') is False
                and error_fields
                and error_fields[0].get('error') == 'Project with the same code already exists.'
            ):
                self.logger.log(f'Project with the same code already exists: {code}. Using existing project.')
                return True

            self.logger.log('Exception when calling ProjectsApi->create_project: %s\n' % e)
            return False

    def create_suite(self, code: str, title: str, description: str, parent_id=None) -> int:
        api_instance = SuitesApi(self.client)
        api_response = api_instance.create_suite(
            code=code,
            suite_create=SuiteCreate(
                title=title,
                description=description if description else "",
                preconditions="",
                # parent_id = ID in Qase
                parent_id=parent_id
            )
        )
        return api_response.result.id

    def _build_case_bulk_json_body(self, chunk: list) -> dict:
        """Build JSON for ``POST /v1/case/{{code}}/bulk`` so case-level attachments apply.

        Qase expects each bulk case object to include ``steps_type`` (e.g. ``classic``) together
        with ``attachments: [\"hash\", ...]``. The generated ``TestCasebulkCasesInner`` model
        has no ``steps_type`` field, so we merge it into the dict after ``to_dict()`` — same
        shape as: ``{\"cases\": [{\"steps_type\": \"classic\", \"attachments\": [\"…\"], …}]}``.
        """
        body = TestCasebulk(cases=chunk).to_dict()
        steps_type = str(self.config.get("migration.qase_case_steps_type", "classic") or "classic")
        inject_with_att = bool(
            self.config.get("migration.qase_bulk_inject_steps_type_with_attachments", True)
        )
        inject_always = bool(
            self.config.get("migration.qase_bulk_inject_steps_type_always", True)
        )
        n_with_att = 0
        for c in body.get("cases") or []:
            if not isinstance(c, dict):
                continue
            att = c.get("attachments")
            if isinstance(att, list):
                c["attachments"] = [str(x) for x in att if x is not None]
            if inject_always:
                c["steps_type"] = steps_type
            elif inject_with_att and isinstance(c.get("attachments"), list) and len(c["attachments"]) > 0:
                c["steps_type"] = steps_type
                n_with_att += 1
        if n_with_att:
            self.logger.log(
                f'[Qase][Cases] bulk body: injected steps_type={steps_type!r} on {n_with_att} case(s) '
                f'with non-empty attachments[]'
            )
        return body

    @staticmethod
    def _qase_http_body_absent_or_empty(data) -> bool:
        """True when RESTResponse.data is missing or empty (Qase client assumes bytes for JSON decode)."""
        if data is None:
            return True
        if isinstance(data, (bytes, bytearray)) and not data.strip():
            return True
        return False

    def _parse_bulk_case_response(self, code: str, chunk_num: int, n_chunks: int, response_data):
        """Deserialize POST /case/{code}/bulk. Handles empty 2xx bodies (urllib3 can leave data None)."""
        st = getattr(response_data, "status", None) or 0
        raw = getattr(response_data, "data", None)
        if 200 <= st <= 299 and self._qase_http_body_absent_or_empty(raw):
            # Documented shape: 200 + JSON with status and result.ids — see
            # https://developers.qase.io/reference/bulk
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}/{n_chunks} HTTP {st} with empty/missing body; '
                f'assuming bulk create succeeded (verify in Qase if needed)',
                'warn',
            )
            return Bulk200Response(status=True, result=None)
        return self.client.response_deserialize(
            response_data=response_data,
            response_types_map={
                "200": "Bulk200Response",
                "400": None,
                "401": None,
                "403": None,
                "404": None,
                "422": None,
                "429": None,
            },
        ).data

    @staticmethod
    def _bulk_chunk_title_suite(case_obj) -> tuple:
        """Title and suite id from a ``TestCasebulkCasesInner`` (or dict-like)."""
        d = case_obj.to_dict() if hasattr(case_obj, "to_dict") else {}
        title = (getattr(case_obj, "title", None) or d.get("title") or "").strip()
        sid = getattr(case_obj, "suite_id", None)
        if sid is None:
            sid = d.get("suite_id")
        return title, sid

    def _hydrate_bulk_case_ids(self, code: str, chunk: list) -> list:
        """Resolve Qase case ids via ``GET /case/{{code}}`` when bulk omits ``result.ids`` (empty body)."""
        if not bool(self.config.get("migration.qase_bulk_hydrate_case_ids", True)):
            return [None] * len(chunk)
        api = CasesApi(self.client)
        out: list = []
        for case_obj in chunk:
            title, suite_id = self._bulk_chunk_title_suite(case_obj)
            if not title or suite_id is None:
                self.logger.log(
                    f"[Qase][Cases] {code}: hydrate skipped row — title={title!r} suite_id={suite_id!r}",
                    "warn",
                )
                out.append(None)
                continue
            try:
                resp = self._retry_io(
                    f"Qase][Cases][hydrate][{code}]",
                    lambda: api.get_cases(
                        code=code,
                        suite_id=int(suite_id),
                        search=title,
                        limit=100,
                        offset=0,
                    ),
                )
            except ApiException as e:
                self.logger.log(
                    f"[Qase][Cases] {code}: hydrate GET /case failed: {e!s}"[:2000],
                    "warn",
                )
                out.append(None)
                continue
            if not resp or not getattr(resp, "status", False):
                out.append(None)
                continue
            result = getattr(resp, "result", None)
            entities = getattr(result, "entities", None) if result is not None else None
            if not isinstance(entities, list) or not entities:
                out.append(None)
                continue
            cid = None
            for ent in entities:
                et = (getattr(ent, "title", None) or "").strip()
                if et == title:
                    cid = getattr(ent, "id", None)
                    break
            if cid is None and len(entities) == 1:
                cid = getattr(entities[0], "id", None)
            try:
                out.append(int(cid) if cid is not None else None)
            except (TypeError, ValueError):
                out.append(None)
        n_ok = sum(1 for x in out if x is not None)
        if n_ok:
            self.logger.log(
                f"[Qase][Cases] {code}: hydrate resolved {n_ok}/{len(chunk)} case id(s) via GET /case "
                f"(search + suite_id)"
            )
        return out

    def create_cases(self, code: str, cases: list) -> tuple:
        """Bulk-create cases. Returns ``(success, ids)`` where ``ids`` are Qase case ids in request order."""
        chunk_size = 100
        if not cases:
            self.logger.log(f'[Qase][Cases] {code}: create_cases called with empty list; skipping', 'warn')
            return True, []
        total = len(cases)
        n_chunks = (total + chunk_size - 1) // chunk_size
        self.logger.log(
            f'[Qase][Cases] {code}: bulk create {total} case(s) in {n_chunks} chunk(s) of up to {chunk_size}'
        )
        all_ids: list = []
        for i in range(0, len(cases), chunk_size):
            chunk = cases[i : i + chunk_size]
            chunk_num = i // chunk_size + 1
            first_title = getattr(chunk[0], "title", None) or getattr(chunk[0], "_title", None)
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}/{n_chunks} offset={i} size={len(chunk)} '
                f'first_title={first_title!r}'
            )
            ok, merged = self._send_case_chunk(code, chunk, chunk_num, n_chunks)
            if ok:
                all_ids.extend(merged)
                continue
            # Bulk failed (HTTP error or status:false). Retry each case alone so one
            # bad case (e.g. an invalid custom-field value → 422) cannot drop the whole
            # chunk. We log each individual failure's payload so the cause is visible.
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}/{n_chunks} bulk failed; '
                f'retrying {len(chunk)} case(s) individually',
                'warn',
            )
            n_ok = 0
            for case_obj in chunk:
                cid = self._create_one_case(code, case_obj)
                all_ids.append(cid)
                if cid is not None:
                    n_ok += 1
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}/{n_chunks} individual fallback '
                f'created {n_ok}/{len(chunk)} case(s)',
                'info' if n_ok == len(chunk) else 'error',
            )
        created = sum(1 for x in all_ids if x is not None)
        self.logger.log(
            f'[Qase][Cases] {code}: all {n_chunks} chunk(s) done — {created}/{total} case(s) created'
        )
        return True, all_ids

    def _send_case_chunk(self, code: str, chunk: list, chunk_num: int, n_chunks: int) -> tuple:
        """POST one bulk chunk. Returns ``(ok, merged_ids)`` and never raises.

        On any failure (HTTP error or ``status:false``) it logs the response detail
        AND the request payload (id/title/custom_field per case) so the offending
        case/field is visible, then returns ``(False, [])`` for the caller to fall back.
        """
        try:
            body_dict = self._build_case_bulk_json_body(chunk)
            method, url, header_params, body, post_params = self.client.param_serialize(
                method="POST",
                resource_path="/case/{code}/bulk",
                path_params={"code": code},
                query_params=[],
                header_params={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=body_dict,
                post_params=[],
                files={},
                auth_settings=["TokenAuth"],
                collection_formats={},
                _host=None,
                _request_auth=None,
            )
            response_data = self._retry_io(
                f"Qase][Cases][{code}]",
                lambda: self.client.call_api(
                    method,
                    url,
                    header_params=header_params,
                    body=body,
                    post_params=post_params,
                    _request_timeout=None,
                ),
            )
            api_response = self._parse_bulk_case_response(
                code, chunk_num, n_chunks, response_data
            )
        except ApiException as e:
            self._log_bulk_failure(code, chunk_num, chunk, e)
            return False, []
        except Exception as e:
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num} unexpected error: {e!r}', 'error'
            )
            self.logger.log(traceback.format_exc(), 'error')
            self._log_chunk_payload(code, chunk_num, chunk)
            return False, []

        ok = bool(api_response.status)
        res = getattr(api_response, "result", None)
        if not ok:
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num} status:false — full response: {api_response!r}'[:4000],
                'error',
            )
            self._log_chunk_payload(code, chunk_num, chunk)
            return False, []

        bulk_ids: list = []
        if res is not None:
            raw_ids = getattr(res, "ids", None)
            if isinstance(raw_ids, list):
                bulk_ids = [int(x) for x in raw_ids if x is not None]
        if len(bulk_ids) == len(chunk):
            return True, bulk_ids

        if bulk_ids:
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}: bulk returned {len(bulk_ids)} id(s), '
                f'expected {len(chunk)} — hydrating via GET /case',
                'warn',
            )
        else:
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num}: bulk returned no id(s); '
                f'hydrating via GET /case (search + suite_id)',
                'warn',
            )
        hydrated = self._hydrate_bulk_case_ids(code, chunk)
        merged = []
        for j in range(len(chunk)):
            bid = bulk_ids[j] if j < len(bulk_ids) else None
            hid = hydrated[j] if j < len(hydrated) else None
            merged.append(bid if bid is not None else hid)
        return True, merged

    def _create_one_case(self, code: str, case_obj):
        """Per-case fallback: create a single case. Returns its Qase id, or None on failure.

        Safety net: a single custom-field value not accepted by Qase for this project
        (e.g. a per-project field not enabled on the target project → 422) would reject
        the whole case. If the first attempt fails and the case carries custom fields,
        retry once with them stripped so the case itself is never lost.
        """
        ok, ids = self._send_case_chunk(code, [case_obj], 1, 1)
        if ok and ids:
            return ids[0]
        cf = getattr(case_obj, "custom_field", None)
        if cf:
            title = getattr(case_obj, "title", None)
            try:
                case_obj.custom_field = {}
            except Exception:
                return None
            self.logger.log(
                f'[Qase][Cases] {code}: retrying case {title!r} without custom field(s) '
                f'{cf!r} after create failure',
                'warn',
            )
            ok, ids = self._send_case_chunk(code, [case_obj], 1, 1)
            if ok and ids:
                self.logger.log(
                    f'[Qase][Cases] {code}: case {title!r} created WITHOUT its custom field value(s) '
                    f'(Qase rejected them for this project — check field project scope)',
                    'warn',
                )
                return ids[0]
        return None

    def _log_bulk_failure(self, code: str, chunk_num: int, chunk: list, e) -> None:
        """Log full API error detail (status/reason/body/data) — Qase often omits body on 422."""
        self.logger.log(
            f'[Qase][Cases] {code}: chunk {chunk_num} ApiException — '
            f'status={getattr(e, "status", None)!r} '
            f'reason={getattr(e, "reason", None)!r} '
            f'body={getattr(e, "body", None)!s} '
            f'data={getattr(e, "data", None)!r}'[:8000],
            'error',
        )
        self._log_chunk_payload(code, chunk_num, chunk)

    def _log_chunk_payload(self, code: str, chunk_num: int, chunk: list) -> None:
        """Dump the request payload Qase rejected so the bad case/field is pinpointable."""
        try:
            body = self._build_case_bulk_json_body(chunk)
            for c in (body.get("cases") or []):
                if not isinstance(c, dict):
                    continue
                self.logger.log(
                    f'[Qase][Cases] {code}: chunk {chunk_num} payload — '
                    f'id={c.get("id")!r} title={c.get("title")!r} suite_id={c.get("suite_id")!r} '
                    f'status={c.get("status")!r} priority={c.get("priority")!r} '
                    f'custom_field={c.get("custom_field")!r}'[:2000],
                    'error',
                )
        except Exception as ex:
            self.logger.log(
                f'[Qase][Cases] {code}: chunk {chunk_num} payload dump failed: {ex!r}', 'warn'
            )

    def create_run(self, run: dict, project_code: str, cases: list = [], milestone_id = None):
        api_instance = RunsApi(self.client)

        def _fmt_ts(ts: int) -> str:
            """Qase expects comparable times; use UTC for both start and end (was mixed utc/local)."""
            return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")

        created = int(run["created_on"])
        completed = int(run["completed_on"])
        if completed < created:
            completed = created

        data = {
            "start_time": _fmt_ts(created),
            "author_id": run["author_id"],
        }

        if run['description']:
            data['description'] = run['description']

        if 'plan_name' in run and run['plan_name']:
            data['title'] = '['+run['plan_name']+'] '+run['name']
        else:
            data['title'] = run['name']

        if 'configurations' in run and run['configurations'] and len(run['configurations']) > 0:
            data['configurations'] = run['configurations']

        if run['is_completed']:
            data["end_time"] = _fmt_ts(completed)

        if milestone_id:
            data['milestone_id'] = milestone_id

        if len(cases) > 0:
            data['cases'] = cases

        try:
            response = api_instance.create_run(code=project_code, run_create=RunCreate(**data))
            return response.result.id
        except Exception as e:
            self.logger.log(f'Exception when calling RunsApi->create_run: {e}')

    def complete_run(self, project_code: str, run_id: int) -> bool:
        """POST ``/run/{code}/{id}/complete`` — Qase marks a run completed (separate from create-time flags)."""
        code = str(project_code).strip()
        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            self.logger.log(f'[Qase][Run] complete_run: invalid run_id {run_id!r}', 'error')
            return False
        try:
            method, url, header_params, body, post_params = self.client.param_serialize(
                method="POST",
                resource_path="/run/{code}/{id}/complete",
                path_params={"code": code, "id": rid},
                query_params=[],
                header_params={"Accept": "application/json"},
                body=None,
                post_params=[],
                files={},
                auth_settings=["TokenAuth"],
                collection_formats={},
                _host=None,
                _request_auth=None,
            )
            response_data = self._retry_io(
                f"Qase][Run][complete][{code}][{rid}]",
                lambda: self.client.call_api(
                    method,
                    url,
                    header_params=header_params,
                    body=body,
                    post_params=post_params,
                    _request_timeout=None,
                ),
            )
        except ApiException as e:
            self.logger.log(
                f'[Qase][Run] POST /run/{code}/{rid}/complete ApiException '
                f'status={getattr(e, "status", None)!r} body={getattr(e, "body", None)!s}'[:4000],
                'error',
            )
            return False
        except Exception as e:
            self.logger.log(f'[Qase][Run] complete_run: {e!r}', 'error')
            return False
        st = int(getattr(response_data, "status", 0) or 0)
        if not (200 <= st <= 299):
            self.logger.log(
                f'[Qase][Run] POST /run/{code}/{rid}/complete HTTP {st}',
                'error',
            )
            return False
        raw = getattr(response_data, "data", None)
        if raw:
            try:
                b = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw)
                payload = json.loads(b)
                if isinstance(payload, dict) and payload.get('status') is False:
                    return False
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                pass
        return True

    def send_bulk_results(
        self,
        tr_run,
        results,
        qase_run_id,
        qase_code,
        mappings,
        cases_map,
        result_statuses=None,
    ):
        """Send bulk results. If ``result_statuses`` is set, use it for Zephyr→Qase status slugs (thread-safe for parallel run uploads)."""
        sts = result_statuses if result_statuses is not None else (mappings.result_statuses or {})
        res = []

        if results:
            for result in results:
                # Drop rows that map to ``in_progress`` or ``untested`` — those leave the case
                # as Untested in the Qase run UI (no result row created) and let
                # ``_zephyr_execution_keeps_qase_run_open`` keep the run open.
                _raw_sid = str(result.get("status_id", "")).strip()
                _mapped = (sts or {}).get(_raw_sid)
                if _mapped in ("in_progress", "untested"):
                    continue
                if _mapped is None and _raw_sid == "3":
                    # Backwards-compat: if no mapping is present, "3" is the historical
                    # in-progress raw id and was always skipped.
                    continue
                elapsed = 0
                if 'elapsed' in result and result['elapsed']:
                    if type(result['elapsed']) is str:
                        elapsed = self.convert_to_seconds(result['elapsed'])
                    else:
                        elapsed = int(result['elapsed'])

                if 'created_on' in result and result['created_on']:
                    start_time = result['created_on'] - elapsed
                    if start_time < tr_run['created_on']:
                        start_time = tr_run['created_on']
                else:
                    start_time = tr_run['created_on']

                if result['test_id'] in cases_map:
                    status = 'skipped'
                    if ("status_id" in result
                        and result["status_id"] is not None
                            and result["status_id"] in sts
                        and sts[result["status_id"]]
                        ):
                        status = sts[result["status_id"]]
                    data = {
                        "case_id": cases_map[result['test_id']],
                        "status": status,
                        "time_ms": elapsed*1000,  # converting to milliseconds
                        "comment": str(result['comment'])
                    }

                    if 'attachments' in result and len(result['attachments']) > 0:
                        data['attachments'] = result['attachments']

                    if start_time:
                        data['start_time'] = start_time

                    #if (result['defects']):
                        #self.defects.append({"case_id": result["case_id"],"defects": result['defects'],"run_id": qase_run_id})

                    try:
                        z_uid = int(result.get("created_by") or 0)
                    except (TypeError, ValueError):
                        z_uid = 0
                    data["author_id"] = mappings.get_user_id(z_uid)

                    if 'custom_step_results' in result and result['custom_step_results']:
                        data['steps'] = self.prepare_result_steps(result['custom_step_results'], sts)

                    res.append(data)

            if len(res) > 0:
                api_results = ResultsApi(self.client)
                self.logger.log(f'Sending {len(res)} results to Qase')
                total_chunks = -(-len(res) // self._RESULT_BULK_CHUNK)
                for start in range(0, len(res), self._RESULT_BULK_CHUNK):
                    chunk = res[start:start + self._RESULT_BULK_CHUNK]
                    if total_chunks > 1:
                        self.logger.log(
                            f'  chunk {start // self._RESULT_BULK_CHUNK + 1}/{total_chunks}'
                            f' ({len(chunk)} result(s))'
                        )
                    api_results.create_result_bulk(
                        code=qase_code,
                        id=int(qase_run_id),
                        resultcreate_bulk=ResultcreateBulk(
                            results=chunk
                        )
                    )

    def prepare_result_steps(self, steps, status_map) -> list:
        allowed_statuses = ['passed', 'failed', 'blocked', 'skipped']
        data = []
        try:
            for step in steps:
                status = status_map.get(str(step.get('status_id')), 'skipped')

                step_data = {
                    "status": status if status in allowed_statuses else 'skipped',
                }

                if 'actual' in step and step['actual'] is not None:
                    comment = step['actual'].strip()
                    if comment != '':
                        step_data['comment'] = comment

                data.append(step_data)
        except Exception as e:
            self.logger.log(f'Exception when preparing result steps: {e}', 'error')

        return data

    def convert_to_seconds(self, time_str: str) -> int:
        total_seconds = 0

        try:
            components = time_str.split()
            for component in components:
                if component.endswith('d'):
                    total_seconds += int(component[:-1]) * 86400  # 60 seconds * 60 minutes * 24 hours
                elif component.endswith('h'):
                    total_seconds += int(component[:-1]) * 3600  # 60 seconds * 60 minutes
                elif component.endswith('m'):
                    total_seconds += int(component[:-1]) * 60
                elif component.endswith('s'):
                    total_seconds += int(component[:-1])
        except Exception as e:
            self.logger.log(f'Exception when converting time string: {e}', 'warn')

        return total_seconds

    def upload_attachment(self, code, attachment_data):
        """Upload to Qase. ``attachment_data`` is a filesystem path (str), or ``(filename, bytes)``.

        The qaseio client accepts path strings or ``io.BytesIO`` with ``.name`` and ``.mime`` set; tuple input is wrapped that way.
        """
        api_attachments = AttachmentsApi(self.client)
        try:
            file_payload = attachment_data
            if isinstance(attachment_data, (tuple, list)) and len(attachment_data) == 2:
                fname, raw = attachment_data[0], attachment_data[1]
                if isinstance(raw, bytearray):
                    raw = bytes(raw)
                elif not isinstance(raw, bytes):
                    raw = bytes(raw)
                buf = io.BytesIO(raw)
                buf.name = os.path.basename(str(fname)) or "attachment"
                buf.mime = mimetypes.guess_type(str(fname))[0] or "application/octet-stream"
                file_payload = buf
            elif isinstance(attachment_data, io.BytesIO):
                file_payload = attachment_data
            response = api_attachments.upload_attachment(code, file=[file_payload])

            if response.status and response.result:
                return response.result[0].to_dict()
        except Exception as e:
            self.logger.log(f'Exception when calling AttachmentsApi->upload_attachment: {e}')
        return None

    def create_milestone(self, project_code, title, description, status, due_date):
        data = {
            'project_code': project_code,
            'title': title
        }

        if description:
            data['description'] = description

        if due_date:
            data['due_date'] = due_date

        api_instance = MilestonesApi(self.client)
        api_response = api_instance.create_milestone(
            code=project_code,
            milestone_create=MilestoneCreate(**data)
        )
        return api_response.result.id

    def create_shared_step(self, project_code, title, steps):
        inner_steps = []

        for step in steps:
            action = step['content'].strip() if 'content' in step and type(step['content']) is str else 'No action'

            if action == '':
                action = 'No action'
            inner_steps.append(
                SharedStepContentCreate(
                    action=action,
                    expected_result=step['expected']
                )
            )

        api_instance = SharedStepsApi(self.client)
        api_response = api_instance.create_shared_step(project_code, SharedStepCreate(title=title, steps=inner_steps))
        return api_response.result.hash