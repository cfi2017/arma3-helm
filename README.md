# Arma 3 / Antistasi Helm chart

A single persistent Arma 3 dedicated server using [BrettMayson/Arma3Server](https://github.com/BrettMayson/Arma3Server). Defaults install **Antistasi Ultimate**, Workshop item [3020755032](https://steamcommunity.com/sharedfiles/filedetails/?id=3020755032), on **Altis**, with 32 slots, Regular difficulty, signature verification, BattlEye, and persistent campaign profiles. No additional faction/map mods or paid maps are needed for this default.

The [linked beginners guide](https://official-antistasi-community.github.io/A3-Antistasi-Docs/beginners_guide/raw_beginners_guide.html) covers **Community Edition**, a different Antistasi variant. Its dedicated-server setup recommendations inform this chart: load Antistasi as a client mod, load only one variant, use a mission cycle, and log in as admin for campaign setup. Use `examples/community.yaml` for that exact edition. Never combine Ultimate and Community in one mod list.

## Install

Requirements: Kubernetes 1.26+, Helm 3, a Linux amd64 node, a default storage class (or existing PVCs), and reachable UDP ports. Allow roughly 80 GiB for base game data and 20 GiB for the default mod cache, plus 1 GiB for Steam authentication/client state; larger modsets/CDLCs need more. Requests are 2 CPU / 4 GiB RAM with an 8 GiB memory limit. Fast single-core performance matters; adjust for your campaign and player count.

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
  --version 0.4.0 --namespace arma3 --wait --timeout 120m

# Or install directly from this checkout:
helm upgrade --install antistasi ./charts/arma3 \
  --namespace arma3 --wait --timeout 120m
```

Steam credentials are passed only to the bootstrap init container through `secretKeyRef`. Use an account that owns Arma 3 and can access the selected Workshop items. The dedicated-server depot can be downloadable without the full game subscription, while Workshop downloads generally require the account to own Arma 3; a successful server install does not prove Workshop access. Keep Steam Guard enabled: bootstrap accepts codes through an interactive Kubernetes terminal as described below. Anonymous login was tested and could not download the Arma 3 server (`No subscription`) or the selected Workshop item. Keep credentials out of committed files and shell history; the commands above contain placeholders only.

The default NodePort setup reserves **UDP 32302–32306**. Forward/open that entire range on your firewall/router and connect to **the node running the pod**, port **32302**. With the default `externalTrafficPolicy: Local`, other nodes do not forward traffic unless they have the server pod. Find it with:

```sh
kubectl -n arma3 get pods -o wide
kubectl -n arma3 logs deployment/antistasi-arma3 -c bootstrap -f
kubectl -n arma3 logs deployment/antistasi-arma3 -c arma3 -f
```

Initial installation can take more than an hour. Downloads happen in an init container, so startup probes cannot interrupt them. `--wait` timing out does not cancel a download; inspect logs and rerun Helm with a longer timeout if needed.

## Steam Guard authentication

Bootstrap first tries `login <username>` with saved Steam authentication. If Steam asks for a password, the broker supplies it from the existing Secret through its terminal. If a Steam Guard challenge appears, bootstrap waits up to **30 minutes** (`steam.authTimeoutSeconds: 1800`) and logs the exact pod/namespace command to attach. It then downloads the game and all configured Workshop items in **one session**. The download timeout starts only after login succeeds.

In a second terminal while Helm is waiting:

```sh
kubectl -n arma3 get pods
kubectl -n arma3 logs deployment/antistasi-arma3 -c bootstrap -f
# Replace POD_NAME with the pod from the logs/get pods output:
kubectl exec -it -n arma3 POD_NAME -c bootstrap -- \
  python3 /chart/runtime.py auth
```

Enter your current email/Steam Mobile authenticator code when prompted. Input is hidden. If SteamCMD requests app approval, approve the login in Steam Mobile and wait; the helper exits when authentication succeeds. QR support is not assumed. The helper connects to the **existing** SteamCMD process over a local Unix socket. It does not launch another downloader or expose an HTTP service. Access requires Kubernetes `pods/exec` permission; the socket accepts one client at a time and has mode 0600 inside a mode-0700 directory.

Ctrl-C or Ctrl-D detaches **without cancelling** the pending login; you can reconnect. If Steam repeats a code prompt, enter a fresh code. If Steam terminates login instead, or authentication times out, the init container fails and Kubernetes retries with its normal backoff. Download retries use `bootstrap.retries`; authentication failures are not immediately retried by the broker. If the server depot succeeds but a Workshop item fails, the successful server install is marked before retrying, so later attempts do not re-verify the full 5.3 GiB depot. A Workshop `Failure` immediately after `Downloading item ...` usually means the Steam account cannot access that item: verify Arma 3 ownership, open the item in the Steam client, subscribe to it, and test that the account can download it. To explicitly terminate the current attempt:

```sh
kubectl exec -n arma3 POD_NAME -c bootstrap -- \
  python3 /chart/runtime.py auth --cancel
```

Kubernetes may restart the init container after cancellation. To stop bootstrapping altogether, scale the deployment to zero (or suspend the release through your GitOps controller).

Authentication state lives on the **Steam PVC**, mounted only in bootstrap at `/var/lib/arma3-steam`: `steamcmd/` holds the self-updated client/config and `home/` holds Steam's home/authentication files. The chart never writes codes to files, logs, or process arguments. Raw login output is withheld from logs to avoid echoed credentials; only fixed login/approval statuses are shown. Download output is streamed with known secrets redacted. Steam manages its own authentication cache and logs within this protected volume, so treat that PVC and its backups as credentials. The game container receives only the SDK libraries it needs, not this volume or the Steam Secret.

Saved authentication is reused where Steam supports it; approvals can expire or be revoked. A fresh PVC, changed account, changed password, or Steam policy may require approval again. To reset cached authentication, stop the deployment, mount **only the Steam PVC** in a maintenance pod, remove its `steamcmd/config` and `home` authentication state (or provision a fresh Steam PVC), and restart. A fresh Steam PVC is the most complete reset. Never remove the data or Workshop PVCs for an authentication reset. Do not run competing SteamCMD processes on the same Steam state.

**Upgrading from 0.1.x:** version 0.2.1 includes the Steam Guard flow and preserves the existing data, Workshop, and Steam claims. On Helm 3.14+, merge new defaults with your existing overrides:

```sh
helm upgrade antistasi oci://ghcr.io/cfi2017/arma3-helm/charts/arma3 \
  -n arma3 --version 0.4.0 --reset-then-reuse-values --wait --timeout 150m
```

For older Helm, use `--reset-values -f your-values.yaml` instead. Plain `--reuse-values` can omit the new defaults. Use your actual namespace (for example `app-arma3-antistasi`) in both the upgrade and attach commands. Avoid `--atomic` during first authentication: an unattended Helm timeout could roll back the waiting pod. GitOps installations should set a Helm timeout long enough for approval plus the first downloads.

## First campaign

1. Every player subscribes to **the same Antistasi variant and client mods** configured on the server and enables them in the Arma launcher. Server-only mods need not be loaded by players.
2. Direct-connect to the external IP and game port. The server starts its configured mission cycle automatically (`-autoInit`) and selects the Ultimate `Antistasi_Altis.Altis` mission; bootstrap extracts it from the Workshop PBO into `mpmissions` when needed.
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
# Install a UDP-capable Gateway controller and Gateway API v1.6 CRDs first.
# Edit the GatewayClass and namespace as appropriate for your cluster:
kubectl apply -f examples/gateway-resource.yaml
helm upgrade --install antistasi ./charts/arma3 -n arma3 \
  -f examples/gateway.yaml --wait --timeout 120m
kubectl -n arma3 get udproutes
```

The chart creates **one stable v1 UDPRoute per port** against an existing Gateway. `examples/gateway.yaml` switches to ClusterIP and ports 2302–2306. The sample Gateway listens on those same ports and permits routes from namespace `arma3`; create its `gateway-system` namespace first if it does not exist. Replace `YOUR-UDP-GATEWAY-CLASS` with your controller's class. UDPRoute support remains controller-dependent. The chart does not install CRDs or a controller. Verify `Accepted=True` and `ResolvedRefs=True` on all routes, and the Gateway's address/listener status. Route schema validation is based on Gateway API v1.6.0.

To use stable Gateway API v1 `ListenerSet` resources, set `gateway.listenerSet.enabled: true` and keep exactly one entry in `gateway.parentRefs`. The chart creates one ListenerSet containing the five UDP listeners and changes every UDPRoute parentRef to that ListenerSet. Listener names come from `gateway.sectionNames` when supplied, otherwise they are `game`, `query`, `steam`, `von`, and `battleye`. Gateway API v1.6 Gateways deny ListenerSet attachment by default, so the Gateway must set `spec.allowedListeners` for the release namespace; see `examples/gateway-resource.yaml` and `examples/listenerset.yaml`. ListenerSet and UDPRoute support remains controller-dependent, and the chart does not install either CRD.

Set `gateway.parentRefs` and optionally `gateway.sectionNames` to attach to named listeners. Without section names, routes attach by their respective port. Listeners must use matching external ports, including when `sectionName` is supplied. Cross-namespace attachment needs the Gateway's `allowedRoutes`; the Service is in the route namespace, so it needs no ReferenceGrant. Gateway and NodePort exposure can also be enabled simultaneously. Ordinary HTTP Ingress cannot carry Arma UDP traffic.

`examples/loadbalancer.yaml` selects a UDP-capable LoadBalancer Service on 2302–2306 without Gateway API. Controller annotations and a requested IP are configurable. Router NAT, provider firewalls, and Gateway external traffic handling remain cluster-specific.

## Storage, mods, and updates

| Volume | Mount | Contents |
| --- | --- | --- |
| Data, 40 GiB | `/arma3` | Server binaries, missions, keys, generated config, **`configs/profiles` campaign saves** |
| Steam, 1 GiB | `/var/lib/arma3-steam` (bootstrap only) | SteamCMD, saved authentication and Steam home/config |
| Workshop, 20 GiB | `/arma3/steamapps/workshop` | Download metadata and content under `content/107410/<id>` |

All three PVCs default to `ReadWriteOnce` and Helm retention. `persistence.<data|workshop|steam>` supports `existingClaim`, `storageClass`, `size`, `accessModes`, `annotations`, `retain`, and `enabled`. Empty storage class uses the cluster default; `"-"` selects no class. An existing claim takes precedence over `enabled`. Disabling persistence without an existing claim uses `emptyDir` and **loses that data when the pod is replaced**. Existing claims must be writable by the configured container UID; the pinned upstream image runs as root. If setting a non-root security context, provide compatible image/volume ownership and SteamCMD permissions.

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

The chart deliberately runs its own bootstrap and directly execs the game binary using the upstream image's SteamCMD/runtime. This catches upstream's unchecked Steam failures and preserves normal Kubernetes signal handling. SteamCMD is staged persistently in `/var/lib/arma3-steam/steamcmd`, owned by the bootstrap user, so its launcher/native binary can execute and self-update with all capabilities dropped. Its downloaded Steam SDK libraries are copied to the data PVC for the game container. The pinned published image uses `/arma3`; **upstream's v2 branch uses `/arma3/server` and a different downloader and is not compatible**. Change the image digest only after checking its SteamCMD layout.

Config/mod value changes trigger pod replacement via a ConfigMap checksum. External Secret changes require an explicit `kubectl -n arma3 rollout restart deployment/antistasi-arma3`. Startup/readiness probes check the UDP game socket, not mission correctness; there is no default liveness restart. The pre-stop hook sends SIGINT to Arma, with 120 seconds to exit. This does not replace an in-game campaign save.

## Development and publishing

```sh
nix develop                    # Helm, kubectl, kubeconform, Python, actionlint, gh, jq
nix develop -c bash scripts/check.sh
nix develop -c bash scripts/kubeconform.sh  # fetches standard Kubernetes schemas
nix flake check --print-build-logs         # offline-capable checks once dependencies exist
```

The locked flake supports Linux/macOS on x86_64/aarch64 for tooling; the game image requires Linux amd64. Optionally copy `.envrc.example` to `.envrc` and use direnv. Checks cover rendering, invalid values, persistence, secret references, route schemas, download failure handling, filename normalization, campaign preservation and config generation. No Steam credentials are used by tests. PTY/socket tests cover password and code input, incorrect codes, app approval, detach/reconnect, cached state, cancellation, deadlines, and hidden input. Real account-specific Steam Guard approval and a game/cluster smoke test are still needed for your infrastructure.

GitHub Actions validates pull requests and pushes. On chart changes pushed to `main`, or manual dispatch, **Publish chart** runs the checks, packages the chart and pushes it to `oci://ghcr.io/<owner>/<repository>/charts/arma3` using `GITHUB_TOKEN` with `packages: write`. No PAT, Pages site, chart index or repository secret is needed. Each release uses `Chart.yaml`'s SemVer; **bump it for every chart release**. Existing version tags are skipped, not overwritten. Workflows serialize publication.

GitHub may create the first GHCR package as **private**. After first publication, make the package public in its GitHub package settings for anonymous Helm installs, or use `helm registry login ghcr.io` for private installs. Organization policies must permit Actions to create/write packages. The package path follows the repository automatically in forks; adjust the install URL. The OCI chart contains templates and the bootstrap script, not Steam game or Workshop content.

## Sources

- [Upstream image and configuration](https://github.com/BrettMayson/Arma3Server/tree/master)
- [Antistasi Community beginners / dedicated server guide](https://official-antistasi-community.github.io/A3-Antistasi-Docs/beginners_guide/raw_beginners_guide.html)
- [Antistasi Ultimate Workshop item and Linux save guidance](https://steamcommunity.com/sharedfiles/filedetails/?id=3020755032)
- [Ultimate mission configuration](https://github.com/Antistasi-Ultimate-Community/A3-Antistasi-Ultimate/blob/main/A3A/addons/maps/config.cpp)
- [Gateway API UDP routing](https://gateway-api.sigs.k8s.io/guides/udp-routing/)
