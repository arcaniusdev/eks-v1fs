import logging
import os
from dataclasses import dataclass

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _int_env(name: str, default: str, min_val: int = 0, max_val: int = 2**31) -> int:
    """Parse an integer environment variable with bounds validation."""
    raw = os.environ.get(name, default)
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}")
    if not (min_val <= val <= max_val):
        raise ValueError(f"{name} must be {min_val}-{max_val}, got: {val}")
    return val


@dataclass
class Config:
    sqs_queue_url: str
    s3_ingest_bucket: str
    s3_quarantine_bucket: str
    s3_review_bucket: str
    v1fs_server_addr: str
    v1fs_tls_enabled: bool
    v1fs_ca_cert: str
    dispatch_mode: str
    scanner_target_group_arn: str
    per_pod_capacity: int
    pod_refresh_secs: int
    v1fs_api_key_secret_arn: str
    aws_region: str
    log_level: str
    max_concurrent_scans: int
    pml_enabled: bool
    audit_log_group: str
    health_port: int
    max_file_size_mb: int
    max_inflight_bytes: int
    review_routing_enabled: bool
    delete_source_enabled: bool
    sqs_visibility_timeout: int
    audit_queue_max_size: int
    reconciliation_enabled: bool
    reconciliation_bucket: str
    reconciliation_queue_url: str
    reconciliation_interval: int
    reconciliation_age_threshold: int


def load_config() -> Config:
    required = {
        "SQS_QUEUE_URL": os.environ.get("SQS_QUEUE_URL"),
        "S3_QUARANTINE_BUCKET": os.environ.get("S3_QUARANTINE_BUCKET"),
        "V1FS_API_KEY_SECRET_ARN": os.environ.get("V1FS_API_KEY_SECRET_ARN"),
        "AWS_REGION": os.environ.get("AWS_REGION"),
    }
    # Informational only — scanner.py reads the source bucket from each SQS
    # message, not from this. Empty in external-queue mode (many source buckets).
    s3_ingest_bucket = os.environ.get("S3_INGEST_BUCKET", "")

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    review_routing_enabled = os.environ.get("REVIEW_ROUTING_ENABLED", "true").lower() == "true"
    s3_review_bucket = os.environ.get("S3_REVIEW_BUCKET", "")
    if review_routing_enabled and not s3_review_bucket:
        raise ValueError("S3_REVIEW_BUCKET is required when REVIEW_ROUTING_ENABLED is true")

    dispatch_mode = os.environ.get("DISPATCH_MODE", "clusterip").lower()
    if dispatch_mode not in ("clusterip", "pull"):
        raise ValueError(f"DISPATCH_MODE must be 'clusterip' or 'pull', got: {dispatch_mode!r}")
    scanner_target_group_arn = os.environ.get("SCANNER_TARGET_GROUP_ARN", "")
    if dispatch_mode == "pull" and not scanner_target_group_arn:
        raise ValueError("SCANNER_TARGET_GROUP_ARN is required when DISPATCH_MODE=pull")

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ValueError(f"Invalid LOG_LEVEL: {log_level!r}. Must be one of {VALID_LOG_LEVELS}")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return Config(
        sqs_queue_url=required["SQS_QUEUE_URL"],
        s3_ingest_bucket=s3_ingest_bucket,
        s3_quarantine_bucket=required["S3_QUARANTINE_BUCKET"],
        s3_review_bucket=s3_review_bucket,
        v1fs_server_addr=os.environ.get(
            "V1FS_SERVER_ADDR",
            "my-release-visionone-filesecurity-scanner:50051",
        ),
        # TLS to the scanner. Default off: the in-cluster ClusterIP/NLB path is
        # plaintext gRPC. Set true for ALB mode (:443). V1FS_CA_CERT points at a
        # PEM to trust — required for a self-signed ALB cert; leave empty to use
        # the system trust store (publicly-signed certs).
        v1fs_tls_enabled=os.environ.get("V1FS_TLS_ENABLED", "false").lower() == "true",
        v1fs_ca_cert=os.environ.get("V1FS_CA_CERT", ""),
        # Dispatch mode: how the app reaches the scanner pods.
        #   clusterip (default) — one gRPC handle to the in-cluster Service;
        #     the Service spreads connections across pods (simple, L4).
        #   pull — discover live pod IPs from the NLB target group and dispatch
        #     each scan to the least-busy pod directly (no LB in the scan path).
        dispatch_mode=dispatch_mode,
        scanner_target_group_arn=scanner_target_group_arn,
        per_pod_capacity=_int_env("PER_POD_CAPACITY", "30", 1, 1000),
        pod_refresh_secs=_int_env("POD_REFRESH_SECS", "20", 5, 300),
        v1fs_api_key_secret_arn=required["V1FS_API_KEY_SECRET_ARN"],
        aws_region=required["AWS_REGION"],
        log_level=log_level,
        max_concurrent_scans=_int_env("MAX_CONCURRENT_SCANS", "50", 1, 1000),
        pml_enabled=os.environ.get("PML_ENABLED", "false").lower() == "true",
        audit_log_group=os.environ.get("AUDIT_LOG_GROUP", ""),
        health_port=_int_env("HEALTH_PORT", "8080", 1, 65535),
        max_file_size_mb=_int_env("MAX_FILE_SIZE_MB", "500", 0, 4096),
        # Total downloaded bytes allowed in memory across concurrent scans.
        # Floored to one max-size file at runtime. Keep it comfortably below
        # the pod memory limit (files are held as bytes plus scan overhead).
        max_inflight_bytes=_int_env("MAX_INFLIGHT_MB", "1024", 0, 65536) * 1024 * 1024,
        review_routing_enabled=review_routing_enabled,
        delete_source_enabled=os.environ.get("DELETE_SOURCE_ENABLED", "true").lower() == "true",
        # Default matches the deploy.sh ConfigMap and the ScanTimeoutSeconds
        # CFN parameter (600s) so the heartbeat math is consistent everywhere.
        sqs_visibility_timeout=_int_env("SQS_VISIBILITY_TIMEOUT", "600", 30, 43200),
        audit_queue_max_size=_int_env("AUDIT_QUEUE_MAX_SIZE", "1000", 100, 100000),
        reconciliation_enabled=os.environ.get("RECONCILIATION_ENABLED", "false").lower() == "true",
        reconciliation_bucket=os.environ.get("RECONCILIATION_BUCKET", ""),
        reconciliation_queue_url=os.environ.get("RECONCILIATION_QUEUE_URL", ""),
        reconciliation_interval=_int_env("RECONCILIATION_INTERVAL", "300", 60, 3600),
        reconciliation_age_threshold=_int_env("RECONCILIATION_AGE_THRESHOLD", "1800", 300, 86400),
    )
