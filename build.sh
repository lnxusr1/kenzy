#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd $SCRIPT_DIR
$SCRIPT_DIR/cleanup.sh
rm $SCRIPT_DIR/dist/*.*
python3 setup.py sdist bdist_wheel