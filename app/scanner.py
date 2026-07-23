"""S3 → SQS → V1FS scanning service.

One async event loop drives everything:

  _poll_loop            long-polls SQS, spawns one task per message
    _process_message    heartbeat + parse (S3 or EventBridge shape) + ack/retry
      _process_record   download → scan via gRPC → route → finalize + audit

Routing: malicious → quarantine · decompression-limit → review (or
quarantine+tags when review is off) · clean → clean. Incompletely-scanned
files are never marked clean.

Support loops (started in start(), stopped in _shutdown()):
  health server         /healthz + /readyz on :8080 for kubelet probes
  audit flusher         batches scan results to CloudWatch Logs
  reconciliation        re-queues ingest objects that never got scanned

Concurrency is bounded by a semaphore (MAX_CONCURRENT_SCANS); all
configuration comes from environment variables via config.load_config().
"""
import asyncio
import json
import logging
import random
import re
import signal
import socket
import time
import urllib.parse
from contextlib import AsyncExitStack

import amaas.grpc.aio
from aiobotocore.session import AioSession
from botocore.exceptions import ClientError

from config import load_config

logger = logging.getLogger("scanner")

# V1FS SDK foundErrors names indicating decompression limits were exceeded.
# Files with these errors return scanResult=0 (clean) but were not fully inspected.
# The SDK returns these in the "foundErrors" array with "name" and "description" fields.
DECOMPRESSION_ERROR_NAMES = frozenset({
    "ATSE_ZIP_RATIO_ERR",       # Compression ratio exceeded (ATSE -71)
    "ATSE_MAXDECOM_ERR",        # Nesting depth exceeded (ATSE -78)
    "ATSE_ZIP_FILE_COUNT_ERR",  # File count exceeded (ATSE -69)
    "ATSE_EXTRACT_TOO_BIG_ERR", # Decompressed size exceeded (ATSE -76)
})


class ByteBudget:
    """Async semaphore over a byte budget.

    Gates in-memory work so the total bytes held across concurrent scans stays
    under a limit, protecting the pod from OOM when many large files arrive at
    once — a bound the plain count semaphore (MAX_CONCURRENT_SCANS) can't give,
    since 50 × 500 MB would dwarf any reasonable pod. A request larger than the
    whole budget is clamped so it still runs (alone, when it reaches the front).
    A total of 0 disables gating.
    """

    def __init__(self, total: int) -> None:
        self._total = total
        self._available = total
        self._cond = asyncio.Condition()

    async def acquire(self, n: int) -> int:
        if self._total <= 0:
            return 0
        n = max(0, min(n, self._total))
        async with self._cond:
            while self._available < n:
                await self._cond.wait()
            self._available -= n
        return n

    async def release(self, n: int) -> None:
        if self._total <= 0 or n <= 0:
            return
        async with self._cond:
            self._available += n
            self._cond.notify_all()


_DELETE_STOP = object()


