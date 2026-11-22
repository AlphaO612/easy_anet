#!/bin/sh
set -eu
base=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = --wizard ]; }; then
    exec python3 "$base/scripts/wizard.py"
fi
case "$*" in
    --access|"--access --plain"|"--plain --access") exec python3 "$base/scripts/wizard.py" "$@";;
    --plain|"--wizard --plain"|"--plain --wizard") exec python3 "$base/scripts/wizard.py" --plain;;
esac
stage_profile=${ANET_PROFILE:-standalone}
mode=${ANET_MODE:-launch}
state=${STATE_DIR:-"${XDG_STATE_HOME:-$HOME/.local/state}/anet"}
env_file=
release=
bundle=
config_only=0
probe_management=0
capacity_probes=0
metrics_panel=0
flow_agent=
join_panel=
bootstrap=0
host=${ANET_HOST:-}
usage() {
  cat <<'USAGE'
install.sh [--profile standalone|webui|anet-easy] [--mode launch|build] [--host HOST]
           [--state ABSOLUTE_PATH] [--env FILE] [--release FILE | --bundle DIR] [--install-docker] [--config]
           [--wizard [--plain] | --access] [--capacity-probes] [--probe-management] [--metrics-panel] [--flow-agent SETTINGS] [--join-panel SETTINGS]
In launch mode set SERVER_IMAGE and NETNS_IMAGE; WebUI also needs AUTH_IMAGE
and WEBUI_IMAGE. Use full image references ending in @sha256:<digest>.
--bundle loads verified local image IDs from DIR/manifest.json and images.tar.
The anet-easy profile requires a provisioned exit credential and an ingress server directory.
--metrics-panel connects WebUI auth to the central monitoring network.
--config initializes state and validates Compose without starting containers.
USAGE
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile) stage_profile=$2; shift;;
        --mode) mode=$2; shift;;
        --state) state=$2; shift;;
        --host) host=$2; shift;;
        --env) env_file=$2; shift;;
        --bundle) bundle=$2; shift;;
        --release) release=$2; shift;;
        --install-docker) bootstrap=1;;
        --config) config_only=1;;
        --capacity-probes) capacity_probes=1;;
        --probe-management) probe_management=1;;
        --metrics-panel) metrics_panel=1;;
        --flow-agent) flow_agent=$2; shift;;
        --join-panel) join_panel=$2; shift;;
        --wizard)
            printf 'Profile [standalone/webui/anet-easy] (%s): ' "$stage_profile"; read -r value
            stage_profile=${value:-$stage_profile}
            printf 'Setup [recommended/expert] (recommended): '; read -r value
            if [ "$value" = expert ]; then
                printf 'Image mode [build/launch] (%s): ' "$mode"; read -r value
                mode=${value:-$mode}
                printf 'Compose environment file: '; read -r env_file
            fi
            if [ "$stage_profile" = anet-easy ] && [ -z "$env_file" ]; then
                printf 'Full-profile environment file (uplink, ingress and ports): '; read -r env_file
            fi
            if [ "$mode" = launch ] && [ -z "$release$bundle" ]; then
                printf 'Offline bundle directory (blank for registry): '; read -r bundle
            fi
            printf 'Public host or IP: '; read -r host;;
        -h|--help) usage; exit 0;;
        *) usage >&2; exit 2;;
    esac
    shift
done
case $stage_profile in standalone|webui|anet-easy) ;; *) usage >&2; exit 2;; esac
init_profile=$stage_profile
if [ "$stage_profile" = anet-easy ]; then
    init_profile=webui
    probe_management=1
    export INGRESS_PATH=chain
fi
if [ "$capacity_probes" = 1 ] && [ "$stage_profile" != anet-easy ]; then
    echo '--capacity-probes requires --profile anet-easy' >&2; exit 2
fi
if [ "$metrics_panel" = 1 ] && [ "$stage_profile" != webui ]; then
    echo '--metrics-panel requires --profile webui' >&2; exit 2
