# TA-missioncontrol-inventory

Lookup-driven Splunk Cloud app for querying local Mission Control / SOAR inventory endpoints through Splunkd.

## What it provides

- `mcquery` generating search command
- `missioncontrol_endpoints.csv` endpoint registry
- Mission Control Inventory dashboard
- Disabled saved searches for current lookup export and daily summary indexing
- No stored passwords, usernames, or external HTTP targets

## Important support note

This app is designed to query local Splunkd paths such as:

```text
/servicesNS/-/missioncontrol/v1/soar/app
```

It intentionally does **not** query browser proxy paths such as:

```text
/en-GB/splunkd/__raw/servicesNS/-/missioncontrol/v1/soar/app
```

Mission Control and SOAR endpoint availability may vary by Splunk Cloud stack, entitlement, role, release, and whether the endpoint is considered supported for app-based use. Validate endpoint access in your Splunk Cloud environment before enabling scheduled collection.

## SPL examples

Query the enabled endpoints:

```spl
| mcquery all=true
```

Query one configured collection:

```spl
| mcquery collection=soar_apps
```

Hide raw JSON:

```spl
| mcquery all=true include_raw=false
```

Save current inventory to lookup:

```spl
| mcquery all=true include_raw=false
| outputlookup missioncontrol_inventory_current.csv
```

Collect daily snapshot:

```spl
| mcquery all=true include_raw=true
| collect index=summary sourcetype=missioncontrol:inventory marker="source=mcquery"
```

## Endpoint registry

Edit `lookups/missioncontrol_endpoints.csv`.

Default columns:

| Column | Purpose |
|---|---|
| collection | Friendly collection name used by `collection=<name>` |
| endpoint | Local Splunkd REST path |
| enabled | Whether `all=true` should include the row |
| page_size | Page size used by the command, capped at 500 |
| sort | Optional sort parameter |
| order | Optional order parameter |
| default_params | Query parameters in `a=b&c=d` format |
| result_path | Dot-path to the list of results, for example `items` or `data.items` |
| id_field | Dot-path for object id |
| name_field | Dot-path for object name |
| description | Human description of the endpoint row |

## Safety controls

The command rejects endpoints that:

- Are full URLs
- Contain `..` or backslash path traversal patterns
- Do not start with `/servicesNS/-/missioncontrol/` or `/services/missioncontrol/`

This keeps the app scoped to local Mission Control inventory paths and avoids an arbitrary URL fetcher pattern.

## Files

```text
TA-missioncontrol-inventory/
├── default/
│   ├── app.conf
│   ├── commands.conf
│   ├── savedsearches.conf
│   ├── transforms.conf
│   └── data/ui/views/missioncontrol_inventory.xml
├── lookups/
│   └── missioncontrol_endpoints.csv
├── bin/
│   ├── mcquery.py
│   └── splunklib/        # bundled Splunk SDK for Python, required by mcquery.py
├── metadata/
│   └── default.meta
├── README/
│   └── README.md
└── app.manifest
```

## Third-party components

`bin/splunklib/` is the `searchcommands` module from the Splunk SDK for
Python (Apache License 2.0), bundled directly in the app because Splunk's
built-in Python runtime does not ship `splunklib` itself. Custom chunked
search commands that import it must carry their own copy. See
`bin/splunklib/__init__.py` for the upstream copyright notice.

## Install notes

1. Install as a private app in Splunk Cloud.
2. Open the app and review `missioncontrol_endpoints.csv`.
3. Test manually:

```spl
| mcquery collection=soar_apps include_raw=false
```

4. Enable additional endpoint rows only after confirming that each path exists and the running user has permission.
5. Enable one of the saved searches if you want scheduled export or historical snapshots.

## Splunk Cloud packaging notes

- Static icon assets are included under `static/` for Splunk Web and AppInspect visibility checks.
- `default/app.conf` sets `[package] check_for_updates = true` so update checking is not disabled.
- `bin/commands.conf` declares `python.required = 3.13` to match the bundled Splunk SDK for Python (3.0.0), which requires Python 3.13.
- `metadata/default.meta` grants write access to both `admin` and `sc_admin` so the app's knowledge objects are manageable by Splunk Cloud administrators, who hold `sc_admin` rather than `admin`.

## AppInspect status

Validated with `splunk-appinspect` 4.2.1, `--mode precert` (all tags, including `cloud`): 0 errors, 0 failures, 0 future-failures. Two informational warnings remain and are expected for this app:

- `check_for_python_script_existence` — generic notice that Python files exist; `bin/mcquery.py` and the bundled `splunklib` are Python 3-only, which the check cannot infer automatically.
- `check_for_updates_disabled` — only applies to apps that will stay private and never reach Splunkbase; since `check_for_updates = true` is correct for a Splunkbase-listed app, this warning does not apply to the intended distribution path.

Run `./build.sh` to produce the package, then validate with:

```sh
pip install splunk-appinspect
splunk-appinspect inspect dist/TA-missioncontrol-inventory-1.0.1.spl --mode precert --max-messages all
```

