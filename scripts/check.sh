#!/usr/bin/env bash
set -euo pipefail
helm lint charts/arma3 --strict
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yaml
