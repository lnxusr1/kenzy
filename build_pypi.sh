#!/bin/sh
# Build and publish kenzy to the REAL PyPI. Uploads are permanent (a version can
# never be replaced), so this validates the artifacts and asks before uploading.
# Run from the activated venv so `build` and `twine` resolve there.
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd $SCRIPT_DIR
$SCRIPT_DIR/cleanup.sh
rm -f $SCRIPT_DIR/dist/*.*
rm -R -f $SCRIPT_DIR/build/*
# v3 is a PEP 517 project (pyproject.toml, no setup.py); build via the build frontend.
python3 -m build
# Validate metadata + long-description rendering BEFORE the irreversible upload.
twine check dist/*

VERSION=$(ls "$SCRIPT_DIR"/dist/kenzy-*.whl 2>/dev/null | sed -E 's#.*/kenzy-([^-]+)-.*#\1#' | head -1)
printf 'Upload kenzy %s to the REAL PyPI now? This is permanent. [y/N] ' "$VERSION"
read -r ans
case "$ans" in
  [yY] | [yY][eE][sS]) ;;
  *) echo "Aborted — nothing uploaded."; exit 1 ;;
esac

twine upload --repository pypi dist/*