class DeleteBatcher:
    """Coalesces SQS message deletions into DeleteMessageBatch calls.

    Message processors hand receipt handles here instead of calling
    DeleteMessage per message; a background loop flushes up to 10 at a time
    (the SQS batch limit) once that many accumulate or a short window elapses.
    Cuts SQS delete API calls by up to 10x at load.
    """

    MAX_BATCH = 10

    def __init__(self, sqs_client, queue_url: str, flush_interval: float = 0.2) -> None:
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def add(self, receipt_handle: str) -> None:
        await self._queue.put(receipt_handle)

    async def stop(self) -> None:
        """Flush everything enqueued so far, then stop. Safe because all
        message producers have finished before this is called at shutdown."""
        if self._task is None:
            return
        await self._queue.put(_DELETE_STOP)
        await self._task

    async def _run(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            first = await self._queue.get()
            if first is _DELETE_STOP:
                return
            batch = [first]
            deadline = loop.time() + self._flush_interval
            while len(batch) < self.MAX_BATCH:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if item is _DELETE_STOP:
                    await self._flush(batch)
                    return
                batch.append(item)
            await self._flush(batch)

    async def _flush(self, batch: list) -> None:
        if not batch:
            return
        try:
            resp = await self._sqs.delete_message_batch(
                QueueUrl=self._queue_url,
                Entries=[
                    {"Id": str(i), "ReceiptHandle": h} for i, h in enumerate(batch)
                ],
            )
            failed = resp.get("Failed", [])
            if failed:
                logger.warning(
                    "SQS batch delete: %d of %d failed; those messages reappear "
                    "after the visibility timeout and are re-scanned",
                    len(failed), len(batch),
                )
        except Exception:
            logger.warning(
                "SQS batch delete failed for %d messages", len(batch), exc_info=True
            )


class NoCapacity(Exception):
    """Every scanner pod is saturated — leave the message for SQS redelivery."""


class _Pod:
    """One scanner pod: its async SDK handle and an in-flight counter."""

    __slots__ = ("addr", "handle", "capacity", "inflight", "draining")

    def __init__(self, addr, handle, capacity):
        self.addr = addr                # "10.2.x.y:50051"
        self.handle = handle
        self.capacity = capacity
        self.inflight = 0
        self.draining = False


class AsyncPodPool:
    """Client-side pull dispatcher over the V1FS scanner pods (async).

    DISPATCH_MODE=pull: discover the live, HEALTHY scanner pod IPs from the NLB
    target group (target-type=ip) via the ELB DescribeTargetHealth API, hold one
    async gRPC handle (== one reused connection) per pod, and hand each scan to
    the pod with the most free capacity (least-outstanding). The NLB is only a
    discovery registry — scans connect DIRECTLY to pod IPs, so no load balancer
    sits in the scan path (no L4 pinning, no L7 latency). A background loop
    re-reads the target group every POD_REFRESH_SECS so the pool tracks the
    KEDA-scaled fleet and drains pods on scale-down.

    Single event loop, so no locks: the (inflight < capacity) check and the
    increment happen without an intervening await, and reconcile mutates the
    roster only between scans.
    """

    def __init__(self, config, api_key, session):
        self._cfg = config
        self._api_key = api_key
        self._session = session
        self._pods: dict[str, _Pod] = {}
        self._elb = None
        self._refresh_task = None
        self._stop = asyncio.Event()

    async def start(self, exit_stack) -> None:
        self._elb = await exit_stack.enter_async_context(
            self._session.create_client(
                "elasticloadbalancingv2", region_name=self._cfg.aws_region
            )
        )
        await self._reconcile()   # seed the roster before serving
        logger.info(
            "Pull dispatcher started — %d scanner pod(s) discovered from target group",
            len(self._pods),
        )
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _healthy_addrs(self) -> set[str]:
        resp = await self._elb.describe_target_health(
            TargetGroupArn=self._cfg.scanner_target_group_arn
        )
        return {
            f"{d['Target']['Id']}:{d['Target']['Port']}"
            for d in resp["TargetHealthDescriptions"]
            if d["TargetHealth"]["State"] == "healthy"
        }

    async def _reconcile(self) -> None:
        healthy = await self._healthy_addrs()
        for addr in healthy:
            if addr not in self._pods:
                # init is synchronous — do not await. Pull mode connects to raw
                # pod IPs, so it is plaintext (a cert SAN can't match a pod IP);
                # TLS/ALB use clusterip mode instead.
                handle = amaas.grpc.aio.init(
                    addr, self._api_key,
                    self._cfg.v1fs_tls_enabled,
                    self._cfg.v1fs_ca_cert or None,
                )
                self._pods[addr] = _Pod(addr, handle, self._cfg.per_pod_capacity)
        for addr in list(self._pods):
            if addr not in healthy:
                pod = self._pods.pop(addr)
                pod.draining = True
                await self._close(pod)

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.pod_refresh_secs)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._reconcile()
            except Exception:
                logger.warning("Pod discovery refresh failed; keeping current roster", exc_info=True)

    async def scan(self, data: bytes, uid: str, pml: bool, tags: list) -> str:
        deadline = asyncio.get_running_loop().time() + 60
        last_exc = None
        for _ in range(3):
            pod = await self._acquire_least_busy(deadline)
            if pod is None:
                raise NoCapacity("no scanner pod capacity within 60s")
            try:
                return await amaas.grpc.aio.scan_buffer(pod.handle, data, uid, pml=pml, tags=tags)
            except Exception as exc:               # pod-level failure → try another pod
                last_exc = exc
                if pod.draining:
                    self._pods.pop(pod.addr, None)
            finally:
                pod.inflight -= 1
        raise last_exc or RuntimeError("scan failed after retries")

    async def _acquire_least_busy(self, deadline: float):
        while True:
            best = None
            for pod in self._pods.values():
                if pod.draining or pod.inflight >= pod.capacity:
                    continue
                if best is None or pod.inflight < best.inflight:
                    best = pod
            if best is not None:
                best.inflight += 1            # atomic with the check (no await between)
                return best
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    @staticmethod
    async def _close(pod: _Pod) -> None:
        try:
            await amaas.grpc.aio.quit(pod.handle)
        except Exception:
            pass

    async def close(self) -> None:
        self._stop.set()
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        for pod in list(self._pods.values()):
            await self._close(pod)
        self._pods.clear()


