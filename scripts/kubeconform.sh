#!/usr/bin/env bash
set -euo pipefail
# Pin the experimental UDPRoute schema version used by these examples.
schema='tests/schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
for values in examples/community.yaml examples/gateway.yaml examples/existing-pvcs.yaml examples/loadbalancer.yaml; do
  helm template test charts/arma3 -f "$values" | kubeconform -strict -summary \
    -schema-location default -schema-location "$schema"
done
helm template test charts/arma3 | kubeconform -strict -summary
