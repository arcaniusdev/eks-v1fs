import asyncio
import json
import logging
import signal
import urllib.parse
from contextlib import AsyncExitStack

import boto3
import amaas.grpc.aio
from aiobotocore.session import AioSession
from botocore.exceptions import ClientError

from config import load_config

logger = logging.getLogger("scanner")


class ScannerApp:
    def __init__(self, config):
        self.config = config
        self.shutdown_event = asyncio.Event()
        self.semaphore = asyncio.Semaphore(config.max_concurrent_scans)
        self.in_flight: set[asyncio.Task] = set()
        self.scan_handle = None
        self.session = AioSession()
        self._exit_stack = AsyncExitStack()

    async def start(self):
        self.s3_client = await self._exit_stack.enter_async_context(
            self.session.create_client("s3", region_name=self.config.aws_region)
        )
        self.sqs_client = await self._exit_stack.enter_async_context(
            self.session.create_client("sqs", region_name=self.config.aws_region)
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
        self.scan_handle = amaas.grpc.aio.init(
            self.config.v1fs_server_addr, api_key, False
        )

        logger.info(
            "Scanner started — polling %s (concurrency=%d)",
            self.config.sqs_queue_url,
            self.config.max_concurrent_scans,
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
            except Exception:
                logger.exception("SQS receive_message error")
                await asyncio.sleep(5)
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

            for record in records:
                bucket = record["s3"]["bucket"]["name"]
                key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
                size = record["s3"]["object"].get("size", 0)
                logger.info(
                    "Processing s3://%s/%s (%d bytes) [msg=%s]",
                    bucket, key, size, message_id,
                )

                # Download into memory
                try:
                    file_bytes = await self._download(bucket, key)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "NoSuchKey":
                        logger.warning(
                            "Object s3://%s/%s no longer exists, deleting message",
                            bucket, key,
                        )
                        await self._delete_message(receipt_handle)
                        return
                    raise

                # Scan
                result_json = await amaas.grpc.aio.scan_buffer(
                    self.scan_handle,
                    file_bytes,
                    key,
                    pml=False,
                    tags=["s3-ingest"],
                )
                result = json.loads(result_json)
                is_malicious = result.get("scanResult", 0) > 0

                # Route
                if is_malicious:
                    dest_bucket = self.config.s3_quarantine_bucket
                    logger.warning(
                        "MALICIOUS: s3://%s/%s → s3://%s/%s sha256=%s result=%s",
                        bucket, key, dest_bucket, key,
                        result.get("fileSHA256", "unknown"),
                        json.dumps(result.get("result", {})),
                    )
                else:
                    dest_bucket = self.config.s3_clean_bucket
                    logger.info(
                        "CLEAN: s3://%s/%s → s3://%s/%s",
                        bucket, key, dest_bucket, key,
                    )

                await self._upload(dest_bucket, key, file_bytes)
                await self._delete_object(bucket, key)

            await self._delete_message(receipt_handle)

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

    async def _upload(self, bucket, key, data):
        await self.s3_client.put_object(Bucket=bucket, Key=key, Body=data)

    async def _delete_object(self, bucket, key):
        await self.s3_client.delete_object(Bucket=bucket, Key=key)

    async def _delete_message(self, receipt_handle):
        await self.sqs_client.delete_message(
            QueueUrl=self.config.sqs_queue_url,
            ReceiptHandle=receipt_handle,
        )

    async def _shutdown(self):
        logger.info(
            "Shutting down — waiting for %d in-flight tasks", len(self.in_flight)
        )
        if self.in_flight:
            await asyncio.gather(*self.in_flight, return_exceptions=True)
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
