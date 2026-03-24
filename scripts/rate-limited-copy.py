#!/usr/bin/env python3
"""
Copy files from a source S3 bucket to a destination S3 bucket at a controlled rate.
Uses server-side S3 copy (no local download). Simulates sustained ingestion load.

Usage:
    python3 rate-limited-copy.py <source-bucket> <dest-bucket> [--rate FILES_PER_SEC] [--exclude KEY ...]

Examples:
    # 20M files/day (~231/sec)
    python3 rate-limited-copy.py eks-v1fs-malware-samples-886436954261 eks-v1fs-33-ingestbucket-xxx --rate 231

    # 2M files/day (~23/sec)
    python3 rate-limited-copy.py eks-v1fs-malware-samples-886436954261 eks-v1fs-33-ingestbucket-xxx --rate 23
"""
import argparse
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

def list_keys(s3, bucket, excludes):
    """List all object keys in a bucket, excluding specified keys."""
    keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if not any(key.endswith(ex) for ex in excludes):
                keys.append(key)
    return keys


def copy_one(s3, src_bucket, dst_bucket, key):
    """Server-side copy a single object."""
    s3.copy_object(
        Bucket=dst_bucket,
        Key=key,
        CopySource={'Bucket': src_bucket, 'Key': key},
    )
    return key


def main():
    parser = argparse.ArgumentParser(description='Rate-limited S3 copy')
    parser.add_argument('source_bucket', help='Source S3 bucket')
    parser.add_argument('dest_bucket', help='Destination S3 bucket')
    parser.add_argument('--rate', type=float, default=231, help='Files per second (default: 231 = 20M/day)')
    parser.add_argument('--exclude', nargs='*', default=['generate-test-files.py', 'sustained-load.py'],
                        help='File suffixes to exclude')
    parser.add_argument('--workers', type=int, default=40, help='Concurrent copy threads')
    args = parser.parse_args()

    s3 = boto3.client('s3')

    print(f'Listing keys in s3://{args.source_bucket}...')
    keys = list_keys(s3, args.source_bucket, args.exclude)
    total = len(keys)
    print(f'Found {total} files. Target rate: {args.rate}/sec ({args.rate * 86400 / 1e6:.1f}M/day)')
    print(f'Estimated upload time: {total / args.rate:.1f}s')
    print()

    interval = 1.0 / args.rate
    start = time.monotonic()
    copied = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for i, key in enumerate(keys):
            # Schedule the copy
            f = pool.submit(copy_one, s3, args.source_bucket, args.dest_bucket, key)
            futures[f] = key
            copied += 1

            # Rate control: sleep to maintain target rate
            expected_time = (i + 1) * interval
            elapsed = time.monotonic() - start
            if expected_time > elapsed:
                time.sleep(expected_time - elapsed)

            # Progress every 500 files
            if copied % 500 == 0:
                elapsed = time.monotonic() - start
                actual_rate = copied / elapsed if elapsed > 0 else 0
                print(f'  {copied}/{total} copied ({elapsed:.1f}s, {actual_rate:.1f}/sec)')

        # Wait for all copies to complete
        print('Waiting for in-flight copies to finish...')
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                errors += 1
                print(f'  ERROR copying {futures[f]}: {e}', file=sys.stderr)

    elapsed = time.monotonic() - start
    actual_rate = total / elapsed if elapsed > 0 else 0
    print()
    print(f'Done: {total} files copied in {elapsed:.1f}s ({actual_rate:.1f}/sec)')
    print(f'Errors: {errors}')


if __name__ == '__main__':
    main()
