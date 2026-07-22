"""Reference SQS-driven scanner consumer (Python) — pull / competing-consumers.

Mirrors reference/java-KEDA. A fixed pool of worker threads long-poll the same
SQS queue (they COMPETE for messages), and each hands its scan to the scanner
pod with the most free capacity via ScannerPool (least-busy). A message is
deleted only after a successful scan + action; any failure leaves it for SQS to
redeliver (reliability net #1). A pod-level failure is retried on a different
pod inside the pool (net #2). No load balancer in the scan path.

Config via env: SCAN_QUEUE_URL, SCANNER_TARGET_GROUP_ARN, V1FS_API_KEY,
PER_POD_CAPACITY (default 30), WORKERS (default = total scanner capacity),
SCANNER_TLS (default false), SCANNER_CA_CERT (path, optional), AWS_REGION.
"""
import json
import os
import re
import threading
import urllib.parse

import boto3

from scanner_pool import ScannerPool, NoCapacity

REGION = os.environ.get("AWS_REGION", "us-east-1")
_S3_REF = re.compile(r'"name"\s*:\s*"([^"]+)".*?"key"\s*:\s*"([^"]+)"', re.DOTALL)


def _env(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"missing env: {name}")
    return v


def _handle(s3, pool, body):
    m = _S3_REF.search(body)
    if not m:
        raise ValueError("no S3 ref in message")
    bucket, raw_key = m.group(1), m.group(2)
    # S3 event keys are form-encoded (spaces as '+') — decode like the pipeline app.
    key = urllib.parse.unquote_plus(raw_key)
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    verdict = pool.scan(data, key, wait_secs=60)        # inner pull: least-busy + retry
    malware = '"scanResult":1' in verdict or '"scanResult": 1' in verdict
    print(f"{bucket}/{key} -> {'MALWARE' if malware else 'clean'}", flush=True)


def _worker(sqs, s3, pool, queue_url):
    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        for msg in resp.get("Messages", []):
            try:
                _handle(s3, pool, msg["Body"])
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])  # ack
            except NoCapacity:
                pass                                    # nack by omission → SQS redelivers
            except Exception as e:
                print(f"scan failed, leaving for redelivery: {e}", flush=True)


def main():
    queue_url = _env("SCAN_QUEUE_URL")
    tg_arn = _env("SCANNER_TARGET_GROUP_ARN")
    api_key = _env("V1FS_API_KEY")
    per_pod = int(os.environ.get("PER_POD_CAPACITY", "30"))
    tls = os.environ.get("SCANNER_TLS", "false").lower() == "true"
    ca = os.environ.get("SCANNER_CA_CERT") or None

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    pool = ScannerPool(tg_arn, api_key, per_pod, tls, ca, REGION)

    # Size the worker pool near total scanner capacity: saturate the fleet
    # without overcommitting (the per-pod semaphores enforce the ceiling).
    workers = int(os.environ.get("WORKERS", str(max(1, pool.total_capacity()))))
    print(f"Starting {workers} workers against {pool.total_capacity()} scanner slots", flush=True)
    threads = [threading.Thread(target=_worker, args=(sqs, s3, pool, queue_url), daemon=True)
               for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
