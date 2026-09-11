# Arma 3 / Antistasi Helm chart

A single persistent Arma 3 dedicated server using [BrettMayson/Arma3Server](https://github.com/BrettMayson/Arma3Server). Defaults install **Antistasi Ultimate**, Workshop item [3020755032](https://steamcommunity.com/sharedfiles/filedetails/?id=3020755032), on **Altis**, with 32 slots, Regular difficulty, signature verification, BattlEye, and persistent campaign profiles. No additional faction/map mods or paid maps are needed for this default.

The [linked beginners guide](https://official-antistasi-community.github.io/A3-Antistasi-Docs/beginners_guide/raw_beginners_guide.html) covers **Community Edition**, a different Antistasi variant. Its dedicated-server setup recommendations inform this chart: load Antistasi as a client mod, load only one variant, use a mission cycle, and log in as admin for campaign setup. Use `examples/community.yaml` for that exact edition. Never combine Ultimate and Community in one mod list.

## Install

Requirements: Kubernetes 1.26+, Helm 3, a Linux amd64 node, a default storage class (or existing PVCs), and reachable UDP ports. Allow roughly 40 GiB for game data and 20 GiB for the default mod cache; larger modsets/CDLCs need more. Requests are 2 CPU / 4 GiB RAM with an 8 GiB memory limit. Fast single-core performance matters; adjust for your campaign and player count.

Create credentials in the release namespace using your secret manager or these placeholder commands. **Passwords are never Helm values or generated Kubernetes Secrets.**

```sh
kubectl create namespace arma3
kubectl -n arma3 create secret generic arma3-steam \
  --from-literal=username=YOUR_STEAM_USERNAME \
  --from-literal=password=YOUR_STEAM_PASSWORD
kubectl -n arma3 create secret generic arma3-admin \
  --from-literal=password=CHOOSE_A_STRONG_ADMIN_PASSWORD

# Available after the publishing workflow succeeds and the package is public:
helm upgrade --install antistasi \
  oci://ghcr.io/cfi2017/arma3-helm/charts/arma3 \
  --version 0.1.1 --namespace arma3 --wait --timeout 120m

# Or install directly from this checkout:
helm upgrade --install antistasi ./charts/arma3 \
  --namespace arma3 --wait --timeout 120m
```

Steam credentials are passed only to the bootstrap init container through `secretKeyRef`. Use an account that can download the server and selected Workshop items (Workshop access may require owning Arma 3). Upstream requires Steam Guard disabled for unattended login; interactive challenges cannot be answered by this chart. Do not use anonymous login for Workshop downloads. Keep credentials out of committed files and shell history; the commands above contain placeholders only.

The default NodePort setup reserves **UDP 32302–32306**. Forward/open that entire range on your firewall/router and connect to **the node running the pod**, port **32302**. With the default `externalTrafficPolicy: Local`, other nodes do not forward traffic unless they have the server pod. Find it with:

```sh
kubectl -n arma3 get pods -o wide
kubectl -n arma3 logs deployment/antistasi-arma3 -c bootstrap -f
kubectl -n arma3 logs deployment/antistasi-arma3 -c arma3 -f
```

Initial installation can take more than an hour. Downloads happen in an init container, so startup probes cannot interrupt them. `--wait` timing out does not cancel a download; inspect logs and rerun Helm with a longer timeout if needed.

## First campaign

1. Every player subscribes to **the same Antistasi variant and client mods** configured on the server and enables them in the Arma launcher. Server-only mods need not be loaded by players.
2. Direct-connect to the external IP and game port. The mission cycle selects `Antistasi_Altis.Altis` automatically; the mission is bundled in the mod, so there is no separate mission PBO to download.
3. Open text chat and enter `#login <admin password>`. A voted admin is insufficient for Antistasi's setup UI. Take the Default Commander slot and configure the new campaign/factions. Start with vanilla factions and only add the mods you actually want.
4. On subsequent starts, `autoLoadLastGame: 60` loads the last campaign 60 seconds after the first player connects if no admin is logged in. It does not create a configured campaign without the initial setup. Use `0` to disable autoload.
5. Save through Antistasi before upgrades/restarts. **Ultimate's Workshop page specifically advises Linux servers to untick “Use New Save file”.** Confirm a save/load round trip before investing in a long campaign. Community Edition and Ultimate saves/options are not interchangeable.

If you get an empty mission screen, log in as admin and use `#missions`. Check that the correct mod is loaded, all its dependencies are present, and the mission template matches the map. Changing the profile name makes existing saves appear absent; keep `server.profile` stable.

## Networking

| Offset | Name | Default UDP port |
| --- | --- | --- |
| +0 | Game | 32302 |
| +1 | Steam query | 32303 |
| +2 | Steam | 32304 |
| +3 | VON/reserved | 32305 |
| +4 | BattlEye | 32306 |

All five ports are exposed. `server.port` defines the base; the query port is explicitly set to base + 1. For NodePort, set `service.nodePortBase` to the **same** base to preserve advertised ports. The chart validates this and uses the standard NodePort range 30000–32767. Multiple installations need distinct five-port ranges.

For Gateway API:

```sh
# Install a UDP-capable Gateway controller and experimental Gateway API CRDs first.
# Edit the GatewayClass and namespace as appropriate for your cluster:
kubectl apply -f examples/gateway-resource.yaml
helm upgrade --install antistasi ./charts/arma3 -n arma3 \
  -f examples/gateway.yaml --wait --timeout 120m
kubectl -n arma3 get udproutes
```

The chart creates **one UDPRoute per port** against an existing Gateway. `examples/gateway.yaml` switches to ClusterIP and ports 2302–2306. The sample Gateway listens on those same ports and permits routes from namespace `arma3`; create its `gateway-system` namespace first if it does not exist. Replace `YOUR-UDP-GATEWAY-CLASS` with your controller's class. UDPRoute is experimental (`v1alpha2`) and **not supported by every Gateway controller**. The chart does not install CRDs or a controller. Verify `Accepted=True` and `ResolvedRefs=True` on all routes, and the Gateway's address/listener status. Route schema validation is based on Gateway API v1.2.1.

Set `gateway.parentRefs` and optionally `gateway.sectionNames` to attach to named listeners. Without section names, routes attach by their respective port. Listeners must use matching external ports, including when `sectionName` is supplied. Cross-namespace attachment needs the Gateway's `allowedRoutes`; the Service is in the route namespace, so it needs no ReferenceGrant. Gateway and NodePort exposure can also be enabled simultaneously. Ordinary HTTP Ingress cannot carry Arma UDP traffic.

`examples/loadbalancer.yaml` selects a UDP-capable LoadBalancer Service on 2302–2306 without Gateway API. Controller annotations and a requested IP are configurable. Router NAT, provider firewalls, and Gateway external traffic handling remain cluster-specific.

## Storage, mods, and updates

| Volume | Mount | Contents |
| --- | --- | --- |
| Data, 40 GiB | `/arma3` | Server binaries, missions, keys, generated config, **`configs/profiles` campaign saves** |
| Workshop, 20 GiB | `/arma3/steamapps/workshop` | Download metadata and content under `content/107410/<id>` |

Both PVCs default to `ReadWriteOnce` and Helm retention. `persistence.<data|workshop>` supports `existingClaim`, `storageClass`, `size`, `accessModes`, `annotations`, `retain`, and `enabled`. Empty storage class uses the cluster default; `"-"` selects no class. An existing claim takes precedence over `enabled`. Disabling persistence without an existing claim uses `emptyDir` and **loses that data when the pod is replaced**. Existing claims must be writable by the configured container UID; the pinned upstream image runs as root. If setting a non-root security context, provide compatible image/volume ownership and SteamCMD permissions.

Uninstall retains chart-created claims by default. Reuse them explicitly with `existingClaim` on reinstall; the chart will not adopt or recreate populated storage automatically. Back up the data PVC (especially `configs/profiles`) with snapshots or an offline copy. The generated `configs/server.cfg` contains admin/join passwords and is mode 0600; protect backups accordingly. A single-replica Deployment with `Recreate` prevents overlapping writers during ordinary upgrades. Do not force-delete pods or run a second release against the same claims.

Configure Workshop mods with string IDs; lists **replace** defaults:

```yaml
mods:
  workshop:
    - "3020755032" # Ultimate, client + server
    - "450814997"  # Example: CBA_A3, if your chosen modset needs it
  serverWorkshop: []
bootstrap:
  updatePolicy: always # or if-missing to reuse completed downloads
```

Use individual items, **not collections**. List every required dependency explicitly in load order. The bootstrap downloads and validates game/mod content, retries Steam failures, rejects incomplete mod downloads, normalizes Workshop filenames to lowercase for Linux, and copies signature keys. Removed mods stop loading and their managed keys are removed; their cached content is retained for reuse. `if-missing` uses completion markers and skips already installed content; `always` checks on every pod creation, not periodically during gameplay. Cached updates may still take time because Steam validates content. Defaults track Steam's current game/mod versions; the pinned container digest does not pin Workshop releases. Back up before updates and restart players' launchers to update matching mods.

For manual content, upload mission PBOs to `/arma3/mpmissions`, and lowercase mod directories to `/arma3/mods/<name>` or `/arma3/servermods/<name>`. Add their relative paths to `mods.local` or `mods.serverLocal`; unlisted directories are not loaded. Bootstrapping does not unpack arbitrary archives or expand collections. To install CDLCs, set `steam.branch: creatordlc`, add flags to `server.cdlc` (e.g. `[vn]`), increase storage, and select a suitable mission/modset. Players may need the corresponding DLC.

## Configuration

[values.yaml](charts/arma3/values.yaml) lists every option; [values.schema.json](charts/arma3/values.schema.json) validates values. Common settings:

- `steam.existingSecret`, `usernameKey`, `passwordKey`, `branchPasswordKey`: existing credential keys in the release namespace.
- `server.admin.existingSecret/passwordKey`, `server.admin.uids`: admin authentication. `server.password.existingSecret/passwordKey` optionally protects player joins.
- `server.hostname`, `maxPlayers`, `mission.template`, `mission.difficulty`, `mission.parameters`: mission and lobby configuration. Use `examples/community.yaml` for Community Edition. Set an empty mission template for manual selection.
- `server.extraConfig`: literal additional `server.cfg` entries. `server.existingConfigSecret` / `configKey` replaces the **entire** generated config, including passwords, query port, persistence and mission cycle. This bypasses the separate admin secret requirement. Custom difficulty profiles must be supplied separately on the data PVC.
- `server.binary`, `profile`, `world`, `limitFPS`, `cdlc`, `extraArgs`: launch options. Arguments are passed directly to the server, without shell evaluation. `extraEnv` applies to the game container. The chart owns startup; upstream `ARMA_*`, `MODS_PRESET`, and `HEADLESS_CLIENTS` environment variables do not configure it. Use separate headless-client deployments if needed.
- `resources`, `bootstrapResources`, scheduling, security contexts, probes, annotations, image credentials, extra volumes/mounts, and shutdown grace period are configurable.

The chart deliberately runs its own bootstrap and directly execs the game binary using the upstream image's SteamCMD/runtime. This catches upstream's unchecked Steam failures and preserves normal Kubernetes signal handling. SteamCMD is staged in `/tmp/arma3-steamcmd`, owned by the bootstrap user, so its launcher/native binary can execute and self-update with all capabilities dropped. Its downloaded Steam SDK libraries are copied to the data PVC for the game container. The pinned published image uses `/arma3`; **upstream's v2 branch uses `/arma3/server` and a different downloader and is not compatible**. Change the image digest only after checking its SteamCMD layout.

Config/mod value changes trigger pod replacement via a ConfigMap checksum. External Secret changes require an explicit `kubectl -n arma3 rollout restart deployment/antistasi-arma3`. Startup/readiness probes check the UDP game socket, not mission correctness; there is no default liveness restart. The pre-stop hook sends SIGINT to Arma, with 120 seconds to exit. This does not replace an in-game campaign save.

## Development and publishing

```sh
nix develop                    # Helm, kubectl, kubeconform, Python, actionlint, gh, jq
nix develop -c bash scripts/check.sh
nix develop -c bash scripts/kubeconform.sh  # fetches standard Kubernetes schemas
nix flake check --print-build-logs         # offline-capable checks once dependencies exist
```

The locked flake supports Linux/macOS on x86_64/aarch64 for tooling; the game image requires Linux amd64. Optionally copy `.envrc.example` to `.envrc` and use direnv. Checks cover rendering, invalid values, persistence, secret references, route schemas, download failure handling, filename normalization, campaign preservation and config generation. No Steam credentials are used by tests. A real game/Steam/cluster smoke test is still needed for your infrastructure.

GitHub Actions validates pull requests and pushes. On chart changes pushed to `main`, or manual dispatch, **Publish chart** runs the checks, packages the chart and pushes it to `oci://ghcr.io/<owner>/<repository>/charts/arma3` using `GITHUB_TOKEN` with `packages: write`. No PAT, Pages site, chart index or repository secret is needed. Each release uses `Chart.yaml`'s SemVer; **bump it for every chart release**. Existing version tags are skipped, not overwritten. Workflows serialize publication.

GitHub may create the first GHCR package as **private**. After first publication, make the package public in its GitHub package settings for anonymous Helm installs, or use `helm registry login ghcr.io` for private installs. Organization policies must permit Actions to create/write packages. The package path follows the repository automatically in forks; adjust the install URL. The OCI chart contains templates and the bootstrap script, not Steam game or Workshop content.

## Sources

- [Upstream image and configuration](https://github.com/BrettMayson/Arma3Server/tree/master)
- [Antistasi Community beginners / dedicated server guide](https://official-antistasi-community.github.io/A3-Antistasi-Docs/beginners_guide/raw_beginners_guide.html)
- [Antistasi Ultimate Workshop item and Linux save guidance](https://steamcommunity.com/sharedfiles/filedetails/?id=3020755032)
- [Ultimate mission configuration](https://github.com/Antistasi-Ultimate-Community/A3-Antistasi-Ultimate/blob/main/A3A/addons/maps/config.cpp)
- [Gateway API UDP routing](https://gateway-api.sigs.k8s.io/guides/udp-routing/)
