#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
find $SCRIPT_DIR/src -type d -name "__pycache__" -exec rm -rf {} +;
find $SCRIPT_DIR/src -name *.pyc -exec rm -rf {} \;