fi
case $mode in build|launch) ;; *) usage >&2; exit 2;; esac
case $state in /*) ;; *) echo '--state requires an absolute path' >&2; exit 2;; esac
[ -n "$host" ] || { echo '--host requires the public DNS name or IP' >&2; exit 2; }
if [ -n "$join_panel" ] && [ "$stage_profile" != standalone ]; then
    echo '--join-panel requires a dedicated standalone node' >&2; exit 2
fi
if [ -n "$flow_agent" ]; then python3 "$base/scripts/install-flow-agent.py" "$flow_agent" --check; fi
if [ "$bootstrap" = 1 ]; then "$base/scripts/rootless-docker.sh"; fi
command -v docker >/dev/null || { echo 'Docker missing: use --install-docker' >&2; exit 1; }
if [ "$config_only" != 1 ]; then
    case "$(docker info --format '{{json .SecurityOptions}}')" in
        *name=rootless*) ;;
        *) echo 'Select a rootless Docker context or run with --install-docker.' >&2; exit 1;;
    esac
    [ -c /dev/net/tun ] || { echo '/dev/net/tun unavailable' >&2; exit 1; }
fi
export STATE_DIR="$state"
if [ -n "$bundle" ]; then
    [ -z "$release" ] && [ "$mode" = launch ] || { echo '--bundle requires launch without --release' >&2; exit 2; }
    references=$(python3 "$base/scripts/bundle.py" load "$bundle" "$stage_profile")
elif [ -n "$release" ]; then
    [ "$mode" = launch ] || { echo '--release is for launch mode' >&2; exit 2; }
    references=$(python3 "$base/scripts/release.py" env "$release" "$stage_profile" --platform linux/amd64)
fi
if [ -n "$bundle$release" ]; then
    while IFS='=' read -r key ref; do
        case "$key" in SERVER_IMAGE|NETNS_IMAGE|AUTH_IMAGE|WEBUI_IMAGE|POSTGRES_IMAGE|CLIENT_IMAGE|INGRESS_IMAGE|PROMETHEUS_IMAGE|BLACKBOX_IMAGE|GRAFANA_IMAGE|DNS_IMAGE) export "$key=$ref";;
            *) echo 'Unexpected key from release manifest' >&2; exit 2;;
        esac
    done <<MANIFEST
$references
MANIFEST
fi
set -- docker compose --project-name "${COMPOSE_PROJECT_NAME:-anet}"
# Put build defaults below both the operator's env file and exported variables.
if [ "$mode" = build ]; then set -- "$@" --env-file "$base/compose/build.env"; fi
if [ -n "$env_file" ]; then set -- "$@" --env-file "$env_file"; else set -- "$@" --env-file /dev/null; fi
if [ "$stage_profile" = anet-easy ] && [ "$capacity_probes" = 0 ]; then
    case $mode in
        build) set -- "$@" -f "$base/compose.full.build.yml";;
        launch) set -- "$@" -f "$base/compose.full.pre-builded.yml";;
    esac
else
    set -- "$@" -f "$base/compose/compose.yaml"
    if [ "$init_profile" = webui ]; then set -- "$@" -f "$base/compose/webui.yaml"; fi
    if [ "$stage_profile" = anet-easy ]; then
        set -- "$@" -f "$base/compose/chain.yaml" -f "$base/../chain/ingress.yaml" -f "$base/compose/full-monitoring.yaml"
    fi
    if [ "$capacity_probes" = 1 ]; then set -- "$@" -f "$base/../monitoring/compose.capacity-source.yaml"; fi
    if [ "$metrics_panel" = 1 ]; then set -- "$@" -f "$base/compose/metrics-panel.yaml"; fi
    if [ "$probe_management" = 1 ]; then
        [ "$init_profile" = webui ] || { echo '--probe-management requires --profile webui' >&2; exit 2; }
        set -- "$@" -f "$base/compose/probe-management.yaml"
    fi
    if [ "$mode" = build ]; then
        if [ "$stage_profile" = anet-easy ]; then
            set -- "$@" -f "$base/compose/full-build.yaml"
        fi
        set -- "$@" -f "$base/compose/build.yaml"
        if [ "$init_profile" = webui ]; then set -- "$@" -f "$base/compose/webui-build.yaml"; fi
    fi
fi
if [ "$mode" = launch ]; then
    if [ -n "$bundle" ]; then
        # Compare all resolved Compose services, including literal third-party image references.
        resolved=$("$@" config --images)
        printf '%s\n' "$resolved" | python3 -c '
import sys
allowed = {line.split("=", 1)[1] for line in sys.argv[1].splitlines()}
refs = set(sys.stdin.read().splitlines())
if not refs or not refs <= allowed: sys.exit("Compose resolved an image outside the bundle")' "$references"
    else
        "$@" config --images | python3 -c '
import re, sys
for ref in sys.stdin:
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", ref.strip()):
        sys.exit("Expected digest-pinned image: " + ref.strip())'
    fi
fi
if [ "$mode" = build ] && [ "$stage_profile" = anet-easy ]; then
    "$@" config --format json | python3 -c '
import json,re,sys
ref=json.load(sys.stdin)["services"]["ingress"]["build"]["args"]["XRAY_IMAGE"]
if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}",ref): sys.exit("Expected digest-pinned XRAY_IMAGE")'
fi
# Resolve the published port from the same Compose environment as the stack.
network_config=$("$@" config --format json | python3 "$base/scripts/validate-network.py" "$stage_profile")
port=${network_config% *}
subnet=${network_config#* }
python3 "$base/scripts/init-state.py" "$state" "$host" "$port" "$init_profile" --subnet "$subnet"
exec 9>>"$state/.installation.lock"
flock -x 9
if [ "$probe_management" = 1 ]; then python3 "$base/scripts/init-probe-signing.py" "$state"; fi
if [ "$stage_profile" = anet-easy ]; then python3 "$base/scripts/init-monitoring.py" "$state"; fi
"$@" config --quiet
if [ "$config_only" = 1 ]; then exit 0; fi
if [ "$mode" = build ]; then
    # Bake may schedule independent targets together even when Compose operations are serialized.
    build_services=$("$@" config --format json | python3 -c '
import json, sys
for name, service in json.load(sys.stdin)["services"].items():
    if "build" in service:
        print(name)')
    for service in $build_services; do "$@" build "$service"; done
elif [ -z "$bundle" ]; then
    "$@" pull
fi
if [ -n "$join_panel" ]; then python3 "$base/scripts/join-panel.py" "$state" "$join_panel"; fi
if [ -n "$bundle" ]; then export ANET_OFFLINE_BUNDLE=1; fi
if [ "$init_profile" = webui ]; then python3 "$base/scripts/register-local-node.py" "$state" "$@"; fi
if [ "$stage_profile" = anet-easy ]; then python3 "$base/scripts/init-monitoring.py" "$state"; fi
if [ -n "$bundle" ]; then
    "$@" up --no-build --pull never -d --wait --wait-timeout 180
else
    "$@" up --no-build -d --wait --wait-timeout 180
fi
if [ "$stage_profile" = anet-easy ]; then
    python3 "$base/scripts/init-monitoring.py" --check "$("$@" port prometheus 9090)" "$("$@" port grafana 3000)"
fi
if [ -n "$join_panel" ] && [ -f "$state/.enrollment-pending" ]; then
    "$@" up -d --no-build --no-deps --pull never --force-recreate --wait server
    rm -f "$state/.enrollment-pending"
fi
if [ "$stage_profile" = standalone ]; then
    printf 'Client profile: %s/client.toml\n' "$state"
else
    printf 'Server public key: %s/server-public-key\n' "$state"
    printf 'WebUI login: admin. Initial password: %s/admin-password\n' "$state"
fi

if [ -n "$flow_agent" ]; then python3 "$base/scripts/install-flow-agent.py" "$flow_agent"; fi
