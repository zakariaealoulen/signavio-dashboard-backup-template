# Analytics Dashboard Backup Pipeline

GitOps-style hourly backup of all dashboards in an Analytics workspace.  
Each dashboard is saved as a formatted JSON file and version-controlled so every change is traceable to a named user.

## Repository secrets & variables

| Name | Type | Purpose |
|---|---|---|
| `ANALYTICS_API_TOKEN` | **Secret** | Bearer token for the GraphQL API |
| `ANALYTICS_API_URL` | Variable | GraphQL endpoint URL |
| `ANALYTICS_WORKSPACE_ID` | Variable | Workspace ID to back up |

## Schedule

Runs every hour, Monday–Friday, 06:00–16:00 UTC (08:00–18:00 CEST / 07:00–17:00 CET).  
Manual runs are available via **Actions → Run workflow**.

## Output

- `dashboards/db_<ID>.json` — one file per dashboard (ID-stable, rename-safe)  
- `commit_message.txt` — temporary file listing which dashboards changed and who last modified them (staged by the workflow, not committed)
