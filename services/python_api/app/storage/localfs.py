#!/usr/bin/env python3
"""
Local filesystem storage driver for copy and checksum operations.

Features:
- Resolve file:// URIs and local paths with an optional root sandbox.
- Copy a single file with streaming I/O and checksums (SHA-256 by default).
- Copy a directory tree recursively with per-file checksums.
- Atomic writes via temp files + os.replace to avoid partial targets.
- Checksum verification helpers.
- Safe parent directory creation and fsync on writes.

Intended Usage:
- As a local/on‑prem storage driver for the Python policy/analytics service and tests.
- For real migrations, pair with a mover/orchestrator (Go or Python) that uses this driver.

Security/Safety:
- If a `root` is provided, all resolved paths must live under this root (prevents path traversal).
- Uses atomic replace to avoid partially written files at final destinations.
- Does not preserve symlinks or extended attributes; follows regular files only.

Note:
- This driver is scoped to local file:// URIs. Cloud drivers (GCS/S3) should be separate implementations.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse, unquote

LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:
    # Default logging setup if not configured by app
    logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Base class for storage errors."""


class PathOutsideRootError(StorageError):
    """Raised when a resolved path is outside of the configured root sandbox."""


class OverwriteError(StorageError):
    """Raised when a destination exists and overwrite=False."""


