import logging
import os
from dataclasses import dataclass


@dataclass
class Config:
    sqs_queue_url: str
    s3_ingest_bucket: str
    s3_clean_bucket: str
    s3_quarantine_bucket: str
    v1fs_server_addr: str
    v1fs_api_key_secret_arn: str
    aws_region: str
    log_level: str
    max_concurrent_scans: int


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

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    return Config(
        sqs_queue_url=required["SQS_QUEUE_URL"],
        s3_ingest_bucket=required["S3_INGEST_BUCKET"],
        s3_clean_bucket=required["S3_CLEAN_BUCKET"],
        s3_quarantine_bucket=required["S3_QUARANTINE_BUCKET"],
        v1fs_server_addr=os.environ.get(
            "V1FS_SERVER_ADDR",
            "my-release-visionone-filesecurity-scanner:50051",
        ),
        v1fs_api_key_secret_arn=required["V1FS_API_KEY_SECRET_ARN"],
        aws_region=required["AWS_REGION"],
        log_level=log_level,
        max_concurrent_scans=int(os.environ.get("MAX_CONCURRENT_SCANS", "20")),
    )