class ScannerApp:
    def __init__(self, config) -> None:
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.semaphore = asyncio.Semaphore(config.max_concurrent_scans)
        self.in_flight: set[asyncio.Task] = set()
        self.max_file_size = config.max_file_size_mb * 1024 * 1024
        # Memory guard: bound total downloaded bytes held across concurrent
        # scans. Floor at one max-size file so a single large file never
        # deadlocks against its own budget.
        self._byte_budget = ByteBudget(
            max(config.max_inflight_bytes, self.max_file_size)
        )
        self._delete_batcher: DeleteBatcher | None = None
        self.scan_handle = None
        self.session = AioSession()
        self._exit_stack = AsyncExitStack()
        self._pool = None            # AsyncPodPool when DISPATCH_MODE=pull
        self._consecutive_errors = 0
        self._ready = False
        self._audit_queue: asyncio.Queue = asyncio.Queue(maxsize=config.audit_queue_max_size)
        self._health_server = None
        self._audit_task = None
        self._reconciliation_task = None

    async def start(self) -> None:
        self.s3_client = await self._exit_stack.enter_async_context(
            self.session.create_client("s3", region_name=self.config.aws_region)
        )
        self.sqs_client = await self._exit_stack.enter_async_context(
            self.session.create_client("sqs", region_name=self.config.aws_region)
        )
        if self.config.audit_log_group:
            self.logs_client = await self._exit_stack.enter_async_context(
                self.session.create_client("logs", region_name=self.config.aws_region)
            )

        logger.info("Retrieving V1FS API key from Secrets Manager")
        # Async client — a blocking boto3 call here would stall the event loop
        # (and the readiness probe) if Secrets Manager is slow at startup.
        async with self.session.create_client(
            "secretsmanager", region_name=self.config.aws_region
        ) as sm_client:
            resp = await sm_client.get_secret_value(
                SecretId=self.config.v1fs_api_key_secret_arn
            )
        api_key = resp["SecretString"]

        if self.config.dispatch_mode == "pull":
            logger.info(
                "Dispatch mode: pull — discovering scanner pods from target group %s",
                self.config.scanner_target_group_arn,
            )
            self._pool = AsyncPodPool(self.config, api_key, self.session)
            await self._pool.start(self._exit_stack)
        else:
            logger.info(
                "Dispatch mode: clusterip — initializing V1FS async gRPC handle at %s (tls=%s, ca_cert=%s)",
                self.config.v1fs_server_addr,
                self.config.v1fs_tls_enabled,
                self.config.v1fs_ca_cert or "system",
            )
            # init is synchronous - do not await. The SDK's ca_cert arg is the
            # "Bring Your Own Certificate" path: pass a PEM to trust (self-signed
            # ALB cert), or None to use the system trust store. There is no
            # skip-verify option — a self-signed cert MUST be supplied here.
            self.scan_handle = amaas.grpc.aio.init(
                self.config.v1fs_server_addr,
                api_key,
                self.config.v1fs_tls_enabled,
                self.config.v1fs_ca_cert or None,
            )

        self._delete_batcher = DeleteBatcher(self.sqs_client, self.config.sqs_queue_url)
        self._delete_batcher.start()

        self._ready = True
        await self._start_health_server()
        if self.config.audit_log_group:
            self._audit_task = asyncio.create_task(self._audit_flush_loop())

        logger.info(
            "Scanner started — polling %s (concurrency=%d, pml=%s)",
            self.config.sqs_queue_url,
            self.config.max_concurrent_scans,
            self.config.pml_enabled,
        )
        if self.config.reconciliation_enabled:
            self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())
            logger.info(
                "Reconciliation enabled — monitoring s3://%s every %ds for objects older than %ds",
                self.config.reconciliation_bucket,
                self.config.reconciliation_interval,
                self.config.reconciliation_age_threshold,
            )

        try:
            await self._poll_loop()
        finally:
            await self._shutdown()

    async def _poll_loop(self) -> None:
        while not self.shutdown_event.is_set():
            # Backpressure: pause polling when too many tasks are in-flight.
            # 2x multiplier avoids pausing too aggressively while the semaphore
            # controls actual concurrency — this just limits the pending queue.
            if len(self.in_flight) >= self.config.max_concurrent_scans * 2:
                await asyncio.sleep(0.1)
                continue

            try:
                resp = await self.sqs_client.receive_message(
                    QueueUrl=self.config.sqs_queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=20,
                    AttributeNames=["ApproximateReceiveCount"],
                )
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                delay = min(2 ** self._consecutive_errors, 60) + random.uniform(0, 1)
                logger.exception("SQS receive_message error, retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue

            for msg in resp.get("Messages", []):
                task = asyncio.create_task(self._guarded_process(msg))
                self.in_flight.add(task)
                task.add_done_callback(self.in_flight.discard)

    async def _guarded_process(self, message: dict) -> None:
        async with self.semaphore:
            await self._process_message(message)

    async def _process_message(self, message: dict) -> None:
        message_id = message.get("MessageId", "unknown")
        try:
            receipt_handle = message["ReceiptHandle"]
        except KeyError:
            logger.error("Missing ReceiptHandle in SQS message [msg=%s]", message_id)
            return

        heartbeat_task = asyncio.create_task(
            self._extend_visibility(receipt_handle)
        )
        try:
            body = json.loads(message["Body"])
            records = self._extract_records(body)
            if not records:
                # s3:TestEvent, non-Object-Created EventBridge event, or empty — discard
                logger.info("No processable records in message %s, deleting", message_id)
                await self._delete_batcher.add(receipt_handle)
                return

            all_succeeded = True
            for record in records:
                try:
                    await self._process_record(record, message_id)
                except Exception:
                    logger.exception(
                        "Failed processing record s3://%s/%s [msg=%s]",
                        record.get("bucket", "?"), record.get("key", "?"), message_id,
                    )
                    all_succeeded = False

            if all_succeeded:
                await self._delete_batcher.add(receipt_handle)
            else:
                logger.warning(
                    "One or more records failed in message %s — shortening visibility for fast retry",
                    message_id,
                )
                await self._shorten_visibility(receipt_handle)

        except json.JSONDecodeError:
            logger.exception("Malformed message body [msg=%s], deleting", message_id)
            await self._delete_batcher.add(receipt_handle)
        except Exception:
            logger.exception("Failed processing message %s — shortening visibility for fast retry", message_id)
            await self._shorten_visibility(receipt_handle)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _extract_records(body: dict) -> list[dict]:
        """Normalize S3 event notifications and EventBridge S3 events into
        [{bucket, key, size}] records.

        Two message shapes arrive on the queue:
        - S3 event notification (stack-created bucket → SQS directly):
          body["Records"][*].s3.bucket.name / .object.key — keys are
          form-encoded (spaces as '+'), so unquote_plus is required.
        - EventBridge "Object Created" (existing user bucket → EventBridge
          rule → SQS): body["detail"].bucket.name / .object.key — keys are
          raw, NOT URL-encoded. Decoding them would corrupt keys containing
          literal '+' or '%' characters.
        """
        records = []
        if "Records" in body:
            for record in body.get("Records", []):
                s3_data = record.get("s3", {})
                bucket = s3_data.get("bucket", {}).get("name")
                key_encoded = s3_data.get("object", {}).get("key")
                if not bucket or not key_encoded:
                    logger.error("Malformed S3 record (missing bucket/key)")
                    continue
                records.append({
                    "bucket": bucket,
                    "key": urllib.parse.unquote_plus(key_encoded),
                    "size": s3_data.get("object", {}).get("size", 0),
                })
        elif body.get("detail-type") == "Object Created":
            detail = body.get("detail", {})
            bucket = detail.get("bucket", {}).get("name")
            key = detail.get("object", {}).get("key")
            if not bucket or not key:
                logger.error("Malformed EventBridge S3 event (missing bucket/key)")
            else:
                records.append({
                    "bucket": bucket,
                    "key": key,  # EventBridge keys are raw — no decoding
                    "size": detail.get("object", {}).get("size", 0),
                })
        return records

    async def _scan(self, data: bytes, uid: str) -> str:
        """Scan a buffer, dispatching per DISPATCH_MODE.

        clusterip: one shared handle to the in-cluster Service (the Service
        spreads connections across pods). pull: hand to the least-busy pod via
        the discovery pool. Same result JSON either way, so routing is
        dispatch-agnostic.
        """
        if self._pool is not None:
            return await self._pool.scan(
                data, uid, pml=self.config.pml_enabled, tags=["S3-Scan"]
            )
        return await amaas.grpc.aio.scan_buffer(
            self.scan_handle, data, uid, pml=self.config.pml_enabled, tags=["S3-Scan"]
        )

    async def _process_record(self, record: dict, message_id: str) -> None:
        bucket = record["bucket"]
        key = record["key"]
        size = record["size"]

        logger.info(
            "Processing s3://%s/%s (%d bytes) [msg=%s]",
            bucket, key, size, message_id,
        )

        if self.max_file_size and size > self.max_file_size:
            if self.config.review_routing_enabled:
                dest_bucket = self.config.s3_review_bucket
                tag = "S3-Review-Oversize"
                verdict = "review"
                logger.warning(
                    "OVERSIZE REVIEW: s3://%s/%s (%d > %d bytes), "
                    "routing to review bucket via server-side copy",
                    bucket, key, size, self.max_file_size,
                )
            else:
                dest_bucket = self.config.s3_quarantine_bucket
                tag = "S3-Oversize"
                verdict = "oversize"
                logger.error(
                    "OVERSIZE: s3://%s/%s (%d > %d bytes), "
                    "moving to quarantine via server-side copy",
                    bucket, key, size, self.max_file_size,
                )
            await self.s3_client.copy_object(
                Bucket=dest_bucket,
                Key=key,
                CopySource={"Bucket": bucket, "Key": key},
                Tagging=f"ScanResult={tag}",
                TaggingDirective="REPLACE",
            )
            await self._finalize_source(bucket, key, {"ScanResult": tag})
            self._enqueue_audit(key, size, verdict, {}, 0, message_id)
            return

        # Reserve the memory budget before pulling the file into RAM. This
        # bounds total in-flight bytes across concurrent scans (on top of the
        # count semaphore), so a burst of large files can't OOM the pod.
        reserved = await self._byte_budget.acquire(size)

        # Download into memory
        try:
            file_bytes = await self._download(bucket, key)
        except BaseException as exc:
            await self._byte_budget.release(reserved)
            if isinstance(exc, ClientError) and \
                    exc.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(
                    "Object s3://%s/%s no longer exists, skipping",
                    bucket, key,
                )
                return
            raise

        # Scan
        try:
            scan_start = time.monotonic()
            result_json = await self._scan(file_bytes, key)
            scan_duration_ms = int((time.monotonic() - scan_start) * 1000)
            result = json.loads(result_json)
            is_malicious = result.get("scanResult", 0) > 0
            decompression_errors = self._get_decompression_errors(result)

            # Route: malicious → quarantine; decompression errors → review
            # (or quarantine with explanatory tags when review is disabled —
            # these files were NOT fully inspected and must not be marked clean);
            # clean → clean
            tags = {}
            if is_malicious:
                dest_bucket = self.config.s3_quarantine_bucket
                verdict = "malicious"
                tags["ScanResult"] = "S3-Malware"
                malware_names = [m.get("malwareName", "") for m in result.get("foundMalwares", [])]
                logger.warning(
                    "MALICIOUS: s3://%s/%s → s3://%s/%s sha256=%s malware=%s",
                    bucket, key, dest_bucket, key,
                    result.get("fileSHA256", "unknown"),
                    malware_names,
                )
            elif decompression_errors and self.config.review_routing_enabled:
                dest_bucket = self.config.s3_review_bucket
                verdict = "review"
                tags["ScanResult"] = "S3-Review"
                logger.warning(
                    "REVIEW: s3://%s/%s → s3://%s/%s (decompression limit errors: %s)",
                    bucket, key, dest_bucket, key, decompression_errors,
                )
            elif decompression_errors:
                dest_bucket = self.config.s3_quarantine_bucket
                verdict = "quarantined-decompression-limit"
                tags["ScanResult"] = "S3-DecompressionLimit"
                # Dedupe and join with '-' (a comma is NOT a legal S3 tag-value
                # character; a multi-error file would otherwise fail to upload).
                tags["ScanErrors"] = "-".join(dict.fromkeys(decompression_errors))
                logger.warning(
                    "DECOMPRESSION LIMIT: s3://%s/%s → s3://%s/%s (errors: %s, "
                    "review pipeline disabled — quarantining incompletely-scanned file)",
                    bucket, key, dest_bucket, key, decompression_errors,
                )
            else:
                dest_bucket = None            # clean files are left in place
                verdict = "clean"
                tags["ScanResult"] = "S3-Clean"
                logger.info("CLEAN: s3://%s/%s (tagged in place)", bucket, key)

            if dest_bucket is None:
                # Clean: leave the file where it is; just tag the source with
                # the verdict. Nothing is moved, copied, or deleted.
                await self._tag_source(bucket, key, tags)
            else:
                # Not fully clean (malicious / decompression-limit / review):
                # move to the verdict bucket, then finalize the source (deleted
                # when it's the stack-owned ingest bucket, tagged-in-place when
                # it's a user's own bucket — DELETE_SOURCE_ENABLED).
                await self._upload(dest_bucket, key, file_bytes, tags)
                await self._finalize_source(bucket, key, tags)
            self._enqueue_audit(key, size, verdict, result, scan_duration_ms, message_id)
        finally:
            del file_bytes  # Explicit cleanup of large buffer
            await self._byte_budget.release(reserved)

    @staticmethod
    def _get_decompression_errors(result: dict) -> list[str]:
        """Return decompression limit error names from scan result, if any."""
        found_errors = result.get("foundErrors", [])
        return [
            e.get("name", "")
            for e in found_errors
            if e.get("name", "") in DECOMPRESSION_ERROR_NAMES
        ]

    async def _shorten_visibility(self, receipt_handle: str, timeout: int = 30) -> None:
        """Shorten message visibility timeout for fast retry on transient failures."""
        try:
            await self.sqs_client.change_message_visibility(
                QueueUrl=self.config.sqs_queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=timeout,
            )
        except Exception:
            logger.debug("Failed to shorten visibility timeout", exc_info=True)

    async def _extend_visibility(self, receipt_handle: str, interval: int | None = None) -> None:
        if interval is None:
            interval = max(self.config.sqs_visibility_timeout - 60, 30)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sqs_client.change_message_visibility(
                    QueueUrl=self.config.sqs_queue_url,
                    ReceiptHandle=receipt_handle,
                    VisibilityTimeout=self.config.sqs_visibility_timeout,
                )
            except Exception:
                logger.warning("Failed to extend visibility", exc_info=True)

    async def _download(self, bucket: str, key: str) -> bytes:
        resp = await self.s3_client.get_object(Bucket=bucket, Key=key)
        async with resp["Body"] as stream:
            return await stream.read()

    # S3 object-tag values allow letters, numbers, spaces, and + - = . _ : / @
    _TAG_DISALLOWED = re.compile(r"[^\w \-=.:/@+]", re.UNICODE)

    @classmethod
    def _safe_tag(cls, value: str) -> str:
        """Coerce a value into a legal S3 tag value (max 256 chars).

        Defense-in-depth: a tag value with a disallowed character (e.g. a comma)
        makes put_object raise InvalidTag, which would fail the whole scan and
        poison-loop the message. Never let tag content break routing.
        """
        return cls._TAG_DISALLOWED.sub("_", value)[:256]

    async def _upload(self, bucket: str, key: str, data: bytes, tags: dict | None = None) -> None:
        kwargs = {"Bucket": bucket, "Key": key, "Body": data}
        if tags:
            kwargs["Tagging"] = urllib.parse.urlencode(
                {k: self._safe_tag(v) for k, v in tags.items()}
            )
        await self.s3_client.put_object(**kwargs)

    async def _delete_object(self, bucket: str, key: str) -> None:
        await self.s3_client.delete_object(Bucket=bucket, Key=key)

    async def _tag_source(self, bucket: str, key: str, tags: dict) -> None:
        """Tag the source object in place with the verdict (no move, no delete)."""
        await self.s3_client.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={"TagSet": [{"Key": k, "Value": self._safe_tag(v)} for k, v in tags.items()]},
        )

    async def _finalize_source(self, bucket: str, key: str, tags: dict) -> None:
        """Finalize the source of a MOVED (not-clean) file.

        Default (stack-owned ingest bucket): delete the source — the verdict
        bucket now holds the file. Existing-user-bucket mode
        (DELETE_SOURCE_ENABLED=false): never delete a user's object;
        tag it with the verdict instead so the result is visible in place.
        """
        if self.config.delete_source_enabled:
            await self._delete_object(bucket, key)
        else:
            await self._tag_source(bucket, key, tags)

    # --- Health Server ---

    async def _start_health_server(self) -> None:
        self._health_server = await asyncio.start_server(
            self._handle_health_request, "0.0.0.0", self.config.health_port
        )
        logger.info("Health server listening on port %d", self.config.health_port)

    async def _handle_health_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=5)
            parts = data.decode("utf-8", errors="replace").split(" ")
            path = parts[1] if len(parts) > 1 else "/"
            if path == "/healthz":
                code, reason, body = 200, "OK", "ok"
            elif path == "/readyz":
                if self._ready:
                    code, reason, body = 200, "OK", "ready"
                else:
                    code, reason, body = 503, "Service Unavailable", "not ready"
            else:
                code, reason, body = 404, "Not Found", "not found"
            resp = f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
            writer.write(resp.encode())
            await writer.drain()
        except asyncio.TimeoutError:
            pass  # Expected: client timeout
        except Exception:
            logger.debug("Health request handler error", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # --- Audit Trail ---

    def _enqueue_audit(self, key: str, size: int, verdict: str, result: dict, scan_duration_ms: int, message_id: str) -> None:
        if not self.config.audit_log_group:
            return
        entry = {
            "timestamp": time.time(),
            "file": key,
            "size": size,
            "verdict": verdict,
            "scanResult": result.get("scanResult", -1),
            "sha256": result.get("fileSHA256", ""),
            "malware": [m.get("malwareName", "") for m in result.get("foundMalwares", [])],
            "foundErrors": [e.get("name", "") for e in result.get("foundErrors", [])],
            "scanId": result.get("scanId", ""),
            "scannerVersion": result.get("scannerVersion", ""),
            "fileSHA1": result.get("fileSHA1", ""),
            "scanDurationMs": scan_duration_ms,
            "pod": socket.gethostname(),
            "messageId": message_id,
        }
        try:
            self._audit_queue.put_nowait(entry)
        except asyncio.QueueFull:
            logger.error("Audit queue full (%d entries), dropping entry for %s", self.config.audit_queue_max_size, key)

    async def _audit_flush_loop(self) -> None:
        stream_name = socket.gethostname()
        try:
            await self.logs_client.create_log_stream(
                logGroupName=self.config.audit_log_group,
                logStreamName=stream_name,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                logger.error("Failed to create audit log stream", exc_info=True)
                return
        logger.info("Audit trail: %s/%s", self.config.audit_log_group, stream_name)
        while not self.shutdown_event.is_set() or not self._audit_queue.empty():
            batch: list[dict] = []
            try:
                entry = await asyncio.wait_for(self._audit_queue.get(), timeout=1.0)
                batch.append(entry)
                while len(batch) < 25:
                    try:
                        batch.append(self._audit_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                continue
            if batch:
                try:
                    await self.logs_client.put_log_events(
                        logGroupName=self.config.audit_log_group,
                        logStreamName=stream_name,
                        logEvents=sorted(
                            [{"timestamp": int(e["timestamp"] * 1000), "message": json.dumps(e)} for e in batch],
                            key=lambda x: x["timestamp"],
                        ),
                    )
                except Exception:
                    logger.warning("Failed to write %d audit entries", len(batch), exc_info=True)

    # --- Reconciliation (orphaned file detection) ---

    async def _reconciliation_loop(self) -> None:
        """Periodically scan the ingest bucket for orphaned files and re-queue them."""
        recon_sqs = await self._exit_stack.enter_async_context(
            self.session.create_client("sqs", region_name=self.config.aws_region)
        )
        recon_s3 = await self._exit_stack.enter_async_context(
            self.session.create_client("s3", region_name=self.config.aws_region)
        )
        bucket = self.config.reconciliation_bucket
        queue_url = self.config.reconciliation_queue_url
        threshold = self.config.reconciliation_age_threshold

        while not self.shutdown_event.is_set():
            await asyncio.sleep(self.config.reconciliation_interval)
            if self.shutdown_event.is_set():
                break
            try:
                now = time.time()
                requeued = 0
                paginator = recon_s3.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=bucket):
                    for obj in page.get("Contents", []):
                        age = now - obj["LastModified"].timestamp()
                        if age < threshold:
                            continue
                        key = obj["Key"]
                        size = obj.get("Size", 0)
                        # Send synthetic S3 event notification to the main scan queue
                        message_body = json.dumps({
                            "Records": [{
                                "eventSource": "aws:s3",
                                "eventName": "ObjectCreated:Reconciliation",
                                "s3": {
                                    "bucket": {"name": bucket},
                                    "object": {"key": urllib.parse.quote(key, safe=""), "size": size},
                                }
                            }]
                        })
                        await recon_sqs.send_message(
                            QueueUrl=queue_url,
                            MessageBody=message_body,
                        )
                        requeued += 1
                if requeued > 0:
                    logger.warning(
                        "Reconciliation: re-queued %d orphaned files from s3://%s (age > %ds)",
                        requeued, bucket, threshold,
                    )
                else:
                    logger.debug("Reconciliation: no orphaned files in s3://%s", bucket)
            except Exception:
                logger.warning("Reconciliation check failed", exc_info=True)

    # --- Shutdown ---

    async def _shutdown(self) -> None:
        self._ready = False
        logger.info(
            "Shutting down — waiting for %d in-flight tasks", len(self.in_flight)
        )
        if self.in_flight:
            await asyncio.gather(*self.in_flight, return_exceptions=True)
        # Flush pending SQS deletes before the client closes — all message
        # producers are done now, so this drains every queued handle.
        if self._delete_batcher:
            try:
                await asyncio.wait_for(self._delete_batcher.stop(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Delete batcher flush timed out; some messages may reappear")
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
            try:
                await self._reconciliation_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._audit_task:
            try:
                await asyncio.wait_for(self._audit_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._health_server:
            self._health_server.close()
            await self._health_server.wait_closed()
        if self._pool is not None:
            await self._pool.close()
        if self.scan_handle:
            await amaas.grpc.aio.quit(self.scan_handle)
        await self._exit_stack.aclose()
        logger.info("Shutdown complete")


def main() -> None:
    config = load_config()
    app = ScannerApp(config)

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGTERM, app.shutdown_event.set)
    loop.add_signal_handler(signal.SIGINT, app.shutdown_event.set)

    try:
        loop.run_until_complete(app.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
