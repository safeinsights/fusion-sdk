#!/bin/sh
# Smoke: the package installed from source imports, and the fake tunnel's /v1/info is reachable.
# Then the SDK's own doctor waits for CHANNEL_UP.
set -eu
python - <<'PY'
import os, sys, time, urllib.request
url = os.environ["FUSION_TUNNEL_ENDPOINT"] + "/v1/info"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["FUSION_TUNNEL_TOKEN"]})
for attempt in range(30):
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print("fake tunnel reachable:", resp.status)
            break
    except Exception as exc:  # noqa: BLE001
        time.sleep(1)
else:
    sys.exit("fake tunnel never became reachable")
PY
python -m safeinsights_fusion doctor --wait
