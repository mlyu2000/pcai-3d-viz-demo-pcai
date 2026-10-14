#!/usr/bin/env bash
# PCAI 3D Viz PoC — build & deploy (run from the Windows host, drives the CS1 bastion).
# Same pipeline as the Styling Demo: scp source to bastion -> docker build/push -> kubectl apply.
#
#   scripts/build-deploy.sh [image] [deploy] [verify] [secrets] [register]
#
#   secrets    create regcred + mlis-key in ns/viz-demo (mlis-key copied from styling-demo)
#   image      build + push registry.ctc.sg.lab:5000/pcai-viz-app:$TAG (default v1)
#   deploy     kubectl apply -f manifests/ (TAG placeholder in app.yaml is substituted)
#   verify     pods, endpoints, in-cluster health + UI, public ingress status
#   register   publish the pcai-3d-viz registration chart to ChartMuseum + apply the
#              EzAppConfig so the app shows up as a tile in the Ezmeral UI
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CS1_DIR="$(cd "$ROOT/../cs1_platform" && pwd)"
SSH="ssh -i $HOME/.ssh/id_ed25519_hermes -o IdentitiesOnly=yes -o BatchMode=yes ml@10.90.2.183"
REG=registry.ctc.sg.lab:5000
NS=viz-demo
TAG=${TAG:-v1}
export KUBECONFIG="$(cygpath -m "$CS1_DIR/kubeconfig-cs1.conf")"
K="$(cygpath -m "$CS1_DIR/kubectl.exe")"

ensure_bastion_ready() {
  $SSH 'test -f /etc/docker/certs.d/registry.ctc.sg.lab:5000/ca.crt' && return 0
  echo "WARNING: registry CA missing on bastion, re-installing"
  $SSH 'sudo mkdir -p /etc/docker/certs.d/registry.ctc.sg.lab:5000'
  echo "  (re-run the Styling Demo's scripts/install-registry-ca.sh if needed)"
}

sync_to_bastion() {
  $SSH 'rm -rf /home/ml/pcai-3d-viz && mkdir -p /home/ml/pcai-3d-viz'
  find "$ROOT/backend" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
  scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
    "$ROOT/backend" ml@10.90.2.183:/home/ml/pcai-3d-viz/backend
  scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
    "$ROOT/frontend" ml@10.90.2.183:/home/ml/pcai-3d-viz/frontend
  # sample_data holds mixed PNG/JPG refs (+ README.md/.gitkeep). scp -r of the
  # whole dir into the (freshly rm -rf'd) base creates base/sample_data/ as a
  # copy (no nesting, since the dest subdir doesn't exist yet). The backend's
  # /api/samples enumerates only ALLOWED_EXT, so README.md/.gitkeep are ignored.
  scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
    "$ROOT/sample_data" ml@10.90.2.183:/home/ml/pcai-3d-viz/
  # geometry model caches (baked into the image; gitignored locally, scp'd from
  # the working tree). Only present when the geometry layer is being shipped.
  if [ -d "$ROOT/models" ]; then
    scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
      "$ROOT/models" ml@10.90.2.183:/home/ml/pcai-3d-viz/
  fi
  # CA bundle (root-level ca/) — ships into the image so the pack endpoint
  # includes the CS1 private CA for Tier 2 LLM TLS.
  if [ -d "$ROOT/ca" ]; then
    scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
      "$ROOT/ca" ml@10.90.2.183:/home/ml/pcai-3d-viz/
  fi
  # .blend sidecar source (Dockerfile + build_blend.py).
  if [ -d "$ROOT/sidecar" ]; then
    scp -r -i "$HOME/.ssh/id_ed25519_hermes" -o IdentitiesOnly=yes \
      "$ROOT/sidecar" ml@10.90.2.183:/home/ml/pcai-3d-viz/
  fi
}

build_image() {
  ensure_bastion_ready
  sync_to_bastion
  $SSH 'cd /home/ml/pcai-3d-viz && \
    docker build -f backend/Dockerfile -t '"$REG"'/pcai-viz-app:'"$TAG"' . && \
    docker push '"$REG"'/pcai-viz-app:'"$TAG"' && \
    docker rmi '"$REG"'/pcai-viz-app:'"$TAG"''
  echo "image pushed: $REG/pcai-viz-app:$TAG"
}

# .blend sidecar: headless Blender (the bpy wheel on python:3.13-slim).
# Built from the same synced repo (context has sidecar/). Separate image so
# the 400MB cp313-only bpy wheel stays out of the main python:3.11 app.
# Run `image` to build BOTH (it calls this last); or `blend-image` for just the
# sidecar after the main image is already built.
build_blend_image() {
  ensure_bastion_ready
  # Make sure the context is synced (no-op if build_image already synced).
  if ! $SSH 'test -f /home/ml/pcai-3d-viz/sidecar/Dockerfile' 2>/dev/null; then
    sync_to_bastion
  fi
  $SSH 'cd /home/ml/pcai-3d-viz && \
    docker build -f sidecar/Dockerfile -t '"$REG"'/pcai-viz-blend:'"$TAG"' . && \
    docker push '"$REG"'/pcai-viz-blend:'"$TAG"' && \
    docker rmi '"$REG"'/pcai-viz-blend:'"$TAG"''
  echo "sidecar image pushed: $REG/pcai-viz-blend:$TAG"
}

