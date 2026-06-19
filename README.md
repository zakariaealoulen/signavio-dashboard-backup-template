# Signavio Dashboard Backup Pipeline

GitOps-style automated backup of all dashboards in a SAP Signavio Process Intelligence workspace.
Every dashboard is saved as a formatted JSON file and version-controlled — every change is traceable with a full diff, commit history, and owner attribution.

## How this repository was created

This repository was generated automatically by the **Signavio Backup Manager** web app.
You do not need to configure anything manually — all secrets and variables are already set.

If your GitHub Personal Access Token expires and backups stop working, go back to the Backup Manager app, run the setup wizard again with your new token and the same workspace name, and everything will be updated automatically.

## How it works

```
SAP BTP Job Scheduling Service  (fires Mon–Fri, every hour, 08:00–18:00 Paris)
    → SAP BTP CF proxy app       (adds required GitHub API headers)
        → GitHub workflow_dispatch
            → backup.py          (authenticates to Signavio, downloads changed dashboards)
                → git commit     (only if content actually changed)
```

## Repository structure

```
dashboards/
└── <ProcessName>__<ProcessID>/
    ├── _index.json                      ← version cache (internal, not a dashboard)
    └── db_<ID>__<DashboardName>.json    ← one file per dashboard
```

- **Folder per process** — named `ProcessName__ProcessID` to stay unique even if two processes share the same display name
- **File per dashboard** — named `db_<ID>__<DashboardName>.json` so renames in the Signavio UI are tracked as renames in git, not as delete + create
- **Version cache** — `_index.json` stores the last-seen version hash per dashboard; unchanged dashboards are skipped without calling the export API

## Commit message format

Every backup commit describes exactly what changed:

```
chore: backup 2 dashboards (12 total checked)

  - [updated] Sales Overview (id=f138b7ae) [process: Sales Analytics] — owner: Marie Dupont
  - [added]   Pipeline KPIs  (id=ab12cd34) [process: Sales Analytics] — owner: Jean Martin

10 dashboard(s) unchanged (version match).
```

Action labels: `added` (new dashboard), `updated` (content changed), `renamed` (display name changed in UI).

## Repository secrets & variables

These are configured automatically by the Backup Manager app. Do not edit them manually unless you know what you are doing.

| Name | Type | Purpose |
|---|---|---|
| `SIGNAVIO_PASSWORD` | **Secret** | Signavio account password |
| `SIGNAVIO_HOST` | Variable | Regional base URL (e.g. `https://editor.signavio.com`) |
| `SIGNAVIO_EMAIL` | Variable | Signavio account email |
| `SIGNAVIO_WORKSPACE_ID` | Variable | Workspace / tenant ID to back up |

## Schedule

The backup is triggered by **SAP BTP Job Scheduling Service** every hour, Monday–Friday, 06:00–16:00 UTC (08:00–18:00 Paris CEST).

Manual runs are available via **Actions → Run workflow**.

## First run

After setup, trigger the first run manually:

1. Go to the **Actions** tab
2. Click **Hourly Analytics Dashboard Backup**
3. Click **Run workflow → Run workflow**

This creates the initial baseline snapshot. The automated schedule takes over from there.

## Token expiry

Your GitHub Personal Access Token is stored in the backup schedule and used every hour to trigger this workflow. When it expires, backups will silently stop.

**To renew:** go back to the Backup Manager app, run the setup wizard again with your new token and the same workspace. Your repository and its history will be preserved — only the token is updated.
