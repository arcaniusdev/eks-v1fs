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
    s3_clean_bucket: str
    s3_quarantine_bucket: str
    s3_review_bucket: str
    v1fs_server_addr: str
    v1fs_api_key_secret_arn: str
    aws_region: str
    log_level: str
    max_concurrent_scans: int
    pml_enabled: bool
    audit_log_group: str
    health_port: int
    max_file_size_mb: int
    review_routing_enabled: bool
    sqs_visibility_timeout: int
    audit_queue_max_size: int


def load_config() -> Config:
    required = {
        "SQS_QUEUE_URL": os.environ.get("SQS_QUEUE_URL"),
        "S3_INGEST_BUCKET": os.environ.get("S3_INGEST_BUCKET"),
        "S3_CLEAN_BUCKET": os.environ.get("S3_CLEAN_BUCKET"),
        "S3_QUARANTINE_BUCKET": os.environ.get("S3_QUARANTINE_BUCKET"),
        "V1FS_API_KEY_SECRET_ARN": os.environ.get("V1FS_API_KEY_SECRET_ARN"),
        "AWS_REGION": os.environ.get("AWS_REGION"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    review_routing_enabled = os.environ.get("REVIEW_ROUTING_ENABLED", "true").lower() == "true"
    s3_review_bucket = os.environ.get("S3_REVIEW_BUCKET", "")
    if review_routing_enabled and not s3_review_bucket:
        raise ValueError("S3_REVIEW_BUCKET is required when REVIEW_ROUTING_ENABLED is true")

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ValueError(f"Invalid LOG_LEVEL: {log_level!r}. Must be one of {VALID_LOG_LEVELS}")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return Config(
        sqs_queue_url=required["SQS_QUEUE_URL"],
        s3_ingest_bucket=required["S3_INGEST_BUCKET"],
        s3_clean_bucket=required["S3_CLEAN_BUCKET"],
        s3_quarantine_bucket=required["S3_QUARANTINE_BUCKET"],
        s3_review_bucket=s3_review_bucket,
        v1fs_server_addr=os.environ.get(
            "V1FS_SERVER_ADDR",
            "my-release-visionone-filesecurity-scanner:50051",
        ),
        v1fs_api_key_secret_arn=required["V1FS_API_KEY_SECRET_ARN"],
        aws_region=required["AWS_REGION"],
        log_level=log_level,
        max_concurrent_scans=_int_env("MAX_CONCURRENT_SCANS", "50", 1, 1000),
        pml_enabled=os.environ.get("PML_ENABLED", "false").lower() == "true",
        audit_log_group=os.environ.get("AUDIT_LOG_GROUP", ""),
        health_port=_int_env("HEALTH_PORT", "8080", 1, 65535),
        max_file_size_mb=_int_env("MAX_FILE_SIZE_MB", "500", 0, 4096),
        review_routing_enabled=review_routing_enabled,
        sqs_visibility_timeout=_int_env("SQS_VISIBILITY_TIMEOUT", "300", 30, 43200),
        audit_queue_max_size=_int_env("AUDIT_QUEUE_MAX_SIZE", "1000", 100, 100000),
    )
