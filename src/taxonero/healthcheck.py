"""Container health check (Dockerfile HEALTHCHECK): asks /healthz on the port and address the dashboard really
listens on, so a DASHBOARD_PORT changed inside the container does not make it "unhealthy"."""

import sys
import urllib.request

from .config import Settings


def main() -> None:
    s = Settings()
    host = "127.0.0.1" if s.dashboard_host in ("", "0.0.0.0", "::") else s.dashboard_host
    if ":" in host:   # IPv6 literal
        host = f"[{host}]"
    try:
        urllib.request.urlopen(f"http://{host}:{s.dashboard_port}/healthz", timeout=4)
    except Exception as e:
        print(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
