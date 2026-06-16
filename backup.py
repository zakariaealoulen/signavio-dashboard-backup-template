"""
backup.py — GitOps-style backup of SAP Signavio Process Intelligence dashboards.

Real API data flow (two-level hierarchy):
    Workspace
    └── Process  (GraphQL: subjects[])
        └── Dashboard  (GraphQL: dashboards[subjectId])
            └── Definition JSON  (REST: GET /g/api/pi-graphql/dashboards/{id}/export)

Output structure:
    dashboards/
    └── <ProcessName>__<ProcessID>/
        ├── _index.json                      ← version cache (not a dashboard)
        └── db_<ID>__<DashboardName>.json

Version-based optimization:
    The GraphQL listing returns a `version` hash per dashboard that changes on
    every save. The script caches these hashes in _index.json. On each run it
    compares the live version against the cache — only dashboards whose version
    changed (or are new) trigger the expensive REST export call.

Environment variables required:
    SIGNAVIO_HOST          — Regional base URL, e.g. https://editor.signavio.com
    SIGNAVIO_EMAIL         — Service-account email address
    SIGNAVIO_PASSWORD      — Service-account password  (stored as a GitHub secret)
    SIGNAVIO_WORKSPACE_ID  — Workspace / tenant ID to back up
"""

import json
import os
import re
import sys
from pathlib import Path

from signavio_client import SignavioAuthError, SignavioAPIError, SignavioClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DASHBOARDS_DIR   = Path("dashboards")
COMMIT_MSG_FILE  = Path("commit_message.txt")
INDEX_FILENAME   = "_index.json"

# ---------------------------------------------------------------------------
# Name sanitization
# ---------------------------------------------------------------------------

