"""
signavio_client.py — Read-only Signavio PI client for the backup pipeline.

Extracted and slimmed from SignavioWorkspaceDownloader/signavio_client.py.
Contains only what is needed for backup: auth, GraphQL execution, process
listing, dashboard listing, and dashboard export.
"""

import copy
import json
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# GraphQL template cache — loaded once at import time
# ---------------------------------------------------------------------------

_GRAPHQL_DIR = Path(__file__).parent / "graphql"

_GRAPHQL_CACHE: dict[str, dict] = {
    p.name: json.loads(p.read_text(encoding="utf-8"))
    for p in _GRAPHQL_DIR.glob("*.json")
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGIONS = {
    "eu":  "https://editor.signavio.com",
    "us":  "https://app-us.signavio.com",
    "au":  "https://app-au.signavio.com",
    "ca":  "https://app-ca.signavio.com",
    "jp":  "https://app-jp.signavio.com",
    "sgp": "https://app-sgp.signavio.com",
    "kr":  "https://app-kr.signavio.com",
}

HOST_TO_REGION = {v: k for k, v in REGIONS.items()}

_REGEX_LOGIN_WORKSPACES = r'<label for="(?P<id>[a-z0-9]*)">(?P<name>[^<>]*)</label>'
_REGEX_LOGIN_SUCCESS    = r'^[0-9a-z]+$'

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SignavioAuthError(Exception):
    pass

class SignavioAPIError(Exception):
    pass

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SignavioClient:
    """Read-only Signavio PI client for the backup pipeline."""

    def __init__(self, host: str, email: str, password: str):
        self.host        = host.rstrip("/")
        self.email       = email
        self._password   = password
        self.token:        str | None = None
        self.jsessionid:   str | None = None
        self.workspace_id: str | None = None
        self._session    = requests.Session()

    # ------------------------------------------------------------------
    # Authentication — identical two-step flow as the downloader app
    # ------------------------------------------------------------------

    def get_workspaces(self) -> list[dict]:
        """Step 1: POST login without tenant. Returns [{id, name}].

        If the account has only one workspace the API logs in directly and
        returns the token as plain text; in that case returns [].
        """
        resp = self._session.post(
            url=f"{self.host}/p/login",
            data={"name": self.email, "password": self._password, "tokenonly": "true"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise SignavioAuthError(f"Login step 1 failed ({resp.status_code})")

        body = resp.text.strip()

        # Single-workspace fast path: API returns token directly.
        if re.match(_REGEX_LOGIN_SUCCESS, body):
            self.token = body
            self.jsessionid = (
                resp.cookies.get("JSESSIONID")
                or self._session.cookies.get("JSESSIONID")
            )
            self._session.headers.update({"x-signavio-id": self.token})
            if self.jsessionid:
                self._session.cookies.set("JSESSIONID", self.jsessionid)
            return []

        # Multi-workspace: parse the HTML workspace selector.
        matches = list(re.finditer(_REGEX_LOGIN_WORKSPACES, body))
        if not matches:
            raise SignavioAuthError(
                "Invalid credentials or unexpected login response from Signavio."
            )
        return [{"id": m.group("id"), "name": m.group("name").strip()} for m in matches]

    def login(self, workspace_id: str) -> None:
        """Step 2: POST login with tenant. Sets token + session cookies.

        Must be called on the same instance as get_workspaces() so that
        the HTTP session cookies from step 1 are carried over.
        """
        resp = self._session.post(
            url=f"{self.host}/p/login",
            data={
                "name":      self.email,
                "password":  self._password,
                "tokenonly": "true",
                "tenant":    workspace_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise SignavioAuthError(f"Login step 2 failed ({resp.status_code})")

        body = resp.text.strip()
        if not re.match(_REGEX_LOGIN_SUCCESS, body):
            raise SignavioAuthError(f"Unexpected login response: {body[:200]}")

        self.token        = body
        self.jsessionid   = resp.cookies.get("JSESSIONID")
        self.workspace_id = workspace_id

        self._session.headers.update({"x-signavio-id": self.token})
        if self.jsessionid:
            self._session.cookies.set("JSESSIONID", self.jsessionid)

    def authenticate(self, workspace_id: str) -> None:
        """Full two-step auth. Convenience wrapper for use in scripts."""
        workspaces = self.get_workspaces()

        if workspaces:
            # Verify the requested workspace_id is in the returned list.
            ids = [w["id"] for w in workspaces]
            if workspace_id not in ids:
                raise SignavioAuthError(
                    f"Workspace '{workspace_id}' not found. "
                    f"Available: {ids}"
                )
            self.login(workspace_id)
        else:
            # Single-workspace fast path: get_workspaces() already set the token.
            # workspace_id must still be recorded so API calls have the tenant context.
            self.workspace_id = workspace_id

    # ------------------------------------------------------------------
    # GraphQL execution
    # ------------------------------------------------------------------

    def _load_graphql(self, filename: str) -> dict:
        return copy.deepcopy(_GRAPHQL_CACHE[filename])

    def _execute_graphql(self, body: dict, max_tries: int = 3) -> dict:
        """POST a GraphQL query with exponential-backoff retry."""
        url       = f"{self.host}/g/api/pi-graphql/graphql"
        operation = body.get("operationName", "unknown")
        last_exc  = None

        for attempt in range(max_tries):
            try:
                resp = self._session.post(url, json=body, timeout=60)
                if not resp.ok:
                    raise SignavioAPIError(
                        f"{operation}: HTTP {resp.status_code} {resp.reason}"
                    )
                result = resp.json()
                if "errors" in result:
                    raise SignavioAPIError(
                        f"{operation}: GraphQL errors: {result['errors']}"
                    )
                return result
            except (requests.RequestException, SignavioAPIError) as exc:
                last_exc = exc
                if attempt < max_tries - 1:
                    wait = 3 * (2 ** attempt)   # 3 s, 6 s, 12 s
                    print(
                        f"[WARN] {operation} attempt {attempt + 1} failed "
                        f"({exc}); retrying in {wait}s…"
                    )
                    time.sleep(wait)

        raise SignavioAPIError(
            f"GraphQL request failed after {max_tries} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Read-only data retrieval
    # ------------------------------------------------------------------

    def get_all_processes(self) -> list[dict]:
        """Return [{id, name, dashboardCount}] for every process in the workspace."""
        body   = self._load_graphql("get_processes.json")
        result = self._execute_graphql(body)
        return [
            {
                "id":             s["id"],
                "name":           s["name"],
                "dashboardCount": s.get("dashboardCount", 0),
            }
            for s in result["data"]["subjects"]
        ]

    def get_dashboards(self, process_id: str) -> list[dict]:
        """Return [{id, name, version, owner}] for every dashboard of a process."""
        body = self._load_graphql("get_dashboards.json")
        body["variables"]["subjectId"] = process_id
        result = self._execute_graphql(body)
        return [
            {
                "id":      d["id"],
                "name":    d["name"],
                "version": d.get("version"),  # hash that changes on every save
                "owner":   d.get("owner"),    # {id, firstName, lastName} or None
            }
            for d in result.get("data", {}).get("dashboards", [])
        ]

    def export_dashboard(self, dashboard_id: str) -> dict:
        """Fetch the full JSON definition of a dashboard via the REST export endpoint.

        Returns the parsed JSON dict. Raises SignavioAPIError on failure.
        """
        url  = f"{self.host}/g/api/pi-graphql/dashboards/{dashboard_id}/export"
        resp = self._session.get(url, timeout=60)
        if not resp.ok:
            raise SignavioAPIError(
                f"Dashboard export failed for id={dashboard_id}: "
                f"HTTP {resp.status_code} {resp.reason}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def owner_label(owner: dict | None) -> str:
        """Format an owner dict as 'First Last' for commit messages."""
        if not owner:
            return "Unknown"
        first = (owner.get("firstName") or "").strip()
        last  = (owner.get("lastName")  or "").strip()
        name  = " ".join(p for p in (first, last) if p)
        return name or "Unknown"
