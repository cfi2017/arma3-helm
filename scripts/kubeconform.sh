#!/usr/bin/env bash
set -euo pipefail
# Use the checked-in stable Gateway API v1 UDPRoute schema for these examples.
schema='tests/schemas/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
for values in examples/community.yaml examples/gateway.yaml examples/listenerset.yaml examples/existing-pvcs.yaml examples/loadbalancer.yaml; do
  helm template test charts/arma3 -f "$values" | kubeconform -strict -summary \
    -schema-location default -schema-location "$schema"
done
helm template test charts/arma3 | kubeconform -strict -summary
