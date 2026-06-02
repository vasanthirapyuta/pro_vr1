#!/bin/bash
# QA Dashboard launcher — sources .bashrc for ADO_PAT/GITHUB_TOKEN, uses Poetry venv Python
source ~/.bashrc 2>/dev/null || true
PYTHON="/home/vasanthi/.cache/pypoetry/virtualenvs/e2e-tests-Gs0inw3F-py3.8/bin/python3"
cd "$(dirname "$0")"
exec "$PYTHON" app.py "$@"
