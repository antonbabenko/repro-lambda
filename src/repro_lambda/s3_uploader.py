"""Idempotent S3 upload helper that relies on bucket-policy immutability."""

from __future__ import annotations

import enum
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


class UploadResult(enum.Enum):
    UPLOADED = "uploaded"
    ALREADY_PRESENT = "already_present"


class S3Uploader:
    def __init__(self, region: str, client=None) -> None:
        self._client = client or boto3.client("s3", region_name=region)

    def exists(self, *, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def copy(self, *, src_bucket: str, dst_bucket: str, key: str) -> UploadResult:
        """
        Server-side copy of the same key from src_bucket to dst_bucket.

        Idempotent via a destination existence pre-check; safe because artifact
        buckets are content-addressed and immutable (the same key never changes
        bytes). Returns ALREADY_PRESENT if the destination key already exists,
        UPLOADED otherwise.
        """
        if self.exists(bucket=dst_bucket, key=key):
            return UploadResult.ALREADY_PRESENT
        self._client.copy_object(
            Bucket=dst_bucket,
            Key=key,
            CopySource={"Bucket": src_bucket, "Key": key},
            ServerSideEncryption="AES256",
        )
        return UploadResult.UPLOADED

    def upload(self, *, bucket: str, key: str, body_path: Path) -> UploadResult:
        """
        PutObject with If-None-Match=*.

        On 412 PreconditionFailed (key already exists) returns ALREADY_PRESENT.
        Any other error is re-raised.
        """
        body = body_path.read_bytes()
        try:
            self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
            )
            return UploadResult.UPLOADED
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                return UploadResult.ALREADY_PRESENT
            raise
