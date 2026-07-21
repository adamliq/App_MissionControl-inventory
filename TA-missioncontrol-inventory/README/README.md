# TA-missioncontrol-inventory

Lookup-driven Splunk Cloud app for querying local Mission Control / SOAR inventory endpoints through Splunkd.

## What it provides

- `mcquery` generating search command (read-only, GET)
- `mcpost` generating search command (SPL syntax validation, POST)
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

If you paste in a Splunk Web browser-proxy path by mistake (anything containing `/splunkd/__raw/`), `mcquery` rejects it with an `endpoint_rejected` error event that tells you to strip the `/<locale>/splunkd/__raw` prefix, rather than failing silently or fetching it anyway.

## SPL examples

`mcquery` requires either `collection=<name>` or `all=true` -- bare `| mcquery` with neither returns a `no_matching_endpoints` error event rather than silently guessing you meant "all enabled endpoints."

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

## Safety controls (mcquery)

The command rejects endpoints that:

- Are full URLs
- Contain `..` or backslash path traversal patterns
- Are Splunk Web browser-proxy paths (contain `/splunkd/__raw/`)
- Are not `/servicesNS/<owner>/<app>/...` or `/services/<app>/...` where `<app>` is in the allowed app namespace list (`ALLOWED_APP_NAMESPACES` in `bin/mcquery.py`, currently `missioncontrol` and `SplunkEnterpriseSecuritySuite`)

Endpoint values must keep their absolute leading slash (e.g. `/servicesNS/-/missioncontrol/v1/soar/app`, not `servicesNS/-/missioncontrol/v1/soar/app`). splunklib treats a path with no leading slash as *relative* to the search's own default namespace and silently re-prefixes it -- `mcquery.py` used to strip the leading slash itself (`endpoint.lstrip("/")`), which caused every request to 404 regardless of how correct the configured path was. `build.sh` now verifies every row in the lookup resolves to its own literal path.

The owner segment (`-`, `nobody`, or a real username) is intentionally unconstrained since it doesn't affect which app's REST handler answers the request; the app namespace is what's checked. This keeps the app scoped to a fixed allowlist of REST-registering apps and avoids an arbitrary URL fetcher pattern. To trust a new app's endpoints, add its namespace to `ALLOWED_APP_NAMESPACES` explicitly -- this is a code change, not a lookup-editable setting, so the safety boundary can't be widened just by editing the CSV.

## mcpost (SPL syntax validation)

`mcpost` POSTs an SPL query string to Splunkd's search parser endpoint with `parse_only=true`, to validate syntax without running the search. It does not dispatch, schedule, or execute anything.

```spl
| mcpost query="index=main | stats count"
```

Output fields: `endpoint`, `query`, `messages` (if the parser returned any), and `raw_json` (the full parser response). A syntax error surfaces as an `error=parse_failed` event with the HTTP status and detail Splunkd returned, rather than a bare exception.

**Safety model -- deliberately tighter than mcquery:**

- `mcquery` is read-only (GET) and allowlists an *app namespace prefix* (`/servicesNS/<owner>/<app>/...`), so any path under a trusted app is reachable.
- `mcpost` performs POST requests, so it allowlists an *exact full path* instead: `ALLOWED_POST_ENDPOINTS` in `bin/mcpost.py` currently contains only `/services/search/v2/parser`. There is no app-namespace matching for POST -- a namespace-prefix allowlist would also open every other endpoint under that namespace (e.g. `/services/search/jobs`, which dispatches and runs searches), which is a categorically different risk than a syntax check.
- `parse_only=true` and `output_mode=json` are hardcoded, not user-configurable options. This command is a syntax validator, not a general search-dispatch proxy; if you need different POST behavior, that's a deliberate code change to `bin/mcpost.py`, not a runtime flag.
- Same baseline protections as `mcquery` (shared via `bin/mc_common.py`): rejects full URLs, path traversal, and Splunk Web browser-proxy paths (`/splunkd/__raw/`), and preserves the endpoint's absolute leading slash so splunklib doesn't silently re-prefix the path.