deploy() {
  local ROOT_WIN
  ROOT_WIN="$(cygpath -m "$ROOT")"   # native Windows paths for kubectl.exe
  local app_yaml="$ROOT_WIN/manifests/app.yaml"
  # substitute the image tag placeholder (image: AND the APP_VERSION env value)
  local tmp="$ROOT/.app.tagged.yaml"
  sed "s|VIZ_TAG|$TAG|g" "$app_yaml" > "$tmp"
  "$K" apply -f "$ROOT_WIN/manifests/namespace.yaml"
  "$K" apply -f "$ROOT_WIN/manifests/pvc.yaml"
  "$K" apply -f "$(cygpath -m "$tmp")"
  "$K" apply -f "$ROOT_WIN/manifests/virtualservice.yaml"
  rm -f "$tmp"
  echo "deployed (TAG=$TAG) to ns/$NS"
  "$K" -n "$NS" wait --for=condition=ready pod -l app=viz-demo-app --timeout=240s
}

verify() {
  echo "=== pods ===";          "$K" -n "$NS" get pods -o wide
  echo "=== endpoints ===";     "$K" -n "$NS" get endpoints
  echo "=== pvc ===";           "$K" -n "$NS" get pvc
  echo "=== virtualservice ==="; "$K" -n "$NS" get virtualservice
  echo "=== in-cluster health ==="
  "$K" -n "$NS" exec deploy/viz-demo-app -- curl -s http://localhost:8000/api/health; echo
  echo "=== in-cluster UI ==="
  "$K" -n "$NS" exec deploy/viz-demo-app -- curl -s http://localhost:8000/ | head -c 300; echo
  echo "=== public ingress ==="
  curl -s -o /dev/null -w "https://viz-demo.aie.cs1.ctc.sg.lab -> %{http_code}\n" https://viz-demo.aie.cs1.ctc.sg.lab
}

secrets() {
  # regcred (registry pull creds — same source as the Styling Demo)
  $SSH true
  local reg_user reg_pass
  reg_user=$("$K" get secret registry-config-creds -n default -o json | python -c "
import json,sys,base64
d=json.load(sys.stdin)['data']['.dockerconfigjson']
a=json.loads(base64.b64decode(d))['auths']
e=a.get('$REG') or list(a.values())[0]
print(e.get('username','') or base64.b64decode(e['auth']).decode().split(':',1)[0])")
  reg_pass=$("$K" get secret registry-config-creds -n default -o json | python -c "
import json,sys,base64
d=json.load(sys.stdin)['data']['.dockerconfigjson']
a=json.loads(base64.b64decode(d))['auths']
e=a.get('$REG') or list(a.values())[0]
print(e.get('password','') or base64.b64decode(e['auth']).decode().split(':',1)[1])")
  "$K" -n "$NS" create secret docker-registry regcred \
    --docker-server="$REG" --docker-username="$reg_user" --docker-password="$reg_pass" \
    --dry-run=client -o yaml | "$K" apply -f -
  echo "regcred secret set (user=$reg_user)"
  # mlis-key: copy the existing value from styling-demo (same MLIS predictor)
  local mlis_b64
  mlis_b64=$("$K" get secret mlis-key -n styling-demo -o jsonpath='{.data.key}')
  "$K" -n "$NS" create secret generic mlis-key \
    --from-literal="key=$(echo "$mlis_b64" | base64 -d)" \
    --dry-run=client -o yaml | "$K" apply -f -
  echo "mlis-key copied from styling-demo"
}

register() {
  # Register the app with the Ezmeral UI (EzApps tile).
  # 1) package + push the registration chart to ChartMuseum (unauthenticated, local storage)
  # 2) apply the EzAppConfig so the controller installs the chart (creates the
  #    hpe-ezua-labelled registration ConfigMap in ns/viz-demo)
  local CHART_VERSION
  CHART_VERSION=$(grep -E '^version:' "$ROOT/chart/pcai-3d-viz/Chart.yaml" | awk '{print $2}')
  echo "packaging chart pcai-3d-viz-$CHART_VERSION"
  (cd "$ROOT/chart" && rm -f "pcai-3d-viz-$CHART_VERSION.tgz" && helm package pcai-3d-viz)

  echo "pushing to ChartMuseum via port-forward"
  local PF_PORT=18099
  # clean any stale listener on the port (MSYS path translation is disabled here,
  # so taskkill needs single-slash flags)
  local stale
  stale=$(netstat -ano | grep ":${PF_PORT}" | awk '{print $5}' | sort -u | grep -E '^[0-9]+$')
  [ -n "$stale" ] && { echo "killing stale listener(s): $stale"; for p in $stale; do taskkill /F /PID $p 2>/dev/null || true; done; sleep 1; }
  "$K" -n ez-chartmuseum-ns port-forward svc/chartmuseum "${PF_PORT}:8080" &
  local PF_PID=$!
  sleep 5
  curl -sk -F "chart=@$ROOT/chart/pcai-3d-viz-$CHART_VERSION.tgz" "http://127.0.0.1:${PF_PORT}/api/charts"
  echo ""
  kill "$PF_PID" 2>/dev/null || true

  echo "applying EzAppConfig (manifests/ezappconfig.yaml)"
  "$K" apply -f "$ROOT/manifests/ezappconfig.yaml"
  "$K" get ezappconfig pcai-3d-viz-0.1.0 -o wide || true
  echo "done — the app should appear in the Ezmeral UI (dataEngineering category)."
}

case "${1:-}" in
  secrets)   secrets ;;
  image)     build_image && build_blend_image ;;
  blend-image) build_blend_image ;;
  deploy)    deploy ;;
  verify)    verify ;;
  register)  register ;;
  *) echo "usage: $0 {secrets|image|deploy|verify|register}   (TAG=v1 default; TAG=x $0 image deploy)" ;;
esac
