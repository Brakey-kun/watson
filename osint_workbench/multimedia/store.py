"""Artifact storage with deduplication and disk quota enforcement.

Manages local storage, retrieval, and lifecycle of multimedia artifacts.
Provides SHA-256 deduplication, LRU eviction of completed-investigation artifacts,
and configurable disk quota enforcement.
"""

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set

from osint_workbench.multimedia.models import MediaType


class StorageQuotaError(Exception):
    """Raised when a storage operation would exceed the configured disk quota."""

    pass


@dataclass
class StoredArtifact:
    """Metadata about a stored multimedia artifact."""

    artifact_id: str
    original_url: Optional[str]
    local_path: Path
    media_type: MediaType
    mime_type: str
    file_size_bytes: int
    sha256_hash: str
    stored_at: str  # ISO timestamp
    investigation_id: str


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe storage.

    Strips path separators (/ and \\), parent directory sequences (..),
    null bytes, control characters (U+0000-U+001F, U+007F-U+009F),
    OS-unsafe characters (: * ? " < > |), and truncates to 255 characters.
    Characters outside [alphanumeric, hyphen, underscore, dot] are replaced
    with underscores.
    """
    # Strip null bytes
    filename = filename.replace("\x00", "_")

    # Strip path separators
    filename = filename.replace("/", "_")
    filename = filename.replace("\\", "_")

    # Strip parent directory sequences
    filename = filename.replace("..", "_")

    # Strip control characters (U+0000-U+001F, U+007F-U+009F)
    filename = re.sub(r"[\u0000-\u001f\u007f-\u009f]", "_", filename)

    # Strip OS-unsafe characters: : * ? " < > |
    filename = re.sub(r'[:\*\?"<>|]', "_", filename)

    # Replace any remaining characters outside [alphanumeric, hyphen, underscore, dot]
    filename = re.sub(r"[^a-zA-Z0-9\-_.]", "_", filename)

    # Truncate to 255 characters
    filename = filename[:255]

    # If filename is empty after sanitization, use a default
    if not filename or filename.strip("._ ") == "":
        filename = "unnamed_artifact"

    return filename


def _compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hash of a file, reading in chunks for efficiency."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)  # 64KB chunks
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def _detect_media_type_from_mime(mime_type: str) -> MediaType:
    """Determine MediaType from a MIME type string."""
    if mime_type.startswith("image/"):
        return MediaType.IMAGE
    elif mime_type.startswith("video/"):
        return MediaType.VIDEO
    elif mime_type.startswith("audio/"):
        return MediaType.AUDIO
    elif mime_type.startswith("application/"):
        return MediaType.DOCUMENT
    return MediaType.UNKNOWN


def _guess_mime_type(file_path: Path) -> str:
    """Guess MIME type from file extension."""
    ext = file_path.suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".mp4": "video/mp4",
        ".avi": "video/avi",
        ".mkv": "video/mkv",
        ".webm": "video/webm",
        ".mov": "video/mov",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return mime_map.get(ext, "application/octet-stream")


class ArtifactStore:
    """Manages local storage of multimedia artifacts with deduplication and quota enforcement.

    Provides SHA-256 deduplication to avoid storing identical files, LRU eviction
    of completed-investigation artifacts when disk quota is approached, and
    investigation-scoped cleanup.
    """

    def __init__(self, storage_dir: Path, max_storage_mb: float = 2048.0) -> None:
        """Initialize the ArtifactStore.

        Args:
            storage_dir: Directory for storing artifact files.
            max_storage_mb: Maximum storage quota in megabytes (1-10000).
        """
        if max_storage_mb < 1 or max_storage_mb > 10000:
            raise ValueError("max_storage_mb must be between 1 and 10000")

        self._storage_dir = storage_dir
        self._max_storage_mb = max_storage_mb

        # Create storage directory if it doesn't exist
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # In-memory metadata store: artifact_id -> StoredArtifact
        self._artifacts: Dict[str, StoredArtifact] = {}

        # SHA-256 hash -> artifact_id for deduplication
        self._hash_index: Dict[str, str] = {}

        # Set of completed investigation IDs
        self._completed_investigations: Set[str] = set()

    @property
    def storage_dir(self) -> Path:
        """Return the storage directory path."""
        return self._storage_dir

    def store(
        self,
        source_path: Path,
        investigation_id: str,
        url: Optional[str] = None,
        media_type: Optional[MediaType] = None,
        mime_type: Optional[str] = None,
    ) -> StoredArtifact:
        """Store a multimedia file with deduplication and quota enforcement.

        Args:
            source_path: Path to the source file to store.
            investigation_id: ID of the investigation this artifact belongs to.
            url: Optional original URL of the artifact.
            media_type: Optional media type override.
            mime_type: Optional MIME type override.

        Returns:
            StoredArtifact with metadata about the stored file.

        Raises:
            FileNotFoundError: If source_path does not exist.
            PermissionError: If source_path is not readable.
            StorageQuotaError: If the file cannot be stored within quota.
            OSError: If an I/O error occurs during copy.
        """
        # Validate source file exists and is readable (Req 8.7)
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Source file does not exist: {source_path}")
        if not os.access(source_path, os.R_OK):
            raise PermissionError(f"Source file is not readable: {source_path}")

        # Get file size
        file_size_bytes = source_path.stat().st_size

        # Check if single file exceeds total quota (Req 9.4)
        file_size_mb = file_size_bytes / (1024 * 1024)
        if file_size_mb > self._max_storage_mb:
            raise StorageQuotaError(
                f"File size ({file_size_mb:.2f} MB) exceeds maximum storage quota "
                f"({self._max_storage_mb:.2f} MB)"
            )

        # Compute SHA-256 hash (Req 8.1)
        sha256_hash = _compute_sha256(source_path)

        # Deduplication check (Req 8.2)
        existing_id = self.is_duplicate(sha256_hash)
        if existing_id is not None:
            return self._artifacts[existing_id]

        # Enforce disk quota with LRU eviction (Req 9.1, 9.2, 9.3)
        self._enforce_quota(file_size_bytes)

        # Generate artifact ID (Req 8.4)
        artifact_id = str(uuid.uuid4())

        # Determine MIME type and media type
        resolved_mime_type = mime_type if mime_type else _guess_mime_type(source_path)
        resolved_media_type = (
            media_type if media_type else _detect_media_type_from_mime(resolved_mime_type)
        )

        # Sanitize filename (Req 8.6, 14.2)
        safe_filename = _sanitize_filename(source_path.name)
        # Prefix with artifact_id to ensure uniqueness
        dest_filename = f"{artifact_id}_{safe_filename}"
        dest_path = self._storage_dir / dest_filename

        # Copy file to storage (Req 8.3, 8.8)
        try:
            shutil.copy2(str(source_path), str(dest_path))
        except OSError as e:
            # Clean up partial write (Req 8.8)
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except OSError:
                    pass
            raise OSError(f"I/O error during file copy: {e}") from e

        # Create StoredArtifact metadata
        stored_artifact = StoredArtifact(
            artifact_id=artifact_id,
            original_url=url,
            local_path=dest_path,
            media_type=resolved_media_type,
            mime_type=resolved_mime_type,
            file_size_bytes=file_size_bytes,
            sha256_hash=sha256_hash,
            stored_at=datetime.now(timezone.utc).isoformat(),
            investigation_id=investigation_id,
        )

        # Register in indexes
        self._artifacts[artifact_id] = stored_artifact
        self._hash_index[sha256_hash] = artifact_id

        return stored_artifact

    def retrieve(self, artifact_id: str) -> Optional[StoredArtifact]:
        """Retrieve stored artifact metadata by artifact_id.

        Args:
            artifact_id: The UUID4 identifier of the artifact.

        Returns:
            StoredArtifact metadata, or None if not found. (Req 8.5)
        """
        return self._artifacts.get(artifact_id)

    def delete(self, artifact_id: str) -> bool:
        """Delete a stored artifact by artifact_id.

        Args:
            artifact_id: The UUID4 identifier of the artifact to delete.

        Returns:
            True if the artifact was found and deleted, False otherwise.
        """
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return False

        # Remove the file from disk
        if artifact.local_path.exists():
            try:
                artifact.local_path.unlink()
            except OSError:
                pass

        # Remove from indexes
        del self._artifacts[artifact_id]
        if artifact.sha256_hash in self._hash_index:
            del self._hash_index[artifact.sha256_hash]

        return True

    def cleanup_investigation(self, investigation_id: str) -> int:
        """Delete all artifacts for a given investigation.

        Args:
            investigation_id: The investigation to clean up.

        Returns:
            Count of deleted artifacts. (Req 9.5)
        """
        # Find all artifacts for the investigation
        to_delete = [
            aid
            for aid, artifact in self._artifacts.items()
            if artifact.investigation_id == investigation_id
        ]

        # Delete each one
        count = 0
        for artifact_id in to_delete:
            if self.delete(artifact_id):
                count += 1

        return count

    def get_disk_usage_mb(self) -> float:
        """Return current storage consumption in megabytes.

        Calculates based on tracked artifact file sizes. (Req 9.6)

        Returns:
            Current disk usage in MB, within 1 MB accuracy.
        """
        total_bytes = sum(a.file_size_bytes for a in self._artifacts.values())
        return total_bytes / (1024 * 1024)

    def is_duplicate(self, sha256_hash: str) -> Optional[str]:
        """Check if a file with the given SHA-256 hash already exists.

        Args:
            sha256_hash: The SHA-256 hash to check.

        Returns:
            The artifact_id of the existing duplicate, or None if not found.
        """
        return self._hash_index.get(sha256_hash)

    def mark_investigation_completed(self, investigation_id: str) -> None:
        """Mark an investigation as completed, making its artifacts eligible for eviction.

        Args:
            investigation_id: The investigation to mark as completed.
        """
        self._completed_investigations.add(investigation_id)

    def _enforce_quota(self, incoming_size_bytes: int) -> None:
        """Enforce disk quota, evicting completed-investigation artifacts if needed.

        Uses LRU eviction: artifacts from completed investigations are evicted
        in order of oldest stored_at timestamp first. (Req 9.1, 9.2, 9.3)

        Args:
            incoming_size_bytes: Size of the file about to be stored.

        Raises:
            StorageQuotaError: If quota cannot be satisfied even after eviction.
        """
        current_usage_bytes = sum(a.file_size_bytes for a in self._artifacts.values())
        max_bytes = self._max_storage_mb * 1024 * 1024

        # Check if we're within quota
        if current_usage_bytes + incoming_size_bytes <= max_bytes:
            return

        # Need to evict — gather candidates from completed investigations (Req 9.2)
        eviction_candidates = [
            artifact
            for artifact in self._artifacts.values()
            if artifact.investigation_id in self._completed_investigations
        ]

        # Sort by stored_at (oldest first) for LRU eviction
        eviction_candidates.sort(key=lambda a: a.stored_at)

        # Evict until we have enough space
        for candidate in eviction_candidates:
            self.delete(candidate.artifact_id)
            current_usage_bytes -= candidate.file_size_bytes
            if current_usage_bytes + incoming_size_bytes <= max_bytes:
                return

        # If we still can't free enough, raise error (Req 9.3)
        raise StorageQuotaError(
            f"Cannot free sufficient space. Need {incoming_size_bytes / (1024 * 1024):.2f} MB "
            f"but only {(max_bytes - current_usage_bytes) / (1024 * 1024):.2f} MB available "
            f"after evicting all completed-investigation artifacts."
        )
