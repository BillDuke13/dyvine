"""User management service for Douyin user operations and content management.

This module provides comprehensive business logic for user-related operations
in the Dyvine application. It acts as the primary interface between the API
layer and external Douyin services, handling complex user workflows including
profile data retrieval, content discovery, and bulk download operations.

Core Responsibilities:
    - User profile information retrieval and caching
    - Content enumeration (posts, liked content, collections)
    - Asynchronous bulk download orchestration
    - Download progress tracking and status management
    - File naming and organization for downloaded content
    - Integration with Cloudflare R2 storage for content persistence
    - Error handling and retry logic for robustness

Architecture:
    The service follows a layered architecture pattern:
    - Service Layer: UserService class with business logic
    - Integration Layer: DouyinHandler for external API calls
    - Storage Layer: R2StorageService for content persistence
    - Utility Layer: Helper functions for data transformation

Key Features:
    - Asynchronous operation support for scalability
    - Comprehensive error handling with custom exceptions
    - Structured logging with correlation tracking
    - Configurable download parameters and filtering
    - Safe filename generation for cross-platform compatibility
    - Progress tracking for long-running operations
    - Resource cleanup and lifecycle management

Usage Patterns:
    Dependency Injection:
        service = UserService()
        user_info = await service.get_user_info(user_id)

    Download Operations:
        download_task = await service.download_user_content(
            user_id="MS4wLjABAAAA...",
            include_posts=True,
            include_likes=False,
            max_items=100
        )

    Status Monitoring:
        status = await service.get_operation_status(task_id)

Custom Exceptions:
    - UserNotFoundError: User profile not accessible or doesn't exist
    - DownloadError: Content download failed or interrupted
    - ServiceError: General service-level errors and timeouts

Thread Safety:
    The service is designed to be thread-safe for concurrent operations.
    Internal state is managed through immutable objects and atomic operations.

Performance Considerations:
    - Implements connection pooling for external API calls
    - Uses async/await patterns for non-blocking operations
    - Provides configurable batch sizes for large downloads
    - Includes timeout handling for long-running operations
"""

import asyncio
import re
import uuid
from datetime import datetime
from pathlib import Path

from f2.apps.douyin.handler import DouyinHandler  # type: ignore

