"""Idempotent S3 upload helper that relies on bucket-policy immutability.

The object key is an INPUT content hash, computable by anyone with the source, and the
bucket is first-write-wins. So the key alone never proves which bytes sit behind it.
Every write records the zip's sha256 as object metadata (and as the S3 ChecksumSHA256)
in the same first PutObject, and every reuse of an existing object downloads and hashes
it before trusting it.
"""

from __future__ import annotations

import base64
import enum
import hashlib
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Stored as x-amz-meta-zip-sha256: hex sha256 of the object body.
SHA256_METADATA_KEY = "zip-sha256"


class UploadResult(enum.Enum):
    UPLOADED = "uploaded"
    ALREADY_PRESENT = "already_present"


class ArtifactIntegrityError(RuntimeError):
    """An existing object's bytes do not match the sha256 it must have."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checksum_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


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

    def read(self, *, bucket: str, key: str) -> tuple[str, str | None]:
        """Download the object; return (sha256 of its body, recorded sha256 or None)."""
        obj = self._client.get_object(Bucket=bucket, Key=key)
        actual = sha256_hex(obj["Body"].read())
        recorded = (obj.get("Metadata") or {}).get(SHA256_METADATA_KEY)
        return actual, recorded

    def verify(
        self,
        *,
        bucket: str,
        key: str,
        expected_sha256: str | None = None,
        allow_unrecorded: bool = False,
    ) -> str:
        """Hash the object and check it; return its sha256 or raise ArtifactIntegrityError.

        The body must match the recorded sha256 and, when given, ``expected_sha256``.
        An object with no recorded sha256 (written before recording existed, or by a
        writer that skipped it) passes only against ``expected_sha256`` or with
        ``allow_unrecorded``.
        """
        actual, recorded = self.read(bucket=bucket, key=key)
        where = f"s3://{bucket}/{key}"
        if recorded is not None and recorded != actual:
            raise ArtifactIntegrityError(
                f"{where}: body sha256 {actual} != recorded {SHA256_METADATA_KEY} {recorded}"
            )
        if expected_sha256 is not None and expected_sha256 != actual:
            raise ArtifactIntegrityError(
                f"{where}: body sha256 {actual} != expected {expected_sha256}"
            )
        if recorded is None and expected_sha256 is None and not allow_unrecorded:
            raise ArtifactIntegrityError(
                f"{where}: no recorded {SHA256_METADATA_KEY} and no expected sha256 to "
                f"check it against"
            )
        return actual

    def copy(
        self,
        *,
        src_bucket: str,
        dst_bucket: str,
        key: str,
        expected_sha256: str | None = None,
        allow_unrecorded: bool = False,
    ) -> UploadResult:
        """
        Server-side copy of the same key from src_bucket to dst_bucket, verified.

        The source is hashed and checked before the copy (see ``verify``). The
        destination, new or already present, must then hash to the same sha256, so a
        pre-existing destination object with other bytes fails instead of reading as
        "already promoted". The copy records the sha256 on the destination.
        """
        src_sha = self.verify(
            bucket=src_bucket,
            key=key,
            expected_sha256=expected_sha256,
            allow_unrecorded=allow_unrecorded,
        )
        if self.exists(bucket=dst_bucket, key=key):
            self.verify(bucket=dst_bucket, key=key, expected_sha256=src_sha)
            return UploadResult.ALREADY_PRESENT
        self._client.copy_object(
            Bucket=dst_bucket,
            Key=key,
            CopySource={"Bucket": src_bucket, "Key": key},
            ServerSideEncryption="AES256",
            MetadataDirective="REPLACE",
            Metadata={SHA256_METADATA_KEY: src_sha},
            ChecksumAlgorithm="SHA256",
        )
        self.verify(bucket=dst_bucket, key=key, expected_sha256=src_sha)
        return UploadResult.UPLOADED

    def upload(self, *, bucket: str, key: str, body_path: Path) -> UploadResult:
        """
        PutObject with If-None-Match=*, recording the body's sha256.

        On 412 PreconditionFailed (key already exists) the existing object must hash
        to this body's sha256: ALREADY_PRESENT if it does, ArtifactIntegrityError if
        not. Any other error is re-raised.
        """
        body = body_path.read_bytes()
        digest = sha256_hex(body)
        try:
            self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                Metadata={SHA256_METADATA_KEY: digest},
                ChecksumSHA256=_checksum_b64(digest),
            )
            return UploadResult.UPLOADED
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "PreconditionFailed" or status == 412:
                self.verify(bucket=bucket, key=key, expected_sha256=digest)
                return UploadResult.ALREADY_PRESENT
            raise