## Files

```text
TA-missioncontrol-inventory/
├── default/
│   ├── app.conf
│   ├── commands.conf
│   ├── logging.conf
│   ├── savedsearches.conf
│   ├── transforms.conf
│   └── data/ui/views/missioncontrol_inventory.xml
├── lookups/
│   └── missioncontrol_endpoints.csv
├── bin/
│   ├── mcquery.py
│   ├── mcpost.py
│   ├── mc_common.py                     # safety helpers shared by mcquery.py and mcpost.py
│   ├── splunklib/                       # bundled Splunk SDK for Python, required by mcquery.py/mcpost.py
│   └── splunk_sdk-3.0.0.dist-info/      # package metadata splunklib reads at request time
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

`bin/splunk_sdk-3.0.0.dist-info/` ships alongside it because
`splunklib.binding` calls `importlib.metadata.version("splunk-sdk")` on
every HTTP request (to build the `User-Agent` header). Without installed
package metadata on `sys.path`, that call raises
`importlib.metadata.PackageNotFoundError` and `mcquery`/`mcpost` fail on
their first real request. `build.sh` verifies this resolves correctly on
every build.

## Operational logging

Every REST call `mcquery` and `mcpost` make is logged at INFO level via
`self.logger` (the Splunk-supported logger the SDK's `SearchCommand` base
class exposes), including the exact path and query/body parameters used:

```text
mcquery collection=soar_apps url=/servicesNS/-/missioncontrol/v1/soar/app?configured=true&pretty=true&page_size=100&page=0
mcpost endpoint=/services/search/v2/parser params={'q': 'index=main | stats count', 'parse_only': 'true', 'output_mode': 'json'}
```

`default/logging.conf` configures this logger at INFO level with a
`RotatingFileHandler` writing to
`$SPLUNK_HOME/var/log/splunk/mission_control_inventory.log` (10 MB per
file, 3 backups), shared by both commands. Without it, `SearchCommand`'s
logger defaults to `WARNING`, so `self.logger.info(...)` calls are
silently dropped -- `build.sh` verifies the effective level and that a
log line actually lands in that file for both commands on every build.

Because `logging.conf` isn't one of Splunk's own recognized `.conf` file
types, AppInspect treats it as a custom config and requires a reload
trigger and a search head cluster replication entry: `default/app.conf`
has `[triggers] reload.logging = simple` and `default/server.conf` has
`[shclustering] conf_replication_include.logging = true`.

This log file is not indexed into Splunk by default. To search it, add a
`monitor://$SPLUNK_HOME/var/log/splunk/mission_control_inventory.log`
stanza to `local/inputs.conf` with an appropriate index and sourcetype.

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
- The Mission Control Inventory dashboard's "Inventory results" and "Counts by collection" panels share one base search (`id="mcquery_base"`) rather than each running their own `| mcquery all=true`, so opening the dashboard queries every live endpoint once per load, not twice.

## AppInspect status

Validated with `splunk-appinspect` 4.2.1, `--mode precert` (all tags, including `cloud`): 0 errors, 0 failures, 0 future-failures. Three informational warnings remain and are expected for this app:

- `check_for_python_script_existence` — generic notice that Python files exist; `bin/mcquery.py` and the bundled `splunklib` are Python 3-only, which the check cannot infer automatically.
- `check_for_updates_disabled` — only applies to apps that will stay private and never reach Splunkbase; since `check_for_updates = true` is correct for a Splunkbase-listed app, this warning does not apply to the intended distribution path.
- `check_python_sdk_version` — confirms the bundled SDK version (3.0.0) via `bin/splunk_sdk-3.0.0.dist-info/METADATA` and explicitly says "No action required at this time."

Run `./build.sh` to produce the package, then validate with:

```sh
pip install splunk-appinspect
splunk-appinspect inspect dist/TA-missioncontrol-inventory-1.3.1.spl --mode precert --max-messages all
```

