"""Reference basic client for the python-default (chart-HPA / TrendAI-supported)
deployment.

In this scenario the V1FS scanner scales on the chart's own CPU/memory HPA, and
your app talks to it through the SDK with a single reused connection — the
standard, simplest integration. (If you need to fan a high-volume client across
many scanner pods without an L7 hop, see the python-KEDA option, which adds the
pull/semaphore dispatcher.)

Config via env: SCANNER_ENDPOINT (e.g. "<nlb-host>:50051" from the stack's SSM
parameter, or "<domain>:443" for ALB), V1FS_API_KEY, SCANNER_TLS (default
false), SCANNER_CA_CERT (path; only for a self-signed ALB cert).
"""
import os
import sys

import amaas.grpc  # visionone-filesecurity (sync API)


def main(paths):
    endpoint = os.environ["SCANNER_ENDPOINT"]         # e.g. k8s-...elb.amazonaws.com:50051
    api_key = os.environ["V1FS_API_KEY"]
    tls = os.environ.get("SCANNER_TLS", "false").lower() == "true"
    ca = os.environ.get("SCANNER_CA_CERT") or None

    # init() ONCE, reuse the handle for every scan (the async/gRPC norm).
    handle = amaas.grpc.init(endpoint, api_key, tls, ca)
    try:
        for p in paths:
            with open(p, "rb") as f:
                data = f.read()
            verdict = amaas.grpc.scan_buffer(handle, data, os.path.basename(p), tags=[])
            print(verdict)
    finally:
        amaas.grpc.quit(handle)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python client_example.py <file> [<file> ...]")
    main(sys.argv[1:])
