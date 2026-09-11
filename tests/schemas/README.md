`udproute_v1alpha2.json` is the OpenAPI validation schema extracted without modification from the `v1alpha2` version of:

https://github.com/kubernetes-sigs/gateway-api/blob/v1.2.1/config/crd/experimental/gateway.networking.k8s.io_udproutes.yaml

Upstream license: Apache-2.0. Used for offline Helm-rendering tests and kubeconform validation. Refresh deliberately when changing the supported Gateway API version.

`ListenerSet_v1.json` is the focused schema used for this chart's generated
stable Gateway API v1 ListenerSet. It covers the fields emitted by the chart;
the complete CRD is supplied by the Gateway API v1.6 installation.