def _sanitize(name: str, max_len: int = 60) -> str:
    """Convert a display name to a safe, readable folder/file segment.

    Keeps letters, digits, hyphens. Collapses everything else to underscores.
    Strips leading/trailing underscores. Truncates to max_len characters.
    Examples:
        "Sales & Operations 2024"  → "Sales_Operations_2024"
        "KPIs — Q1/Q2"             → "KPIs_Q1_Q2"
    """
    safe = re.sub(r"[^\w-]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:max_len] or "unnamed"

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def process_folder(process_name: str, process_id: str) -> Path:
    """Return the folder path for a process: dashboards/<SanitizedProcessName>__<ProcessID>/

    The process ID suffix guarantees uniqueness when two processes share the same display name.
    """
    return DASHBOARDS_DIR / f"{_sanitize(process_name)}__{process_id}"


def dashboard_path(process_name: str, process_id: str, dashboard_id: str, dashboard_name: str) -> Path:
    """Return the full file path for a dashboard.

    Format: dashboards/<ProcessName>__<ProcessID>/db_<ID>__<DashboardName>.json
    """
    folder   = process_folder(process_name, process_id)
    filename = f"db_{dashboard_id}__{_sanitize(dashboard_name)}.json"
    return folder / filename


def find_existing_file(process_name: str, process_id: str, dashboard_id: str) -> Path | None:
    """Locate an existing file for this dashboard ID in the process folder.

    Handles renames: finds db_<id>__*.json regardless of the current name suffix.
    Returns None if no file exists yet for this ID.
    """
    folder = process_folder(process_name, process_id)
    if not folder.exists():
        return None
    matches = list(folder.glob(f"db_{dashboard_id}__*.json"))
    return matches[0] if matches else None


def load_existing(path: Path) -> dict | None:
    """Return the parsed JSON already on disk, or None if absent / unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_dashboard(path: Path, definition: dict) -> None:
    """Write a dashboard definition as pretty-printed UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(definition, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

# ---------------------------------------------------------------------------
# Version index  (_index.json per process folder)
# ---------------------------------------------------------------------------

def index_path(process_name: str, process_id: str) -> Path:
    return process_folder(process_name, process_id) / INDEX_FILENAME


def load_index(process_name: str, process_id: str) -> dict:
    """Load the cached version index for a process folder. Returns {} if missing."""
    path = index_path(process_name, process_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_index(process_name: str, process_id: str, index: dict) -> None:
    """Persist the version index for a process folder."""
    path = index_path(process_name, process_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

# ---------------------------------------------------------------------------
# Commit message
# ---------------------------------------------------------------------------

def write_commit_message(updated: list[str], skipped: int, skipped_export: int, total: int) -> None:
    """Write commit_message.txt summarising this backup cycle."""
    if not updated:
        subject = "chore: backup cycle — no dashboard changes detected"
        body    = (
            f"Checked {total} dashboard(s) across all processes; "
            f"{skipped} unchanged (version match), "
            f"{skipped_export} skipped (export error)."
        )
    else:
        noun    = "dashboard" if len(updated) == 1 else "dashboards"
        subject = f"chore: backup {len(updated)} {noun} ({total} total checked)"
        body    = "\n".join(updated)
        body   += (
            f"\n\n{skipped} dashboard(s) unchanged (version match), "
            f"{skipped_export} skipped (export error)."
        )

    COMMIT_MSG_FILE.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Core backup logic
# ---------------------------------------------------------------------------

def run_backup() -> None:
    # ---- Validate env vars ------------------------------------------------
    required = {
        "SIGNAVIO_HOST":         os.environ.get("SIGNAVIO_HOST"),
        "SIGNAVIO_EMAIL":        os.environ.get("SIGNAVIO_EMAIL"),
        "SIGNAVIO_PASSWORD":     os.environ.get("SIGNAVIO_PASSWORD"),
        "SIGNAVIO_WORKSPACE_ID": os.environ.get("SIGNAVIO_WORKSPACE_ID"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"[ERROR] Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    DASHBOARDS_DIR.mkdir(exist_ok=True)

    # ---- Authenticate -----------------------------------------------------
    client = SignavioClient(
        host     = required["SIGNAVIO_HOST"],
        email    = required["SIGNAVIO_EMAIL"],
        password = required["SIGNAVIO_PASSWORD"],
    )
    print(f"[INFO] Authenticating as {required['SIGNAVIO_EMAIL']} …")
    try:
        client.authenticate(required["SIGNAVIO_WORKSPACE_ID"])
    except SignavioAuthError as exc:
        print(f"[ERROR] Authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Authenticated. Workspace: {client.workspace_id}")

    # ---- Iterate processes → dashboards -----------------------------------
    print("[INFO] Fetching process list …")
    try:
        processes = client.get_all_processes()
    except SignavioAPIError as exc:
        print(f"[ERROR] Could not fetch processes: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Found {len(processes)} process(es).")

    updated:        list[str] = []
    skipped         = 0   # version matched — no export needed
    skipped_export  = 0   # export call failed

    total = 0

    for process in processes:
        proc_id   = process["id"]
        proc_name = process["name"]

        print(f"[INFO] Process '{proc_name}' — fetching dashboards …")
        try:
            dashboards = client.get_dashboards(proc_id)
        except SignavioAPIError as exc:
            print(f"[WARN] Could not fetch dashboards for '{proc_name}': {exc}", file=sys.stderr)
            continue

        # Load the cached version index for this process folder once.
        version_index  = load_index(proc_name, proc_id)
        index_dirty    = False   # version hash updated (may include no-content-change cases)
        files_written  = False   # a dashboard JSON was actually written to disk

        for db in dashboards:
            db_id      = db["id"]
            db_name    = db["name"]
            db_version = db.get("version")
            owner      = db.get("owner")
            total     += 1

            # --- Version check: skip export if unchanged -------------------
            if db_version and version_index.get(db_id) == db_version:
                skipped += 1
                print(f"[INFO] Skipped (unchanged) '{db_name}' (id={db_id})")
                continue

            # --- Fetch full definition only when version changed -----------
            try:
                definition = client.export_dashboard(db_id)
            except SignavioAPIError as exc:
                print(f"[WARN] Export failed for '{db_name}' (id={db_id}): {exc}", file=sys.stderr)
                skipped_export += 1
                continue

            # Resolve target path and detect renames.
            new_path      = dashboard_path(proc_name, proc_id, db_id, db_name)
            existing_path = find_existing_file(proc_name, proc_id, db_id)
            existing      = load_existing(existing_path) if existing_path else None

            # Secondary content check: version changed but exported content is identical
            # (can happen if the API bumps the version hash on non-visual saves).
            if existing == definition:
                skipped += 1
                # Update the index so we skip the export next time, but do NOT
                # mark this as a dashboard change — the file on disk is untouched.
                if db_version:
                    version_index[db_id] = db_version
                    index_dirty = True
                continue

            # Determine action label.
            if existing_path is None:
                action = "added"
            elif existing_path != new_path:
                action = "renamed"
                existing_path.unlink()
                print(f"[INFO] Renamed: {existing_path.name} → {new_path.name}")
            else:
                action = "updated"

            save_dashboard(new_path, definition)
            files_written = True

            # Update version index entry.
            if db_version:
                version_index[db_id] = db_version
                index_dirty = True

            owner_label = SignavioClient.owner_label(owner)
            line = (
                f"  - [{action}] {db_name} (id={db_id}) "
                f"[process: {proc_name}] — owner: {owner_label}"
            )
            updated.append(line)
            print(f"[INFO] {line.strip()}")

        # Only persist the index when dashboard files were actually written.
        # This prevents a standalone _index.json commit with a misleading
        # "no changes detected" message when only the version hash changed
        # but the exported content was identical.
        if index_dirty and files_written:
            save_index(proc_name, proc_id, version_index)

    # ---- Write commit message ---------------------------------------------
    write_commit_message(updated, skipped, skipped_export, total)

    if updated:
        print(
            f"[INFO] {len(updated)} dashboard(s) changed out of {total} checked "
            f"({skipped} skipped via version cache). Commit message written."
        )
    else:
        print(
            f"[INFO] No changes in {total} dashboard(s) "
            f"({skipped} skipped via version cache). Nothing to commit."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_backup()



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_backup()
