import botocore.exceptions  # type: ignore
import boto3  # type: ignore
import os
import logging


def upload_dir_to_s3(local_directory: str, s3_bucket: str, s3_bucket_dir: str,
                     make_public=False) -> None:
    client = boto3.client('s3')
    for root, dirs, files in os.walk(local_directory):

        for filename in files:
            local_path = os.path.join(root, filename)

            # construct the full path
            relative_path = os.path.relpath(local_path, local_directory)
            s3_path = os.path.join(s3_bucket_dir, relative_path)

            print(f"Searching {s3_path} in {s3_bucket}")
            try:
                client.head_object(Bucket=s3_bucket, Key=s3_path)
                logging.warning("Existing file {s3_path} found on S3! Skipping.")
            except botocore.exceptions.ClientError:  # Exception abuse
                extra_args = None
                if make_public:
                    extra_args = {'ACL': 'public-read'}

                logging.info(f"Uploading {s3_path}")
                client.upload_file(local_path, s3_bucket, s3_path, ExtraArgs=extra_args)