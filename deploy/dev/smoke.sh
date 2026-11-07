#!/usr/bin/env bash
# Smoke test for a running studio API: GET /api/v1/projects must answer 200 with a
# JSON list. Exits non-zero (and prints why) otherwise, so it can gate scripts/CI.
#
#   deploy/dev/smoke.sh                                   # http://127.0.0.1:8000
#   STUDIO_BASE_URL=http://studio.digitalizer.stu.k8s.internal deploy/dev/smoke.sh   # k8s, VPN on
set -euo pipefail

base_url="${STUDIO_BASE_URL:-http://127.0.0.1:8000}"
url="${base_url%/}/api/v1/projects"

body="$(curl --fail --silent --show-error --max-time 10 -H 'Accept: application/json' "$url")"

# A list of projects, each with a slug (ProjectOut); python3 is on every dev box.
printf '%s' "$body" | python3 -c '
import json, sys
projects = json.load(sys.stdin)
if not isinstance(projects, list):
    sys.exit(f"expected a JSON list, got {type(projects).__name__}")
slugs = ", ".join(str(p.get("slug", "?")) for p in projects) or "-"
print("OK %d project(s): %s" % (len(projects), slugs))
'
