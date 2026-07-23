"""Client-side "pull" load balancer over the V1FS scanner pods (Python).

Mirrors reference/java-KEDA. Discovers the live, HEALTHY scanner pod IPs from
the NLB target group (target-type=ip) via the ELB DescribeTargetHealth API,
holds one gRPC handle (== one reused connection) per pod, and hands each scan
to the pod with the most free capacity (least-outstanding).

The NLB is used ONLY as a discovery registry — scans connect DIRECTLY to pod
IPs, so there is no load balancer in the scan path (no L4 pinning, no L7
latency). A background thread re-reads the target group every 20s so the pool
tracks the KEDA-scaled fleet and drains pods on scale-down.

Uses the SYNCHRONOUS V1FS SDK with a thread pool: each worker thread does a
blocking scan on a per-pod handle, so N workers = N in-flight scans across many
connections — the pull/competing-consumers pattern.
"""
import threading
import time

import amaas.grpc  # visionone-filesecurity (sync API)
import boto3


class _PodClient:
    """One scanner pod: its SDK handle and a capacity semaphore ('free slots')."""

    def __init__(self, addr, handle, capacity):
        self.addr = addr                    # "10.2.x.y:50051"
        self.handle = handle
        self.sem = threading.BoundedSemaphore(capacity)
        self.draining = False


class NoCapacity(Exception):
    """Every scanner pod is saturated — caller should nack for redelivery."""


class ScannerPool:
    def __init__(self, target_group_arn, api_key, per_pod_capacity=30,
                 tls=False, ca_cert=None, region="us-east-1", refresh_secs=20):
        self._tg = target_group_arn
        self._api_key = api_key
        self._cap = per_pod_capacity
        self._tls = tls
        self._ca = ca_cert
        self._elb = boto3.client("elbv2", region_name=region)
        self._pods = {}                     # addr -> _PodClient
        self._lock = threading.Lock()
        self._reconcile()                   # initial roster before serving
        t = threading.Thread(target=self._refresh_loop, args=(refresh_secs,), daemon=True)
        t.start()

    def total_capacity(self):
        with self._lock:
            return len(self._pods) * self._cap

    # -- discovery -----------------------------------------------------------
    def _healthy_addrs(self):
        resp = self._elb.describe_target_health(TargetGroupArn=self._tg)
        return {
            f"{d['Target']['Id']}:{d['Target']['Port']}"
            for d in resp["TargetHealthDescriptions"]
            if d["TargetHealth"]["State"] == "healthy"
        }

    def _reconcile(self):
        healthy = self._healthy_addrs()
        with self._lock:
            for addr in healthy:                        # add new pods
                if addr not in self._pods:
                    handle = amaas.grpc.init(addr, self._api_key, self._tls, self._ca)
                    self._pods[addr] = _PodClient(addr, handle, self._cap)
            for addr in list(self._pods):               # drain + close departed pods
                if addr not in healthy:
                    pc = self._pods.pop(addr)
                    pc.draining = True
                    self._close(pc)

    def _refresh_loop(self, secs):
        while True:
            time.sleep(secs)
            try:
                self._reconcile()
            except Exception:                           # never let discovery kill the loop
                pass

    # -- dispatch ------------------------------------------------------------
    def scan(self, data, uid, wait_secs=60):
        """Scan on the least-busy pod; retry on another pod on error.

        Raises NoCapacity if no pod frees a slot within wait_secs (nack so the
        source queue redelivers).
        """
        last = None
        for _ in range(3):
            pc = self._acquire_least_busy(wait_secs)
            if pc is None:
                raise NoCapacity(f"no scanner capacity within {wait_secs}s")
            try:
                # scan_buffer signature varies by SDK version — adjust as needed.
                return amaas.grpc.scan_buffer(pc.handle, data, uid, tags=[])
            except Exception as e:                      # pod-level failure → try another
                last = e
                if pc.draining:
                    with self._lock:
                        self._pods.pop(pc.addr, None)
            finally:
                pc.sem.release()
        raise last or RuntimeError("scan failed after retries")

    def _acquire_least_busy(self, wait_secs):
        deadline = time.monotonic() + max(0, wait_secs)
        while True:
            best, best_free = None, -1
            with self._lock:
                for pc in self._pods.values():
                    if pc.draining:
                        continue
                    # BoundedSemaphore._value is the free-slot count.
                    free = pc.sem._value
                    if free > best_free:
                        best, best_free = pc, free
            if best is not None and best.sem.acquire(timeout=0.2):
                return best
            if time.monotonic() >= deadline:
                return None

    # -- lifecycle -----------------------------------------------------------
    @staticmethod
    def _close(pc):
        try:
            amaas.grpc.quit(pc.handle)
        except Exception:
            pass

    def close(self):
        with self._lock:
            for pc in self._pods.values():
                self._close(pc)
            self._pods.clear()
