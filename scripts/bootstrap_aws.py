"""One-time bootstrap: create the DynamoDB table and S3 bucket for the
runtime serving layer. Idempotent — safe to re-run.

Region: ap-south-1 (matches Bedrock).
"""

from __future__ import annotations

import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REGION = os.getenv("AWS_REGION", "ap-south-1")
DDB_TABLE = "Item-Item-Similarity-Search"
S3_BUCKET = "search-item-item-poc"


def ensure_ddb_table() -> None:
    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        ddb.describe_table(TableName=DDB_TABLE)
        log.info("DynamoDB table %r already exists", DDB_TABLE)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    log.info("Creating DynamoDB table %r in %s…", DDB_TABLE, REGION)
    ddb.create_table(
        TableName=DDB_TABLE,
        AttributeDefinitions=[{"AttributeName": "alias_item_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "alias_item_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = ddb.get_waiter("table_exists")
    waiter.wait(TableName=DDB_TABLE)
    log.info("  table ACTIVE")


def ensure_s3_bucket() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        log.info("S3 bucket %r already exists", S3_BUCKET)
        return
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            # 403 means it exists but we can't access it — likely owned by another account
            log.error("S3 head_bucket failed: %s", e)
            raise

    log.info("Creating S3 bucket %r in %s…", S3_BUCKET, REGION)
    # ap-south-1 requires LocationConstraint
    s3.create_bucket(
        Bucket=S3_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    s3.put_bucket_versioning(
        Bucket=S3_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    log.info("  bucket created with versioning enabled")


def main() -> None:
    ensure_ddb_table()
    ensure_s3_bucket()
    log.info("Bootstrap complete.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        log.error("AWS error: %s", e)
        sys.exit(1)
