import base64
import re
import time
import http.client

import requests

from ..exceptions.api import APIError


def _normalize_zephyr_base_url(base_url: str) -> str:
    """Strip UI paths so REST URLs are built correctly.

    Config must resolve to ``{origin}/flex/services/rest/latest/...``.
    Values like ``https://host/flex/html5/`` would wrongly become
    ``.../flex/html5/flex/services/...`` — remove ``/flex/html5`` suffix.
    """
    u = (base_url or "").strip().rstrip("/")
    lower = u.lower()
    for suffix in ("/flex/html5/index.html", "/flex/html5/", "/flex/html5"):
        if lower.endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
            lower = u.lower()
            break
    return u


# UI-aligned case-tab files; repository also merges legacy ``latest/attachment/list`` URLs.
_V3_ATTACHMENT_PATH = "flex/services/rest/v3/attachment"


class ZephyrEnterpriseApiClient:
    def __init__(
        self,
        base_url,
        token,
        logger,
        max_retries=7,
        backoff_factor=5,
        connect_timeout=30,
        read_timeout=120,
        attachment_list_read_timeout=45,
        attachment_list_max_retries=2,
        attachment_list_backoff_factor=1.0,
        auth_type: str = "bearer",
        basic_username: str = None,
        basic_password: str = None,
    ):
        base_url = _normalize_zephyr_base_url(base_url)
        if not base_url.endswith("/"):
            base_url += "/"
        self.__url = base_url + "flex/services/rest/latest/"
        self.logger = logger
        self.base_url = base_url

        auth = (auth_type or "bearer").strip().lower()
        if auth == "basic" and basic_username is not None and basic_password is not None:
            blob = base64.b64encode(
                f"{basic_username}:{basic_password}".encode("utf-8")
            ).decode("ascii")
            self.headers = {
                "Authorization": f"Basic {blob}",
                "Content-Type": "application/json",
            }
        else:
            tok = token or ""
            self.headers = {
                "Authorization": "Bearer " + tok,
                "Content-Type": "application/json",
            }
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.page_size = 30
        # (connect, read) — without this, a silent server blocks the migrator indefinitely.
        self.request_timeout = (float(connect_timeout), float(read_timeout))
        # Small JSON only — tighter than ``read_timeout`` so a hung ``attachment/list`` does not
        # block the migrator for minutes (see README.md, Case attachments).
        self.attachment_list_read_timeout = float(attachment_list_read_timeout)
        self.attachment_list_max_retries = int(attachment_list_max_retries)
        self.attachment_list_backoff_factor = float(attachment_list_backoff_factor)
        self.attachment_v3_path = _V3_ATTACHMENT_PATH.strip("/")

    def fetch_v3_attachment_list(self, item_id: int, item_type: str = "testcase"):
        """GET ``/flex/services/rest/v3/attachment?itemid=&type=`` — returns a bare JSON array.

        Zephyr UI uses this (not ``.../latest/attachment/list``) for case-tab files; rows use
        ``refId`` as the flex ``fileId`` for ``/flex/download``.

        ``type`` is case-sensitive on Zephyr Enterprise: ``testcase`` for case-tab files,
        ``releaseTestSchedule`` (camelCase) for execution result files. Pass-through preserved.
        """
        it = (str(item_type or "testcase").strip()) or "testcase"
        path = f"{self.attachment_v3_path}?itemid={int(item_id)}&type={it}"
        full_url = self.base_url.rstrip("/") + "/" + path
        timeout = (self.request_timeout[0], self.attachment_list_read_timeout)
        mr = self.attachment_list_max_retries
        bf = self.attachment_list_backoff_factor
        for attempt in range(mr + 1):
            try:
                response = requests.get(
                    full_url,
                    headers=self.headers,
                    timeout=timeout,
                )
                if response.status_code in (403, 404):
                    raise APIError(
                        f"v3 attachment HTTP {response.status_code} for {path!r}"
                    )
                if response.status_code == 400:
                    raise APIError(f"v3 attachment HTTP 400 for {path!r}")
                if response.status_code != 429 and response.status_code <= 201:
                    if not (response.content or b"").strip():
                        return []
                    try:
                        data = response.json()
                    except (ValueError, requests.exceptions.JSONDecodeError) as e:
                        raise APIError(f"Non-JSON v3 attachment response for {path!r}") from e
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        for key in ("attachments", "data", "results", "items"):
                            v = data.get(key)
                            if isinstance(v, list):
                                return v
                    return []
                time.sleep(bf * (2 ** attempt))
            except APIError:
                raise
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(bf * (2 ** attempt))
            if attempt == mr:
                raise APIError(f"Max retries v3 attachment {path!r}")

    def get(self, uri, *, read_timeout=None, max_retries=None, backoff_factor=None):
        timeout = self.request_timeout
        if read_timeout is not None:
            timeout = (self.request_timeout[0], float(read_timeout))
        mr = self.max_retries if max_retries is None else int(max_retries)
        return self.send_request(
            requests.get,
            uri,
            payload=None,
            timeout=timeout,
            max_retries=mr,
            backoff_factor=backoff_factor,
        )

    def get_server_path(self, relative_path: str, *, read_timeout=None, max_retries=None, backoff_factor=None):
        """GET JSON from a path under the server origin (e.g. ``flex/services/rest/v3/...``).

        Unlike :meth:`get`, this is not prefixed with ``flex/services/rest/latest/``.
        Used for v3 endpoints such as ``externalGroup/search`` (see SmartBear docs).
        """
        path = (relative_path or "").lstrip("/")
        uri = path
        full_url = self.base_url.rstrip("/") + "/" + path
        timeout = self.request_timeout
        if read_timeout is not None:
            timeout = (self.request_timeout[0], float(read_timeout))
        mr = self.max_retries if max_retries is None else int(max_retries)
        bf = self.backoff_factor if backoff_factor is None else float(backoff_factor)
        for attempt in range(mr + 1):
            try:
                response = requests.get(
                    full_url,
                    headers=self.headers,
                    timeout=timeout,
                )
                if response.status_code != 429 and response.status_code <= 201:
                    return self.process_response(response, uri)
                if response.status_code == 403:
                    raise APIError("Access denied.")
                # Deterministic client errors (any 4xx except 408 Request Timeout
                # and 429 Rate Limit) won't change on retry — fail fast rather than
                # burn ``backoff * 2**attempt`` sleeps. (405 on a legacy endpoint
                # variant stalled the runs phase ~5 min/release.)
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(f"Client error HTTP {response.status_code} (non-retryable) for {uri!r}")
                time.sleep(bf * (2 ** attempt))
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(bf * (2 ** attempt))
            if attempt == mr:
                raise APIError("Max retries reached or server error.")

    def send_request(
        self,
        request_method,
        uri,
        payload=None,
        *,
        timeout=None,
        max_retries=None,
        backoff_factor=None,
    ):
        url = self.__url + uri
        req_timeout = self.request_timeout if timeout is None else timeout
        mr = self.max_retries if max_retries is None else int(max_retries)
        bf = self.backoff_factor if backoff_factor is None else float(backoff_factor)
        for attempt in range(mr + 1):
            try:
                response = request_method(
                    url,
                    headers=self.headers,
                    data=payload,
                    timeout=req_timeout,
                )
                if response.status_code != 429 and response.status_code <= 201:
                    return self.process_response(response, uri)
                if response.status_code == 403:
                    raise APIError('Access denied.')
                # Deterministic client errors (any 4xx except 408 Request Timeout
                # and 429 Rate Limit) won't change on retry — fail fast instead of
                # burning ``backoff * 2**attempt`` sleeps. Covers 400/404 (legacy
                # ``attachment/list``) AND 405 (legacy ``cycle?projectId=…`` variant
                # that stalled the runs phase ~5 min/release).
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    raise APIError(f'Client error HTTP {response.status_code} (non-retryable) for {uri!r}')
                else:
                    time.sleep(bf * (2 ** attempt))
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(bf * (2 ** attempt))
            
            if attempt == mr:
                raise APIError('Max retries reached or server error.')

    def process_response(self, response, uri):
        if not (response.content or b"").strip():
            raise APIError(
                f"Empty body from {uri!r} (HTTP {response.status_code}). "
                "Check zephyr.host: use the server root only "
                "(e.g. https://zephyr.example.com), not the /flex/html5 UI URL."
            )
        try:
            return response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as e:
            snippet = (response.text or "")[:400].replace("\n", " ")
            ct = response.headers.get("Content-Type", "?")
            raise APIError(
                f"Non-JSON from {uri!r} (HTTP {response.status_code}, {ct}). "
                f"Body starts with: {snippet!r}. "
                "Use host = scheme + host with no /flex/html5 path; "
                "confirm the API token and that the server is reachable."
            ) from e

    def download_flex_asset(self, relative_path: str) -> tuple[bytes, str]:
        """Download binary from Zephyr origin (not under ``.../rest/latest/``).

        Typical path: ``flex/download?action=download&fileId=<uuid>`` (same host as API).
        Returns ``(body, suggested_filename)``.
        """
        url = self.base_url.rstrip("/") + "/" + relative_path.lstrip("/")
        headers = {"Authorization": self.headers["Authorization"]}
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(url, headers=headers, timeout=self.request_timeout)
                if response.status_code == 200:
                    if not response.content:
                        time.sleep(self.backoff_factor * (2 ** attempt))
                        continue
                    name = "attachment.bin"
                    cd = response.headers.get("Content-Disposition") or ""
                    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
                    if m:
                        name = m.group(1).strip()
                    elif "filename=" in cd:
                        name = cd.split("filename=", 1)[-1].strip().strip('"')[:255]
                    ct = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                    if "json" in ct.lower() and response.content[:1] in (b"{", b"["):
                        raise APIError(f"download returned JSON not binary from {relative_path!r}")
                    return response.content, name or "attachment.bin"
                if response.status_code in (400, 403, 404):
                    raise APIError(
                        f"download HTTP {response.status_code} for {relative_path!r}"
                    )
                # A 500 on a binary asset GET means the file handler choked on a
                # missing/corrupt asset — deterministic, won't recover on retry.
                # Fast-fail so one broken attachment can't burn max_retries*backoff
                # (~155s) of sleeps and stall the whole migration. Genuinely
                # transient gateway errors (502/503/504) still retry below.
                if response.status_code == 500:
                    raise APIError(
                        f"download HTTP 500 (asset unavailable) for {relative_path!r}"
                    )
                time.sleep(self.backoff_factor * (2 ** attempt))
            except (
                requests.exceptions.Timeout,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                requests.exceptions.ConnectionError,
            ):
                time.sleep(self.backoff_factor * (2 ** attempt))

            if attempt == self.max_retries:
                raise APIError(f"Max retries downloading {relative_path!r}")