class CopyError(StorageError):
    """Raised when a copy action fails."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class CopyFileResult:
    src: str
    dst: str
    bytes_copied: int
    checksum_alg: str
    checksum_hex: str
    duration_s: float


@dataclass
class CopyTreeResult:
    src_root: str
    dst_root: str
    files_copied: int
    bytes_copied: int
    checksum_alg: str
    per_file: List[CopyFileResult] = field(default_factory=list)
    duration_s: float = 0.0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _is_file(path: str) -> bool:
    try:
        return Path(path).is_file()
    except Exception:
        return False


def _is_dir(path: str) -> bool:
    try:
        return Path(path).is_dir()
    except Exception:
        return False


def _ensure_parent_dir(dst_path: str) -> None:
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)


def _realpath(p: str) -> str:
    return os.path.realpath(p)


def _atomic_write_stream(
    src_path: str,
    dst_path: str,
    chunk_size: int = 16 * 1024 * 1024,
    checksum_alg: str = "sha256",
) -> Tuple[int, str]:
    """
    Stream copy src->tmp, compute checksum while reading, fsync, then atomic rename tmp->dst.
    Returns (bytes_copied, checksum_hex).
    """
    hasher = hashlib.new(checksum_alg)
    bytes_copied = 0

    # Write into a temp file in the same directory for atomic replace
    dst_dir = os.path.dirname(dst_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".copy_tmp_", dir=dst_dir)
    try:
        with os.fdopen(fd, "wb") as out_f, open(src_path, "rb") as in_f:
            while True:
                buf = in_f.read(chunk_size)
                if not buf:
                    break
                hasher.update(buf)
                out_f.write(buf)
                bytes_copied += len(buf)
            out_f.flush()
            os.fsync(out_f.fileno())

        # Atomic replace
        os.replace(tmp_path, dst_path)
    except Exception as e:
        # Clean best-effort
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise CopyError(f"Failed to copy file {src_path} -> {dst_path}: {e}") from e

    return bytes_copied, hasher.hexdigest()


def _validate_overwrite(dst_path: str, overwrite: bool) -> None:
    if os.path.exists(dst_path) and not overwrite:
        raise OverwriteError(f"Destination exists and overwrite=False: {dst_path}")


def _parse_file_uri(uri_or_path: str) -> str:
    """
    Parse file:// URIs; for plain paths, return as-is.
    - file:///abs/path -> /abs/path
    - /abs/path       -> /abs/path
    - relative/path   -> relative/path (will be resolved with root if provided)
    """
    if uri_or_path.startswith("file://"):
        parsed = urlparse(uri_or_path)
        # Support file://localhost/path and file:///path
        raw_path = parsed.path
        if not raw_path:
            raise ValueError(f"Invalid file URI: {uri_or_path}")
        return unquote(raw_path)
    return uri_or_path


def _enforce_root(path: str, root: Optional[str]) -> str:
    """
    If root is provided, ensure path resolves under root.
    Returns the realpath for safety.
    """
    rp = _realpath(path)
    if not root:
        return rp
    rr = _realpath(root)
    # Allow exact root or any subpath under it
    if rp == rr or rp.startswith(rr + os.sep):
        return rp
    raise PathOutsideRootError(f"Path {rp} is outside of root sandbox {rr}")


# ---------------------------------------------------------------------------
# LocalFSStorage
# ---------------------------------------------------------------------------


class LocalFSStorage:
    """
    Local filesystem storage driver.

    Args:
        root: Optional sandbox directory. All resolved paths must lie under this root.
        follow_symlinks: When True, copy() will follow symlinks as regular files.
                         For directories, os.walk(followlinks=False) by default to avoid loops.

    Example:
        s = LocalFSStorage(root=os.getenv("STORAGE_DRIVER_LOCAL_ROOT", "/shared_storage"))
        s.copy("file:///shared_storage/src/file.bin", "file:///shared_storage/dst/file.bin")
    """

    def __init__(
        self, root: Optional[str] = None, follow_symlinks: bool = True
    ) -> None:
        self.root = root
        self.follow_symlinks = follow_symlinks

    # ------------- Path resolution -------------

    def resolve(self, uri_or_path: str) -> str:
        raw = _parse_file_uri(uri_or_path)
        # If a relative path is given and root exists, resolve relative to root
        if not os.path.isabs(raw) and self.root:
            raw = os.path.join(self.root, raw)
        return _enforce_root(raw, self.root)

    # ------------- Checksums -------------

    def checksum(
        self,
        uri_or_path: str,
        algorithm: str = "sha256",
        chunk_size: int = 8 * 1024 * 1024,
    ) -> str:
        """Compute checksum for a single file."""
        path = self.resolve(uri_or_path)
        if not _is_file(path):
            raise StorageError(f"Not a file for checksum: {path}")
        h = hashlib.new(algorithm)
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()

    def verify_checksum(
        self, uri_or_path: str, expected_hex: str, algorithm: str = "sha256"
    ) -> bool:
        """Return True if computed checksum matches expected_hex."""
        actual = self.checksum(uri_or_path, algorithm=algorithm)
        return actual.lower() == (expected_hex or "").lower()

    # ------------- Copy single file -------------

    def copy_file(
        self,
        src_uri: str,
        dst_uri: str,
        *,
        overwrite: bool = False,
        checksum_alg: str = "sha256",
        chunk_size: int = 16 * 1024 * 1024,
    ) -> CopyFileResult:
        """
        Copy a single file with streaming and checksums. Uses atomic replace for destination.

        Returns:
            CopyFileResult with bytes, checksum, and timing.
        """
        start = time.time()
        src = self.resolve(src_uri)
        dst = self.resolve(dst_uri)

        if not _is_file(src):
            raise StorageError(f"Source is not a file: {src}")

        _ensure_parent_dir(dst)
        _validate_overwrite(dst, overwrite)

        bytes_copied, checksum_hex = _atomic_write_stream(
            src_path=src, dst_path=dst, chunk_size=chunk_size, checksum_alg=checksum_alg
        )
        dur = time.time() - start

        LOGGER.info(
            "Copied file src=%s dst=%s bytes=%d alg=%s sum=%s dur=%.3fs",
            src,
            dst,
            bytes_copied,
            checksum_alg,
            checksum_hex,
            dur,
        )

        return CopyFileResult(
            src=src,
            dst=dst,
            bytes_copied=bytes_copied,
            checksum_alg=checksum_alg,
            checksum_hex=checksum_hex,
            duration_s=dur,
        )

    # ------------- Copy directory tree -------------

    def copy_tree(
        self,
        src_uri: str,
        dst_uri: str,
        *,
        overwrite: bool = False,
        checksum_alg: str = "sha256",
        chunk_size: int = 16 * 1024 * 1024,
        include_hidden: bool = True,
    ) -> CopyTreeResult:
        """
        Recursively copy a directory tree. Copies regular files only.

        Policy:
        - Creates destination directories as needed.
        - If overwrite=False and a file exists at destination, raises OverwriteError.
        - Skips special files (device, fifo); follows symlinks if follow_symlinks=True
          when copying individual files (os.walk does not follow links by default).

        Returns:
            CopyTreeResult with per-file results and aggregate stats.
        """
        start = time.time()
        src_root = self.resolve(src_uri)
        dst_root = self.resolve(dst_uri)

        if not _is_dir(src_root):
            raise StorageError(f"Source is not a directory: {src_root}")

        Path(dst_root).mkdir(parents=True, exist_ok=True)

        per_file: List[CopyFileResult] = []
        total_bytes = 0
        files_copied = 0
        errors: List[str] = []

        for dirpath, dirnames, filenames in os.walk(
            src_root, topdown=True, followlinks=False
        ):
            # Optionally filter hidden files/dirs
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                filenames = [f for f in filenames if not f.startswith(".")]

            rel = os.path.relpath(dirpath, src_root)
            rel = "" if rel == "." else rel
            dst_dir = os.path.join(dst_root, rel)
            Path(dst_dir).mkdir(parents=True, exist_ok=True)

            for name in filenames:
                src_file = os.path.join(dirpath, name)
                dst_file = os.path.join(dst_dir, name)

                try:
                    # Validate overwrite for this file
                    _validate_overwrite(dst_file, overwrite)

                    # Copy only regular files; follow symlinks optionally by opening them (default behavior).
                    if not os.path.isfile(src_file) and not (
                        self.follow_symlinks and os.path.islink(src_file)
                    ):
                        LOGGER.warning("Skipping non-regular file: %s", src_file)
                        continue

                    result = self.copy_file(
                        src_file,
                        dst_file,
                        overwrite=True,  # already validated per-file overwrite
                        checksum_alg=checksum_alg,
                        chunk_size=chunk_size,
                    )
                    per_file.append(result)
                    total_bytes += result.bytes_copied
                    files_copied += 1
                except OverwriteError as e:
                    # Bubble up overwrite errors to caller
                    raise
                except Exception as e:
                    err = f"Failed to copy {src_file} -> {dst_file}: {e}"
                    LOGGER.error(err)
                    errors.append(err)

        dur = time.time() - start
        return CopyTreeResult(
            src_root=src_root,
            dst_root=dst_root,
            files_copied=files_copied,
            bytes_copied=total_bytes,
            checksum_alg=checksum_alg,
            per_file=per_file,
            duration_s=dur,
            errors=errors,
        )

    # ------------- High level dispatcher -------------

    def copy_any(
        self,
        src_uri: str,
        dst_uri: str,
        *,
        overwrite: bool = False,
        checksum_alg: str = "sha256",
        chunk_size: int = 16 * 1024 * 1024,
    ):
        """
        Copy either a single file or a directory tree, based on source type.
        Returns:
            CopyFileResult or CopyTreeResult
        """
        src_path = self.resolve(src_uri)
        if _is_file(src_path):
            return self.copy_file(
                src_uri,
                dst_uri,
                overwrite=overwrite,
                checksum_alg=checksum_alg,
                chunk_size=chunk_size,
            )
        elif _is_dir(src_path):
            return self.copy_tree(
                src_uri,
                dst_uri,
                overwrite=overwrite,
                checksum_alg=checksum_alg,
                chunk_size=chunk_size,
            )
        else:
            raise StorageError(f"Source not found or unsupported type: {src_path}")


# ---------------------------------------------------------------------------
# Optional CLI for quick manual testing
# ---------------------------------------------------------------------------


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="LocalFSStorage copy with checksum")
    parser.add_argument("src", help="Source path or file:// URI")
    parser.add_argument("dst", help="Destination path or file:// URI")
    parser.add_argument(
        "--root", default=os.getenv("STORAGE_DRIVER_LOCAL_ROOT"), help="Sandbox root"
    )
    parser.add_argument("--overwrite", action="store_true", help="Allow overwrite")
    parser.add_argument(
        "--alg", default="sha256", help="Checksum algorithm (default: sha256)"
    )
    args = parser.parse_args()

    drv = LocalFSStorage(root=args.root)
    res = drv.copy_any(
        args.src, args.dst, overwrite=args.overwrite, checksum_alg=args.alg
    )
    print(res)


if __name__ == "__main__":
    _cli()