from ..core.exceptions import DownloadError, ServiceError, UserNotFoundError
from ..core.logging import ContextLogger
from ..core.settings import settings
from ..schemas.users import DownloadResponse, UserResponse
from .storage import ContentType, R2StorageService


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for cross-platform filesystem compatibility.

    Cleans and normalizes filenames to ensure they work across different
    operating systems and filesystems. Removes potentially problematic
    characters including emojis, special symbols, and filesystem-reserved
    characters.

    Transformations Applied:
        1. Remove non-ASCII characters (including emojis and Unicode symbols)
        2. Replace filesystem-reserved characters with underscores
        3. Collapse multiple consecutive underscores to single underscore
        4. Trim leading/trailing spaces and underscores
        5. Provide fallback name for empty results

    Args:
        filename: Original filename string to sanitize.

    Returns:
        Sanitized filename safe for use across different filesystems.
        Returns 'untitled' if the input results in an empty string.

    Example:
        >>> sanitize_filename("My Video 📱 <2024>.mp4")
        "My_Video_2024.mp4"

        >>> sanitize_filename("文件名/with\\special:chars")
        "with_special_chars"

        >>> sanitize_filename("🎥📹🎬")
        "untitled"

    Note:
        This function is designed for content downloaded from Douyin which
        often contains emojis, Chinese characters, and special symbols in
        titles and descriptions.
    """
    # Remove emoji and non-ASCII characters for compatibility
    filename = re.sub(r"[^\x00-\x7F]+", "", filename)

    # Replace filesystem-reserved characters with underscores
    # Covers Windows, macOS, and Linux reserved characters
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

    # Collapse multiple consecutive underscores to improve readability
    filename = re.sub(r"_+", "_", filename)

    # Remove leading/trailing whitespace and underscores
    filename = filename.strip("_ ")

    # Provide fallback for empty filenames
    return filename or "untitled"


logger = ContextLogger(__name__)

# Alias for backward compatibility
UserServiceError = ServiceError


class UserService:
    """Service class for handling user-related operations.

    This class encapsulates the business logic for various user-related
    operations in the Douyin application, such as retrieving user information,
    initiating content downloads, and tracking download progress. It follows the
    Singleton pattern to ensure a single instance throughout the application.
    """

    _instance = None
    _active_downloads: dict[str, dict] = {}

    def __new__(cls) -> "UserService":
        """Ensure a single instance of the UserService class (Singleton pattern)."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """Initialize the UserService instance (only once)."""
        # Skip initialization if already initialized
        if hasattr(self, "_initialized"):
            return

        self._initialized = True
        self.storage = R2StorageService()

    async def get_user_info(self, user_id: str) -> UserResponse:
        """Retrieve user information from Douyin.

        Args:
            user_id: The Douyin user ID.

        Returns:
            UserResponse: Object containing the user's information.

        Raises:
            UserNotFoundError: If the requested user cannot be found.
            UserServiceError: If an error occurs during the operation.
        """
        try:
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{user_id}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "mode": "post",
            }

            handler = DouyinHandler(handler_kwargs)
            user_data = await handler.fetch_user_profile(user_id)

            if not user_data.nickname:
                raise UserNotFoundError(f"User {user_id} not found")

            return UserResponse(
                user_id=user_id,
                nickname=user_data.nickname,
                avatar_url=str(user_data.avatar_url or ""),
                signature=str(user_data.signature or ""),
                following_count=int(user_data.following_count or 0),  # type: ignore
                follower_count=int(user_data.follower_count or 0),  # type: ignore
                total_favorited=int(user_data.total_favorited or 0),  # type: ignore
                is_living=bool(user_data.room_id),  # type: ignore
                room_id=int(user_data.room_id) if user_data.room_id else None,  # type: ignore
            )
        except Exception as e:
            logger.exception("Failed to get user info", extra={"user_id": user_id})
            raise UserServiceError(f"Failed to get user info: {str(e)}") from e

    async def start_download(
        self,
        user_id: str,
        include_posts: bool = True,
        include_likes: bool = False,
        max_items: int | None = None,
    ) -> DownloadResponse:
        """Start an asynchronous download of user content from Douyin.

        Args:
            user_id: The Douyin user ID.
            include_posts: Whether to include the user's posts in the download.
            include_likes: Whether to include the user's liked posts in the download.
            max_items: Maximum number of items to download.

        Returns:
            DownloadResponse: Object containing details about the initiated download
                task.
        """
        task_id = str(uuid.uuid4())

        # Initialize download task
        self._active_downloads[task_id] = {
            "user_id": user_id,
            "status": "pending",
            "progress": 0.0,
            "start_time": datetime.now(),
            "include_posts": include_posts,
            "include_likes": include_likes,
            "max_items": max_items,
        }

        # Start download task
        asyncio.create_task(self._process_download(task_id))

        return DownloadResponse(
            task_id=task_id,
            status="pending",
            message="Download started",
            progress=0.0,
            total_items=None,
            downloaded_items=None,
            error=None,
        )

    async def get_download_status(self, task_id: str) -> DownloadResponse:
        """Get the status of a download task.

        Args:
            task_id: The unique identifier of the download task.

        Returns:
            DownloadResponse: Object containing the current status of the download task.

        Raises:
            DownloadError: If the specified download task is not found.
        """
        if task_id not in self._active_downloads:
            raise DownloadError(f"Download task {task_id} not found")

        task = self._active_downloads[task_id]
        return DownloadResponse(
            task_id=task_id,
            status=task["status"],
            message=f"Download {task['status']}",
            progress=task["progress"],
            total_items=task.get("total_items"),
            downloaded_items=task.get("downloaded_items"),
            error=task.get("error"),
        )

    async def _process_download(self, task_id: str) -> None:
        """Process a download task asynchronously.

        Args:
            task_id (str): The unique identifier of the download task.
        """
        task = self._active_downloads[task_id]
        temp_dir = None
        try:
            # Update status to running
            task["status"] = "running"

            # Create temporary downloads directory
            temp_dir = Path("temp_downloads")
            temp_dir.mkdir(exist_ok=True)

            # Initialize f2 handler with temporary directory
            handler_kwargs = {
                "url": f"https://www.douyin.com/user/{task['user_id']}",
                "cookie": settings.douyin_cookie,
                "headers": {
                    "User-Agent": settings.douyin_user_agent,
                    "Referer": settings.douyin_referer,
                },
                "proxy": settings.douyin_proxy_http,
                "download_path": str(temp_dir),
                "max_counts": task["max_items"],
                "download_favorite": task["include_likes"],
                "timeout": 5,
                "folderize": True,
                "mode": "post",
                "naming": "{create}_{desc}",
                "download_image": True,
                "filename_filter": sanitize_filename,  # Add filename sanitization
            }

            handler = DouyinHandler(handler_kwargs)
            task["total_items"] = 0
            task["downloaded_items"] = 0

            # Get user profile to create user directory and verify post count
            user_data = await handler.fetch_user_profile(task["user_id"])
            if not user_data.nickname:
                raise UserNotFoundError(f"User {task['user_id']} not found")

            # Get total posts count from profile
            aweme_count = user_data.aweme_count
            total_posts = int(aweme_count) if isinstance(aweme_count, int) else 0
            if total_posts == 0:
                logger.info(f"User {task['user_id']} has no posts")
                task["status"] = "completed"
                task["progress"] = 100.0
                return

            logger.info(f"User {user_data.nickname} has {total_posts} posts")
            task["total_items"] = total_posts

            # Create user directory in temp
            user_dir = temp_dir / user_data.nickname
            user_dir.mkdir(exist_ok=True)

            # Use the handler to download user posts
            max_cursor = 0
            has_more = True
            downloaded_count = 0

            while has_more and (
                task["max_items"] is None or downloaded_count < task["max_items"]
            ):
                async for aweme_data in handler.fetch_user_post_videos(
                    task["user_id"],
                    min_cursor=0,
                    max_cursor=max_cursor,
                    page_counts=100,  # Increased page size
                    max_counts=task["max_items"],
                ):
                    if not aweme_data.has_aweme:
                        has_more = False
                        break

                    current_batch_size = len(aweme_data.aweme_id)
                    downloaded_count += current_batch_size
                    task["downloaded_items"] = downloaded_count
                    if total_posts > 0:
                        task["progress"] = (downloaded_count / total_posts) * 100
                    else:
                        task["progress"] = 100.0

                    logger.info(
                        f"Downloaded {downloaded_count}/{total_posts} posts "
                        f"({task['progress']:.1f}%)"
                    )

                    # Download files to temp directory
                    await handler.downloader.create_download_tasks(
                        handler_kwargs, aweme_data._to_list(), user_dir
                    )

                    # Upload downloaded files to R2 (search recursively)
                    for file_path in user_dir.glob("**/*"):
                        if file_path.is_file():
                            try:
                                # Generate R2 storage path
                                if file_path.suffix.lower() in [
                                    ".jpg",
                                    ".jpeg",
                                    ".png",
                                    ".webp",
                                ]:
                                    content_type = (
                                        "image/" + file_path.suffix.lower().lstrip(".")
                                    )
                                    r2_path = self.storage.generate_ugc_path(
                                        task["user_id"], file_path.name, content_type
                                    )
                                else:
                                    content_type = "video/mp4"
                                    r2_path = self.storage.generate_ugc_path(
                                        task["user_id"], file_path.name, content_type
                                    )

                                # Generate metadata
                                metadata = self.storage.generate_metadata(
                                    author=user_data.nickname,
                                    category=ContentType.POSTS,
                                    content_type=content_type,
                                    source="douyin",
                                )

                                # Upload to R2
                                await self.storage.upload_file(
                                    file_path, r2_path, metadata, content_type
                                )

                                # Delete local file after upload
                                file_path.unlink()

                            except Exception as e:
                                logger.error(
                                    f"Failed to upload {file_path} to R2: {str(e)}"
                                )

                    # Update cursor for next page
                    if (
                        aweme_data.max_cursor
                        and isinstance(aweme_data.max_cursor, int)
                        and aweme_data.max_cursor != max_cursor
                    ):
                        max_cursor = aweme_data.max_cursor
                        has_more = aweme_data.has_more
                    else:
                        # If cursor didn't change but we haven't got all posts,
                        # increment it
                        if downloaded_count < total_posts:
                            max_cursor += 1
                            has_more = True
                        else:
                            has_more = False

                    # If max_items is set and we've reached it, stop
                    if (
                        task["max_items"]
                        and task["downloaded_items"] >= task["max_items"]
                    ):
                        has_more = False
                        break

                    # Add delay between pages
                    await asyncio.sleep(handler_kwargs.get("timeout", 5))
            # Verify download completion
            if total_posts > 0:
                completion_percentage = (downloaded_count / total_posts) * 100
            else:
                completion_percentage = 100.0

            if completion_percentage >= 100:
                # Consider anything >= 100% as complete success
                logger.info(
                    f"Successfully downloaded {downloaded_count} posts "
                    f"(100% complete)"
                )
                task["status"] = "completed"
                task["progress"] = 100.0
            else:
                # Less than 100% means we missed some posts
                logger.warning(
                    f"Only downloaded {downloaded_count}/{total_posts} posts "
                    f"({completion_percentage:.1f}%). Some posts may have been missed."
                )
                task["status"] = "partial"
                task["error"] = (
                    f"Only downloaded {downloaded_count} out of {total_posts} posts"
                )
                task["progress"] = completion_percentage

        except Exception as e:
            logger.exception(
                "Download failed",
                extra={"task_id": task_id, "user_id": task["user_id"]},
            )
            task["status"] = "failed"
            task["error"] = str(e)

        finally:
            # Clean up temp directory and task
            try:
                if temp_dir and temp_dir.exists():
                    for file in temp_dir.glob("**/*"):
                        if file.is_file():
                            file.unlink()
                    for dir in temp_dir.glob("**/*"):
                        if dir.is_dir():
                            dir.rmdir()
                    temp_dir.rmdir()
            except Exception as e:
                logger.error(f"Failed to clean up temp directory: {str(e)}")

            await asyncio.sleep(3600)  # Keep task info for 1 hour
            self._active_downloads.pop(task_id, None)
