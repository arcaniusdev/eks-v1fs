import asyncio
import json
import logging
import random
import signal
import socket
import time
import urllib.parse
from contextlib import AsyncExitStack

import boto3
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


class ScannerApp:
    def __init__(self, config):
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.semaphore = asyncio.Semaphore(config.max_concurrent_scans)
        self.in_flight: set[asyncio.Task] = set()
        self.max_file_size = config.max_file_size_mb * 1024 * 1024
        self.scan_handle = None
        self.session = AioSession()
        self._exit_stack = AsyncExitStack()
        self._consecutive_errors = 0
        self._ready = False
        self._audit_queue = asyncio.Queue(maxsize=1000)
        self._health_server = None
        self._audit_task = None

    async def start(self):
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
        sm_client = boto3.client(
            "secretsmanager", region_name=self.config.aws_region
        )
        resp = sm_client.get_secret_value(
            SecretId=self.config.v1fs_api_key_secret_arn
        )
        api_key = resp["SecretString"]

        logger.info(
            "Initializing V1FS async gRPC handle at %s",
            self.config.v1fs_server_addr,
        )
        # init is synchronous - do not await
        self.scan_handle = amaas.grpc.aio.init(
            self.config.v1fs_server_addr, api_key, False
        )

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
        try:
            await self._poll_loop()
        finally:
            await self._shutdown()

    async def _poll_loop(self):
        while not self.shutdown_event.is_set():
            # Backpressure: pause polling when too many tasks are in-flight
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

    async def _guarded_process(self, message):
        async with self.semaphore:
            await self._process_message(message)

    async def _process_message(self, message):
        message_id = message.get("MessageId", "unknown")
        receipt_handle = message["ReceiptHandle"]
        heartbeat_task = asyncio.create_task(
            self._extend_visibility(receipt_handle)
        )
        try:
            body = json.loads(message["Body"])
            records = body.get("Records", [])
            if not records:
                # s3:TestEvent or empty — discard
                logger.info("No Records in message %s, deleting", message_id)
                await self._delete_message(receipt_handle)
                return

            all_succeeded = True
            for record in records:
                try:
                    await self._process_record(record, message_id)
                except Exception:
                    bucket = record.get("s3", {}).get("bucket", {}).get("name", "?")
                    key = record.get("s3", {}).get("object", {}).get("key", "?")
                    logger.exception(
                        "Failed processing record s3://%s/%s [msg=%s]",
                        bucket, key, message_id,
                    )
                    all_succeeded = False

            if all_succeeded:
                await self._delete_message(receipt_handle)
            else:
                logger.warning(
                    "One or more records failed in message %s — leaving for retry",
                    message_id,
                )

        except json.JSONDecodeError:
            logger.exception("Malformed message body [msg=%s], deleting", message_id)
            await self._delete_message(receipt_handle)
        except Exception:
            logger.exception("Failed processing message %s — leaving for retry", message_id)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _process_record(self, record, message_id):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        size = record["s3"]["object"].get("size", 0)

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
            await self._delete_object(bucket, key)
            self._enqueue_audit(key, size, verdict, {}, 0, message_id)
            return

        # Download into memory
        try:
            file_bytes = await self._download(bucket, key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(
                    "Object s3://%s/%s no longer exists, skipping",
                    bucket, key,
                )
                return
            raise

        # Scan
        scan_start = time.monotonic()
        result_json = await amaas.grpc.aio.scan_buffer(
            self.scan_handle,
            file_bytes,
            key,
            pml=self.config.pml_enabled,
            tags=["S3-Scan"],
        )
        scan_duration_ms = int((time.monotonic() - scan_start) * 1000)
        result = json.loads(result_json)
        is_malicious = result.get("scanResult", 0) > 0
        decompression_errors = self._get_decompression_errors(result)

        # Route: malicious → quarantine, decompression errors → review, clean → clean
        if is_malicious:
            dest_bucket = self.config.s3_quarantine_bucket
            verdict = "malicious"
            tag = "S3-Malware"
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
            tag = "S3-Review"
            logger.warning(
                "REVIEW: s3://%s/%s → s3://%s/%s (decompression limit errors: %s)",
                bucket, key, dest_bucket, key, decompression_errors,
            )
        else:
            dest_bucket = self.config.s3_clean_bucket
            verdict = "clean"
            tag = "S3-Clean"
            logger.info(
                "CLEAN: s3://%s/%s → s3://%s/%s",
                bucket, key, dest_bucket, key,
            )

        await self._upload(dest_bucket, key, file_bytes, tag)
        await self._delete_object(bucket, key)
        self._enqueue_audit(key, size, verdict, result, scan_duration_ms, message_id)

    @staticmethod
    def _get_decompression_errors(result: dict) -> list[str]:
        """Return decompression limit error names from scan result, if any."""
        found_errors = result.get("foundErrors", [])
        return [
            e.get("name", "")
            for e in found_errors
            if e.get("name", "") in DECOMPRESSION_ERROR_NAMES
        ]

    async def _extend_visibility(self, receipt_handle, interval=240):
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sqs_client.change_message_visibility(
                    QueueUrl=self.config.sqs_queue_url,
                    ReceiptHandle=receipt_handle,
                    VisibilityTimeout=300,
                )
            except Exception:
                logger.warning("Failed to extend visibility", exc_info=True)

    async def _download(self, bucket, key):
        resp = await self.s3_client.get_object(Bucket=bucket, Key=key)
        async with resp["Body"] as stream:
            return await stream.read()

    async def _upload(self, bucket, key, data, tag=None):
        kwargs = {"Bucket": bucket, "Key": key, "Body": data}
        if tag:
            kwargs["Tagging"] = f"ScanResult={tag}"
        await self.s3_client.put_object(**kwargs)

    async def _delete_object(self, bucket, key):
        await self.s3_client.delete_object(Bucket=bucket, Key=key)

    async def _delete_message(self, receipt_handle):
        await self.sqs_client.delete_message(
            QueueUrl=self.config.sqs_queue_url,
            ReceiptHandle=receipt_handle,
        )

    # --- Health Server ---

    async def _start_health_server(self):
        self._health_server = await asyncio.start_server(
            self._handle_health_request, "0.0.0.0", self.config.health_port
        )
        logger.info("Health server listening on port %d", self.config.health_port)

    async def _handle_health_request(self, reader, writer):
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
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # --- Audit Trail ---

    def _enqueue_audit(self, key, size, verdict, result, scan_duration_ms, message_id):
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
            logger.warning("Audit queue full, dropping entry for %s", key)

    async def _audit_flush_loop(self):
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
            batch = []
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

    # --- Shutdown ---

    async def _shutdown(self):
        self._ready = False
        logger.info(
            "Shutting down — waiting for %d in-flight tasks", len(self.in_flight)
        )
        if self.in_flight:
            await asyncio.gather(*self.in_flight, return_exceptions=True)
        if self._audit_task:
            try:
                await asyncio.wait_for(self._audit_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._health_server:
            self._health_server.close()
            await self._health_server.wait_closed()
        if self.scan_handle:
            await amaas.grpc.aio.quit(self.scan_handle)
        await self._exit_stack.aclose()
        logger.info("Shutdown complete")


def main():
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
