"""Healthcheck for the MCP servers' streamable-http endpoint.

The endpoint answers a bare GET with 406 (it wants an initialize POST or an
SSE accept header), which urllib raises as HTTPError. Any HTTP response at all
proves the process is serving; only connection failures mean unhealthy.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen(sys.argv[1], timeout=3)
except urllib.error.HTTPError:
    pass
except Exception as exc:
    print(f"unhealthy: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
