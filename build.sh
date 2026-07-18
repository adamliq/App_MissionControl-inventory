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
VERSION="1.0.2"

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
  || fail "bundled splunklib is missing or broken -- mcquery.py would fail at runtime"
log "  splunklib import OK"

log "Validating bundled splunklib package metadata ($PY_REQUIRED)"
PYTHONPATH="$STAGE_DIR/bin" "$PY_REQUIRED" -c "
import importlib.metadata
importlib.metadata.version('splunk-sdk')
" || fail "bin/*.dist-info is missing or broken -- splunklib.binding.request() calls importlib.metadata.version('splunk-sdk') on every HTTP request and would crash at runtime"
log "  splunklib package metadata OK"

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
