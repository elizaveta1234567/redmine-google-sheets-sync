from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================================
# CONFIG
# ============================================================================
# Example: "https://redmine.company.com"
# If Redmine lives in a subfolder, include it:
# "https://company.com/redmine"
REDMINE_URL = "https://redmine.justmoby.com/"

# Recommended authentication method.
# You can find the API key in Redmine: My account -> API access key.
REDMINE_API_KEY = os.getenv("REDMINE_API_KEY", "")

# Optional fallback: login/password instead of API key.
# Leave empty when REDMINE_API_KEY is used.
REDMINE_USERNAME = ""
REDMINE_PASSWORD = ""

# Set False only if your internal Redmine uses a self-signed certificate
# and you understand the security implications.
VERIFY_SSL = True

DEFAULT_TIMEOUT_SECONDS = 30


class RedmineAPIError(RuntimeError):
    """Raised when Redmine returns an HTTP/API error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass
class RedmineClient:
    base_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("base_url must not be empty")

        if not self.api_key and not self.username:
            raise ValueError("Provide api_key or username/password")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "redmine-api-client/1.0",
                # Always ask for fresh issue data during scheduled synchronization.
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            }
        )

        if self.api_key:
            self.session.headers["X-Redmine-API-Key"] = self.api_key
        else:
            self.session.auth = (self.username, self.password)

        # Retry temporary server/network errors. Do not retry POST by default,
        # because repeating a POST can create duplicates.
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}),
            respect_retry_after_header=True,
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _url(self, endpoint: str) -> str:
        """Build URL without losing a possible Redmine subpath."""
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        try:
            response = self.session.request(
                method=method,
                url=self._url(endpoint),
                params=dict(params or {}),
                json=json,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise RedmineAPIError(f"Network error while calling Redmine: {exc}") from exc

        if not response.ok:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text

            errors = body.get("errors") if isinstance(body, dict) else None
            details = f": {errors}" if errors else ""
            raise RedmineAPIError(
                f"Redmine API returned HTTP {response.status_code}{details}",
                status_code=response.status_code,
                response_body=body,
            )

        # Successful DELETE/PUT requests often return 204 No Content.
        if response.status_code == 204 or not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise RedmineAPIError(
                "Redmine returned a non-JSON response",
                status_code=response.status_code,
                response_body=response.text,
            ) from exc

    # ----------------------------------------------------------------------
    # Generic REST methods. These are the foundation for future endpoints.
    # ----------------------------------------------------------------------
    def get(self, endpoint: str, **params: Any) -> Any:
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
        return self._request("POST", endpoint, json=payload)

    def put(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
        return self._request("PUT", endpoint, json=payload)

    def delete(self, endpoint: str) -> None:
        self._request("DELETE", endpoint)

    # ----------------------------------------------------------------------
    # Pagination helpers
    # ----------------------------------------------------------------------
    def iter_all(
        self,
        endpoint: str,
        root_key: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        page_size: int = 100,
    ) -> Iterator[Dict[str, Any]]:
        """Yield every object from a paginated Redmine collection."""
        if not 1 <= page_size <= 100:
            raise ValueError("Redmine page_size must be between 1 and 100")

        query: Dict[str, Any] = dict(params or {})
        offset = int(query.pop("offset", 0))
        query.pop("limit", None)

        while True:
            page_params = {**query, "offset": offset, "limit": page_size}
            data = self._request("GET", endpoint, params=page_params)

            if not isinstance(data, dict):
                raise RedmineAPIError(f"Unexpected response for {endpoint}: expected object")

            items = data.get(root_key, [])
            if not isinstance(items, list):
                raise RedmineAPIError(
                    f"Unexpected response for {endpoint}: '{root_key}' is not a list"
                )

            for item in items:
                yield item

            received = len(items)
            total_count = data.get("total_count")

            if received == 0:
                break

            offset += received

            if isinstance(total_count, int) and offset >= total_count:
                break

            # Fallback for endpoints/versions that don't return total_count.
            if total_count is None and received < page_size:
                break

    def get_all(
        self,
        endpoint: str,
        root_key: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        return list(
            self.iter_all(
                endpoint,
                root_key,
                params=params,
                page_size=page_size,
            )
        )

    # ----------------------------------------------------------------------
    # Ready-to-use Redmine methods
    # ----------------------------------------------------------------------
    def get_current_user(self) -> Dict[str, Any]:
        data = self.get("users/current.json")
        return data["user"]

    def get_projects(self, **filters: Any) -> List[Dict[str, Any]]:
        return self.get_all("projects.json", "projects", params=filters)

    def get_project(self, project_id_or_identifier: int | str, **params: Any) -> Dict[str, Any]:
        data = self.get(f"projects/{project_id_or_identifier}.json", **params)
        return data["project"]

    def get_issues(
        self,
        *,
        project_id: Optional[int] = None,
        include_closed: bool = False,
        **filters: Any,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = dict(filters)

        if project_id is not None:
            params["project_id"] = project_id

        # Redmine /issues returns open issues by default.
        if include_closed and "status_id" not in params:
            params["status_id"] = "*"

        return self.get_all("issues.json", "issues", params=params)

    def get_issue(self, issue_id: int, *, include: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if include:
            params["include"] = include
        data = self.get(f"issues/{issue_id}.json", **params)
        return data["issue"]

    def get_time_entries(
        self,
        *,
        project_id: Optional[int | str] = None,
        user_id: Optional[int | str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        **filters: Any,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = dict(filters)

        if project_id is not None:
            params["project_id"] = project_id
        if user_id is not None:
            params["user_id"] = user_id
        if date_from is not None:
            params["from"] = date_from
        if date_to is not None:
            params["to"] = date_to

        return self.get_all("time_entries.json", "time_entries", params=params)

    def get_time_entry(self, time_entry_id: int) -> Dict[str, Any]:
        data = self.get(f"time_entries/{time_entry_id}.json")
        return data["time_entry"]

    def get_project_versions(self, project_id: int | str) -> List[Dict[str, Any]]:
        """Get all versions for a project."""
        data = self.get(f"projects/{project_id}/versions.json")
        return data.get("versions", []) if isinstance(data, dict) else []
