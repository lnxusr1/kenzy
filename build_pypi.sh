#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
pandoc --from=markdown --to=rst --output=$SCRIPT_DIR/README.rst $SCRIPT_DIR/README.md
cd $SCRIPT_DIR
$SCRIPT_DIR/cleanup.sh
rm $SCRIPT_DIR/dist/*.*
python3 setup.py sdist bdist_wheel
twine upload --repository pypi dist/*
