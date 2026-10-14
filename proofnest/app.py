
import os
import io
import boto3
from botocore.config import Config


class Storage:
    def __init__(self):
        self.s3 = None

        if S3_BUCKET:
            self.s3 = boto3.client(
                "s3",
                region_name=os.environ.get("AWS_REGION", "eu-north-1"),
                config=Config(signature_version="s3v4")
            )

    def put(self, key, data, content_type):
        if self.s3:
            self.s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=content_type
            )
            return

        path = self.local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "wb") as f:
            f.write(data)

    def get(self, key):
        if self.s3:
            response = self.s3.get_object(
                Bucket=S3_BUCKET,
                Key=key
            )
            return response["Body"].read()

        path = self.local_path(key)

        if not os.path.isfile(path):
            return None

        with open(path, "rb") as f:
            return f.read()

    def url(self, key):
        if self.s3:
            return self.s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": S3_BUCKET,
                    "Key": key
                },
                ExpiresIn=3600
            )

        return None

    def local_path(self, key):
        base_dir = os.path.abspath(
            os.environ.get("UPLOAD_FOLDER", "uploads")
        )

        safe_key = os.path.normpath(key)

        if (
            os.path.isabs(safe_key)
            or safe_key == ".."
            or safe_key.startswith(".." + os.sep)
        ):
            raise ValueError("Invalid storage key")

        path = os.path.abspath(os.path.join(base_dir, safe_key))

        if os.path.commonpath([base_dir, path]) != base_dir:
            raise ValueError("Invalid storage path")

        return path


store = Storage()
