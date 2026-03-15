#!/usr/bin/env bash
# Render.com build script: install setuptools/wheel first to avoid
# "Cannot import 'setuptools.build_meta'" when any package builds from source.
set -e
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
