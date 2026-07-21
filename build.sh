#!/usr/bin/env bash
# Repeatable build/package script for TA-missioncontrol-inventory.
#
# Usage: ./build.sh
#
# Cleans the build output, copies the source tree, strips
# packaging-prohibited files, validates conf/XML/JSON/Python syntax,
# imports the bundled splunklib to catch missing-dependency regressions,
# and produces a Splunkbase-ready .spl (a renamed .tar.gz) plus a
# SHA-256 checksum and a build report.
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

APP_NAME="TA-missioncontrol-inventory"
VERSION="1.3.2"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${ROOT_DIR}/${APP_NAME}"
DIST_DIR="${ROOT_DIR}/dist"
BUILD_DIR="${DIST_DIR}/build"
STAGE_DIR="${BUILD_DIR}/${APP_NAME}"
PACKAGE_FILE="${DIST_DIR}/${APP_NAME}-${VERSION}.spl"
REPORT_FILE="${DIST_DIR}/build-report.txt"

log() { printf '==> %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

[ -d "$SRC_DIR" ] || fail "source directory not found: $SRC_DIR"

log "Cleaning build directory"
rm -rf "$BUILD_DIR"
mkdir -p "$STAGE_DIR"

log "Copying approved application files"
tar -c \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '.pytest_cache' \
  --exclude '.git' \
  --exclude '.gitignore' \
  --exclude '.DS_Store' \
  --exclude '*.egg-info' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '*.log' \
  --exclude 'tests' \
  -C "$(dirname "$SRC_DIR")" "$(basename "$SRC_DIR")" \
  | tar -x -C "$BUILD_DIR"

log "Removing prohibited and temporary files (defense in depth)"
find "$STAGE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE_DIR" -name '*.pyc' -delete
find "$STAGE_DIR" -name '.DS_Store' -delete
find "$STAGE_DIR" -name '.git*' -delete

PY_REQUIRED="python3.13"
command -v "$PY_REQUIRED" >/dev/null 2>&1 || PY_REQUIRED="python3"

log "Validating Python syntax ($PY_REQUIRED)"
find "$STAGE_DIR/bin" -name '*.py' -print0 | xargs -0 -n1 "$PY_REQUIRED" -m py_compile
log "  Python syntax OK"

log "Validating bundled splunklib is importable ($PY_REQUIRED)"
PYTHONPATH="$STAGE_DIR/bin" "$PY_REQUIRED" -c "from splunklib.searchcommands import GeneratingCommand" \
  || fail "bundled splunklib is missing or broken -- mcquery.py/mcparser.py would fail at runtime"
log "  splunklib import OK"

log "Validating custom commands import cleanly ($PY_REQUIRED)"
PYTHONPATH="$STAGE_DIR/bin" "$PY_REQUIRED" -c "
import mcquery
import mcparser
mcquery.MCQueryCommand()
mcparser.MCParserCommand()
" || fail "mcquery.py or mcparser.py failed to import/instantiate"
log "  custom commands OK"

log "Validating bundled splunklib package metadata ($PY_REQUIRED)"
PYTHONPATH="$STAGE_DIR/bin" "$PY_REQUIRED" -c "
import importlib.metadata
importlib.metadata.version('splunk-sdk')
" || fail "bin/*.dist-info is missing or broken -- splunklib.binding.request() calls importlib.metadata.version('splunk-sdk') on every HTTP request and would crash at runtime"
log "  splunklib package metadata OK"

log "Validating operational logging configuration ($PY_REQUIRED)"
LOG_TEST_DIR="$(mktemp -d)"
mkdir -p "$LOG_TEST_DIR/SPLUNK_HOME/var/log/splunk"
cp -r "$STAGE_DIR" "$LOG_TEST_DIR/$APP_NAME"
cat > "$LOG_TEST_DIR/$APP_NAME/bin/_logging_check.py" <<'PYEOF'
import mcparser
import mcquery

for cmd in (mcquery.MCQueryCommand(), mcparser.MCParserCommand()):
    level = cmd.logger.getEffectiveLevel()
    if level > 20:
        raise SystemExit(f"{cmd.__class__.__name__} logger effective level is {level}, expected INFO (20) or lower")
    cmd.logger.info(f"build.sh logging self-check ({cmd.__class__.__name__})")
PYEOF
(
  cd "$LOG_TEST_DIR/$APP_NAME/bin"
  SPLUNK_HOME="$LOG_TEST_DIR/SPLUNK_HOME" "$PY_REQUIRED" _logging_check.py
) || fail "default/logging.conf is missing or broken -- mcquery/mcparser would silently drop self.logger.info() calls (including the queried-URL log line) at runtime"
LOG_FILE="$LOG_TEST_DIR/SPLUNK_HOME/var/log/splunk/mission_control_inventory.log"
grep -q "build.sh logging self-check (MCQueryCommand)" "$LOG_FILE" 2>/dev/null \
  || fail "default/logging.conf did not produce a log line for MCQueryCommand at var/log/splunk/mission_control_inventory.log"
grep -q "build.sh logging self-check (MCParserCommand)" "$LOG_FILE" 2>/dev/null \
  || fail "default/logging.conf did not produce a log line for MCParserCommand at var/log/splunk/mission_control_inventory.log"
rm -rf "$LOG_TEST_DIR"
log "  logging configuration OK"

log "Validating absolute-path handling for configured endpoints ($PY_REQUIRED)"
PYTHONPATH="$STAGE_DIR/bin" "$PY_REQUIRED" -c "
import csv
from pathlib import Path
from splunklib.binding import Context

import mc_common
import mcparser

ctx = Context(host='127.0.0.1', port=8089, owner='nobody', app='search', scheme='https')
failed = []

lookup = Path('$STAGE_DIR/lookups/missioncontrol_endpoints.csv')
with lookup.open(newline='', encoding='utf-8-sig') as handle:
    for row in csv.DictReader(handle):
        endpoint = (row.get('endpoint') or '').strip()
        if not endpoint:
            continue
        resolved = ctx._abspath(mc_common.to_absolute_path(endpoint))
        if resolved != endpoint:
            failed.append((row.get('collection'), endpoint, resolved))

for endpoint in mcparser.ALLOWED_POST_ENDPOINTS:
    resolved = ctx._abspath(mc_common.to_absolute_path(endpoint))
    if resolved != endpoint:
        failed.append(('mcparser', endpoint, resolved))

if failed:
    for collection, endpoint, resolved in failed:
        print(f'  {collection}: {endpoint} resolved to {resolved}, expected unchanged')
    raise SystemExit(1)
" || fail "an endpoint does not resolve to its own literal path -- mcquery/mcparser would silently 404 (this is the exact bug class where lstrip('/') mangled every request)"
log "  absolute-path handling OK"

log "Validating XML views/nav"
find "$STAGE_DIR/default/data/ui" -name '*.xml' -print0 | xargs -0 -n1 xmllint --noout
log "  XML OK"

log "Validating app.manifest JSON"
python3 -c "import json; json.load(open('${STAGE_DIR}/app.manifest'))"
log "  JSON OK"

log "Checking for duplicate stanzas in .conf files"
python3 - "$STAGE_DIR" <<'PYEOF'
import configparser
import sys
from pathlib import Path

stage = Path(sys.argv[1])
failed = False
for conf in stage.rglob("default/*.conf"):
    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read(conf)
    except configparser.DuplicateSectionError as exc:
        print(f"FAIL: duplicate stanza in {conf}: {exc}", file=sys.stderr)
        failed = True
if failed:
    sys.exit(1)
PYEOF
log "  conf stanzas OK"

log "Checking version consistency across app.conf and app.manifest"
CONF_VERSION="$(grep -E '^version' "$STAGE_DIR/default/app.conf" | head -1 | cut -d= -f2 | tr -d ' ')"
MANIFEST_VERSION="$(python3 -c "import json; print(json.load(open('${STAGE_DIR}/app.manifest'))['info']['id']['version'])")"
[ "$CONF_VERSION" = "$VERSION" ] || fail "app.conf version ($CONF_VERSION) != build version ($VERSION)"
[ "$MANIFEST_VERSION" = "$VERSION" ] || fail "app.manifest version ($MANIFEST_VERSION) != build version ($VERSION)"
log "  version OK ($VERSION)"

log "Removing any bytecode caches produced by validation steps"
find "$STAGE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE_DIR" -name '*.pyc' -delete

log "Checking for disallowed artifacts in staged package"
DISALLOWED="$(find "$STAGE_DIR" -name '__pycache__' -o -name '*.pyc' -o -name '.git' -o -name '.DS_Store')"
[ -z "$DISALLOWED" ] || fail "disallowed artifacts present: $DISALLOWED"
log "  no disallowed artifacts"

log "Creating package: $PACKAGE_FILE"
mkdir -p "$DIST_DIR"
rm -f "$PACKAGE_FILE"
tar -czf "$PACKAGE_FILE" -C "$BUILD_DIR" "$APP_NAME"

log "Computing SHA-256 checksum"
CHECKSUM="$(shasum -a 256 "$PACKAGE_FILE" | awk '{print $1}')"

log "Writing build report"
{
  echo "TA-missioncontrol-inventory build report"
  echo "Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Version: $VERSION"
  echo "Package: $(basename "$PACKAGE_FILE")"
  echo "SHA-256: $CHECKSUM"
  echo ""
  echo "Package contents:"
  tar -tzf "$PACKAGE_FILE" | sort
} > "$REPORT_FILE"

log "Build complete"
log "  Package:  $PACKAGE_FILE"
log "  Checksum: $CHECKSUM"
log "  Report:   $REPORT_FILE"
