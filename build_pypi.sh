#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd $SCRIPT_DIR
$SCRIPT_DIR/cleanup.sh
rm -f $SCRIPT_DIR/dist/*.*
rm -R -f $SCRIPT_DIR/build/*
# v3 is a PEP 517 project (pyproject.toml, no setup.py); build via the build frontend.
python3 -m build
twine upload --repository pypi dist/*
