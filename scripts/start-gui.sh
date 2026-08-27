#!/usr/bin/env sh
set -eu

APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$APP_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
DATABASE=${MSDIAL_REPOSITORY_CATALOG:-"$APP_ROOT/catalog-data/catalog.sqlite"}

exec python3 -m msdial_repository_catalog.gui_server --database "$DATABASE"
