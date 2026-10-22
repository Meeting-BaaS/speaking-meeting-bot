import argparse
import mimetypes
import os
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config.persona_utils import persona_manager


class UTFSUploader:
    def __init__(self, api_key: str, app_id: str):
        self.api_key = api_key
        self.app_id = app_id
        self.base_url = "https://api.uploadthing.com"  # Updated base URL
        self.persona_manager = persona_manager
        self.uploaded_urls = self._load_existing_urls()

        # Configure logger levels
        logger.remove()
        logger.add(lambda msg: print(msg), level="INFO")

    def _load_existing_urls(self) -> dict:
        """Load existing image URLs from personas"""
        return self.persona_manager.get_image_urls()

    def _is_valid_url(self, url: str) -> bool:
        """Check if URL is valid and accessible"""
        if not url or not url.startswith("http"):
            return False

        try:
            response = requests.head(url, timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def _image_needs_upload(self, persona_key: str) -> bool:
        """Check if image needs to be uploaded"""
        # Check if URL exists and is from uploadthing.com
        current_url = self.uploaded_urls.get(persona_key, "")
        return not (
            current_url
            and "uploadthing.com" in current_url
            and self._is_valid_url(current_url)
        )

    def upload_file(
        self, file_path: Path, custom_id: Optional[str] = None
    ) -> Optional[str]:
        """Upload a file using the UploadThing API"""
        # Extract persona key from filename
        persona_key = Path(file_path).stem

        # Check if file exists locally
        if not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            return None

        # Check if we need to upload
        if not self._image_needs_upload(persona_key):
            logger.info(f"Image already uploaded for {persona_key}")
            return self.uploaded_urls[persona_key]

        # Continue with existing upload logic
        logger.info(f"Uploading file: {file_path}")
        try:
            # Get file info
            file_name = file_path.name
            file_size = file_path.stat().st_size
            file_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

            # Step 1: Prepare the upload
            prepare_url = f"{self.base_url}/v6/uploadFiles"
            headers = {
                "x-uploadthing-api-key": self.api_key,
                "Content-Type": "application/json",
            }

            prepare_data = {
                "files": [{"name": file_name, "size": file_size, "type": file_type}],
                "acl": "public-read",
                "contentDisposition": "inline",
            }

            logger.info(f"Request data: {prepare_data}")
            logger.info(f"Uploading file: {file_name} (size: {file_size} bytes)")
            logger.info(f"Headers: {headers}")

            # Get presigned URL
            logger.info("Making request to get presigned URL...")
            response = requests.post(prepare_url, headers=headers, json=prepare_data)
            logger.info(f"Presigned URL response status: {response.status_code}")
            logger.debug(f"Presigned URL raw response: {response.text}")

            if response.status_code != 200:
                raise Exception(f"Failed to get presigned URL: {response.text}")

            presigned_data = response.json()
            logger.debug(f"Parsed presigned data: {presigned_data}")

            if not presigned_data.get("data"):
                raise Exception("No presigned URL received")

            file_data = presigned_data["data"][0]
            logger.info(f"Got presigned URL: {file_data['url']}")
            logger.debug(f"Upload fields: {file_data['fields']}")

            # Step 2: Upload to presigned URL
            logger.info("Starting file upload to presigned URL...")
            with open(file_path, "rb") as f:
                upload_response = requests.post(
                    file_data["url"],
                    data=file_data["fields"],
                    files={"file": (file_name, f, file_type)},
                    timeout=30,
                )
                logger.info(f"Upload response status: {upload_response.status_code}")
                logger.debug(f"Upload response: {upload_response.text}")

                if upload_response.status_code != 204:
                    raise Exception(f"Upload failed: {upload_response.text}")

            # Update personas.json with the new URL while preserving other fields
            try:
                base_filename = file_path.stem
                logger.debug(f"Looking for persona with key: {base_filename}")

                if base_filename in self.persona_manager.personas:
                    # Get the complete current persona data
                    current_persona = self.persona_manager.personas[base_filename]

                    # Only update the image URL, preserving all other fields
                    updated_persona = {
                        **current_persona,  # Preserve all existing fields
                        "image": file_data["fileUrl"],  # Update only the image URL
                    }

                    # Save using PersonaManager
                    success = self.persona_manager.save_persona(
                        base_filename, updated_persona
                    )
                    if success:
                        logger.success(
                            f"Updated image URL for {base_filename} while preserving all metadata"
                        )
                    else:
                        logger.error(f"Failed to save persona data for {base_filename}")
                else:
                    logger.warning(f"Could not find persona for key {base_filename}")
                    logger.debug(
                        "Available personas: "
                        + ", ".join(self.persona_manager.personas.keys())
                    )

            except Exception as e:
                logger.error(f"Error updating persona: {str(e)}")

            return file_data["fileUrl"]

        except Exception as e:
            logger.error(f"Error during upload: {str(e)}")
            return None

    def check_api_health(self) -> bool:
        """Check if the API is responding"""
        try:
            logger.info("Checking API health...")
            test_url = f"{self.base_url}/v6/uploadFiles"
            headers = {
                "x-uploadthing-api-key": self.api_key,
                "Content-Type": "application/json",
            }

            # Properly formatted test request based on API docs
            test_data = {
                "files": [{"name": "test.txt", "size": 100, "type": "text/plain"}],
                "acl": "public-read",
                "contentDisposition": "inline",
            }

            logger.debug(f"Making health check request with data: {test_data}")
            response = requests.post(
                test_url, headers=headers, json=test_data, timeout=10
            )

            logger.info(f"API health check response: {response.status_code}")
            if response.status_code != 200:
                logger.error(f"API response: {response.text}")

            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error checking API health: {str(e)}")
            return False

    def verify_credentials(self) -> bool:
        """Verify API key and app ID are valid"""
        try:
            logger.info("Verifying credentials...")
            response = requests.post(
                f"{self.base_url}/v7/getAppInfo",
                headers={
                    "x-uploadthing-api-key": self.api_key,
                    "x-uploadthing-version": "7.0.0",
                    "Content-Type": "application/json",
                },
                timeout=5,
            )

            logger.info(f"Credentials check response: {response.status_code}")
            if response.status_code != 200:
                logger.error(
                    f"API returned status {response.status_code}: {response.text}"
                )
                return False

            app_info = response.json()
            logger.debug(f"App info: {app_info}")

            # Verify app ID matches
            if app_info.get("appId") != self.app_id:
                logger.error("App ID mismatch")
                return False

            return True

        except Exception as e:
            logger.error(f"Error verifying credentials: {str(e)}")
            return False


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for the image uploader"""
    parser = argparse.ArgumentParser(description="Upload files to UTFS.io")
    parser.add_argument("--api-key", required=True, help="UTFS API key")
    parser.add_argument("--app-id", required=True, help="UTFS App ID")
    parser.add_argument(
        "--file-path", type=Path, help="Path to a single file to upload"
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Upload all images from local_images directory",
    )
    parser.add_argument(
        "--custom-id", help="Optional custom ID for the file", default=None
    )
    return parser


def main():
    """Main entry point for the image uploader"""
    parser = create_parser()
    args = parser.parse_args()

    if not args.batch and not args.file_path:
        parser.error("Either --file-path or --batch must be specified")

    try:
        uploader = UTFSUploader(api_key=args.api_key, app_id=args.app_id)

        # Check API health first
        if not uploader.check_api_health():
            logger.error("UploadThing API is not responding")
            return 1

        # Verify credentials
        if not uploader.verify_credentials():
            logger.error("Invalid API key or app ID")
            return 1

        if args.batch:
            # Update the path to be relative to the project root
            local_images_dir = Path(__file__).parent / "local_images"
            if not local_images_dir.exists() or not local_images_dir.is_dir():
                logger.error(f"local_images directory not found at {local_images_dir}")
                return 1

            # Process each image file in the directory
            for image_file in local_images_dir.glob("*"):
                if not image_file.is_file():
                    continue

                if not mimetypes.guess_type(image_file)[0] or not mimetypes.guess_type(
                    image_file
                )[0].startswith("image/"):
                    logger.warning(f"Skipping non-image file: {image_file}")
                    continue

                logger.info(f"Processing file: {image_file}")
                file_url = uploader.upload_file(file_path=image_file)

                if not file_url:
                    logger.error(f"Failed to upload {image_file}")
                    continue  # Changed from return 1 to continue to process other files

                logger.success(f"Successfully uploaded: {image_file} -> {file_url}")

        else:
            # Single file upload
            file_url = uploader.upload_file(
                file_path=args.file_path, custom_id=args.custom_id
            )
            if not file_url:
                logger.error(f"Failed to upload {args.file_path}")
                return 1
            logger.success(f"Successfully uploaded: {args.file_path} -> {file_url}")

        return 0

    except Exception as e:
        logger.error(f"Error during upload: {str(e)}")
        return 1


if __name__ == "__main__":
    exit(main())
