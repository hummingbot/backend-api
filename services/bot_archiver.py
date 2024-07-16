import os
import shutil

import boto3
from botocore.exceptions import NoCredentialsError


class BotArchiver:
    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None, default_bucket_name=None):
        if aws_access_key_id and aws_secret_access_key:
            self.s3 = boto3.client('s3', aws_access_key_id=aws_access_key_id,
                                   aws_secret_access_key=aws_secret_access_key)
            self.default_bucket_name = default_bucket_name
        else:
            self.s3 = None
            self.default_bucket_name = None

    def archive_and_upload(self, instance_name, instance_dir, bucket_name=None):
        if not self.s3:
            raise ValueError("AWS S3 credentials are not provided.")

        if bucket_name is None:
            bucket_name = self.default_bucket_name

        archive_name = f"{instance_name}_archive.tar.gz"
        archive_path = os.path.join('bots', 'archived', archive_name)
        self.compress_directory(instance_dir, archive_path)

        try:
            self.s3.upload_file(archive_path, bucket_name, archive_name)
            print(f"Archive {archive_name} uploaded successfully to S3.")
            os.remove(archive_path)  # Remove the local archive file
            shutil.rmtree(instance_dir)  # Remove the instance directory
        except NoCredentialsError:
            print("Credentials not available for AWS S3.")

    @staticmethod
    def compress_directory(source_dir, output_path):
        shutil.make_archive(output_path.replace('.tar.gz', ''), 'gztar', source_dir)
        print(f"Compressed {source_dir} into {output_path}")

    def archive_locally(self, instance_name, instance_dir, compress=False):
        if compress:
            archive_name = f"{instance_name}_archive.tar.gz"
            archive_path = os.path.join('bots', 'archived', archive_name)
            self.compress_directory(instance_dir, archive_path)
            shutil.rmtree(instance_dir)  # Remove the instance directory
        else:
            archive_path = os.path.join('bots', 'archived', instance_name)
            shutil.move(instance_dir, archive_path)
