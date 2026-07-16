"""Shared MarkItDown-based conversion engine."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse


EXISTING_FILE_ACTIONS = {"skip", "replace"}
MSG_RECEIVED_TIME_PROPERTY_ID = 0x0E06
MSG_SENT_TIME_PROPERTY_ID = 0x0039
MSG_SYSTIME_PROPERTY_TYPE = 0x0040
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")
OUTLOOK_SAFELINK_RE = re.compile(
    r"<?https://[^\s<>]*safelinks\.protection\.outlook\.com[^\s<>]*>?",
    re.IGNORECASE,
)
MSG_BODY_STREAMS = (
    ("__substg1.0_1000001F", "utf-16-le"),
    ("__substg1.0_1000001E", "cp1252"),
)
MSG_STRING_STREAM_TYPES = (
    ("001F", "utf-16-le"),
    ("001E", "cp1252"),
)
EMAIL_REPLY_HEADER_RE = re.compile(
    r"^\s*(?:>+\s*)?(?:From|Sent|To|Cc|Subject):\s+",
    re.IGNORECASE,
)
EMAIL_REPLY_INTRO_RE = re.compile(r"^\s*On .+ wrote:\s*$", re.IGNORECASE)
EMAIL_SIGNOFF_RE = re.compile(
    r"^\s*(?:kind regards|best regards|warm regards|regards|many thanks|"
    r"thanks|thank you|cheers|yours sincerely|sincerely)[,!.\s]*$",
    re.IGNORECASE,
)
EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+", re.IGNORECASE)
PHONE_RE = re.compile(
    r"^\s*(?:m|mobile|main|phone|tel|telephone|direct|d|p|t)\s*[:|]\s*[\d\s()+-]+",
    re.IGNORECASE,
)
PHONE_NUMBER_ONLY_RE = re.compile(r"^\s*\+?\d[\d\s().-]{6,}\s*$")
URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
ADDRESS_TOKEN_RE = re.compile(
    r"\b(?:"
    r"level|suite|unit|floor|street|st|road|rd|avenue|ave|drive|dr|"
    r"lane|ln|way|boulevard|blvd|po box|gpo box|"
    r"nsw|vic|qld|sa|wa|tas|act|nt|australia"
    r")\b",
    re.IGNORECASE,
)
ROLE_LINE_RE = re.compile(
    r"\b(?:"
    r"partner|director|manager|adviser|advisor|consultant|accountant|"
    r"analyst|associate|principal|lawyer|solicitor|counsel|assistant|"
    r"coordinator|specialist|officer|lead|head|tax|audit|wealth|"
    r"business advisory|financial planner|client services"
    r")\b",
    re.IGNORECASE,
)
SIGNATURE_FOOTER_TOKEN_RE = re.compile(
    r"(?:"
    r"safelinks\.protection\.outlook\.com|"
    r"disclaimer|professional standards legislation|"
    r"liability limited|"
    r"\babn\b|"
    r"confidential|privileged|intended recipient|"
    r"received this (?:e-?mail|message) in error|"
    r"virus|malware|unsubscribe|privacy policy|"
    r"please consider the environment|"
    r"facebook\.com|linkedin\.com|"
    r"the title .?partner"
    r")",
    re.IGNORECASE,
)


class ConversionStatus(str, Enum):
    """Outcome for a single source file."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class ConversionOptions:
    """Options shared by GUI, CLI and watch mode."""

    recursive: bool = True
    skip_existing: bool = True
    overwrite: bool = False
    existing_file_action: str = "skip"
    prefer_plain_text_body: bool = True
    extract_attachments: bool = True
    list_attachments: bool = True

    def __post_init__(self) -> None:
        """Normalise old skip/overwrite flags into one output action."""

        if self.existing_file_action not in EXISTING_FILE_ACTIONS:
            self.existing_file_action = "replace" if self.overwrite else "skip"
        if self.overwrite:
            self.existing_file_action = "replace"


@dataclass(slots=True)
class ConversionResult:
    """Result of converting or skipping one file."""

    source_path: Path
    output_path: Path | None
    status: ConversionStatus
    message: str = ""
    elapsed_seconds: float = 0.0


@dataclass(slots=True)
class EmailMetadata:
    """Small subset of MSG metadata used by naming and front matter."""

    subject: str | None = None
    received_at: datetime | None = None
    sent_at: datetime | None = None


@dataclass(slots=True)
class AttachmentInfo:
    """Visible MSG attachment extracted or listed for Markdown output."""

    name: str
    size_bytes: int | None = None
    saved_path: Path | None = None
    embedded_message: bool = False
    skipped_reason: str | None = None


@dataclass(slots=True)
class OutputPaths:
    """Resolved output locations for one source message."""

    main_markdown: Path
    bundle_folder: Path | None = None
    legacy_markdown: Path | None = None


@dataclass(slots=True)
class ConversionStats:
    """Running totals for a conversion batch."""

    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    cancelled: bool = False

    @property
    def elapsed_seconds(self) -> float:
        """Return elapsed seconds for completed or in-progress work."""

        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    def add(self, result: ConversionResult) -> None:
        """Add one conversion result to the totals."""

        self.processed += 1
        if result.status == ConversionStatus.SUCCEEDED:
            self.succeeded += 1
        elif result.status == ConversionStatus.FAILED:
            self.failed += 1
        elif result.status == ConversionStatus.SKIPPED:
            self.skipped += 1

    def finish(self, cancelled: bool = False) -> None:
        """Mark the batch complete."""

        self.cancelled = cancelled
        self.ended_at = datetime.now(timezone.utc)

    def summary_lines(self) -> list[str]:
        """Return the standard batch summary."""

        return [
            f"Processed : {self.processed}",
            f"Succeeded : {self.succeeded}",
            f"Failed    : {self.failed}",
            f"Skipped   : {self.skipped}",
            f"Elapsed   : {format_duration(self.elapsed_seconds)}",
        ]


@dataclass(slots=True)
class ConversionCallbacks:
    """Optional callbacks used by the GUI and CLI."""

    on_batch_start: Callable[[int], None] | None = None
    on_file_start: Callable[[Path], None] | None = None
    on_file_complete: Callable[[ConversionResult], None] | None = None
    on_batch_complete: Callable[[ConversionStats], None] | None = None
    should_cancel: Callable[[], bool] | None = None


class MarkItDownEmailBackend:
    """Adapter isolating Microsoft MarkItDown behind one conversion method."""

    def __init__(self) -> None:
        self._markitdown: object | None = None

    def convert(self, source_path: Path) -> tuple[str, str | None]:
        """Convert a .msg file and return Markdown plus optional title."""

        if self._markitdown is None:
            from markitdown import MarkItDown

            self._markitdown = MarkItDown()

        converter = self._markitdown
        result = converter.convert(source_path)  # type: ignore[attr-defined]
        markdown = getattr(result, "markdown", None)
        if markdown is None:
            markdown = getattr(result, "text_content", None)
        if markdown is None:
            raise RuntimeError("MarkItDown returned no Markdown content.")

        title = getattr(result, "title", None)
        return str(markdown), str(title) if title else None


class MsgToMarkdownConverter:
    """Convert Outlook .msg files to UTF-8 Markdown files."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        backend: MarkItDownEmailBackend | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("msg_to_md")
        self._backend = backend or MarkItDownEmailBackend()
        self._conversion_lock = threading.Lock()

    def convert_path(
        self,
        input_path: Path,
        output_folder: Path,
        options: ConversionOptions,
        callbacks: ConversionCallbacks | None = None,
    ) -> ConversionStats:
        """Convert one .msg file or every .msg file in a folder."""

        input_path = input_path.expanduser().resolve()
        output_folder = output_folder.expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Input path does not exist: {input_path}")

        output_folder.mkdir(parents=True, exist_ok=True)
        files = list(find_msg_files(input_path, options.recursive))
        input_root = input_path.parent if input_path.is_file() else input_path
        return self._convert_files(
            files=files,
            output_folder=output_folder,
            options=options,
            callbacks=callbacks,
            input_root=input_root,
            input_label=input_path,
        )

    def convert_files(
        self,
        source_paths: list[Path],
        output_folder: Path,
        options: ConversionOptions,
        callbacks: ConversionCallbacks | None = None,
    ) -> ConversionStats:
        """Convert an explicit list of selected .msg files."""

        files: list[Path] = []
        for source_path in source_paths:
            resolved = source_path.expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Input file does not exist: {resolved}")
            if resolved.is_file() and not is_temporary_or_unsupported(resolved):
                files.append(resolved)

        output_folder = output_folder.expanduser().resolve()
        output_folder.mkdir(parents=True, exist_ok=True)
        return self._convert_files(
            files=files,
            output_folder=output_folder,
            options=options,
            callbacks=callbacks,
            input_root=None,
            input_label=f"{len(files)} selected file(s)",
        )

    def _convert_files(
        self,
        *,
        files: list[Path],
        output_folder: Path,
        options: ConversionOptions,
        callbacks: ConversionCallbacks | None,
        input_root: Path | None,
        input_label: Path | str,
    ) -> ConversionStats:
        """Run shared batch conversion logic for folders or selected files."""

        stats = ConversionStats(total=len(files))
        self._logger.info("Starting conversion run")
        self._logger.info("Input: %s", input_label)
        self._logger.info("Output: %s", output_folder)
        self._logger.info("Files queued: %s", len(files))

        if callbacks and callbacks.on_batch_start:
            callbacks.on_batch_start(len(files))

        cancelled = False
        for source_path in files:
            if callbacks and callbacks.should_cancel and callbacks.should_cancel():
                cancelled = True
                self._logger.info("Conversion run cancelled by user")
                break

            if callbacks and callbacks.on_file_start:
                callbacks.on_file_start(source_path)

            result = self.convert_file(
                source_path=source_path,
                input_root=input_root or source_path.parent,
                output_folder=output_folder,
                options=options,
            )
            stats.add(result)

            if callbacks and callbacks.on_file_complete:
                callbacks.on_file_complete(result)

        stats.finish(cancelled=cancelled)
        for line in stats.summary_lines():
            self._logger.info(line)
        if callbacks and callbacks.on_batch_complete:
            callbacks.on_batch_complete(stats)
        return stats

    def convert_file(
        self,
        source_path: Path,
        input_root: Path,
        output_folder: Path,
        options: ConversionOptions,
    ) -> ConversionResult:
        """Convert one .msg file while preserving relative folder structure."""

        source_path = source_path.expanduser().resolve()
        input_root = input_root.expanduser().resolve()
        output_folder = output_folder.expanduser().resolve()
        started = time.monotonic()
        metadata = extract_msg_metadata(source_path)
        output_stem = output_stem_for(source_path, metadata)
        has_attachments = msg_has_visible_attachments(source_path)
        output_paths = output_paths_for(
            source_path,
            input_root,
            output_folder,
            output_stem=output_stem,
            has_attachments=has_attachments,
        )
        output_path = output_paths.main_markdown

        if is_temporary_or_unsupported(source_path):
            message = "Skipped temporary or unsupported file"
            self._logger.info("Skipped: %s | %s", source_path, message)
            return ConversionResult(
                source_path=source_path,
                output_path=output_path,
                status=ConversionStatus.SKIPPED,
                message=message,
                elapsed_seconds=time.monotonic() - started,
            )

        with self._conversion_lock:
            if output_path.exists() and options.existing_file_action == "skip":
                message = "Output already exists"
                self._logger.info("Skipped: %s | %s", source_path, message)
                return ConversionResult(
                    source_path=source_path,
                    output_path=output_path,
                    status=ConversionStatus.SKIPPED,
                    message=message,
                    elapsed_seconds=time.monotonic() - started,
                )
            try:
                self._logger.info("Processing: %s", source_path)
                if output_paths.bundle_folder is not None:
                    reset_bundle_folder(output_paths.bundle_folder)
                    remove_legacy_output(output_paths.legacy_markdown)
                    output_paths.bundle_folder.mkdir(parents=True, exist_ok=True)
                else:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                markdown, title = self._read_body_markdown(
                    source_path,
                    metadata,
                    options,
                )
                attachments = (
                    extract_msg_attachments(
                        source_path,
                        output_path,
                        refresh=output_paths.bundle_folder is None,
                        target_folder=output_paths.bundle_folder,
                    )
                    if options.extract_attachments or options.list_attachments
                    else []
                )
                content = build_markdown_document(
                    source_path=source_path,
                    input_root=input_root,
                    markdown=markdown,
                    title=title,
                    metadata=metadata,
                    attachments=attachments if options.list_attachments else [],
                    output_path=output_path,
                )
                temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
                temp_path.write_text(content, encoding="utf-8", newline="\n")
                temp_path.replace(output_path)
                if output_paths.bundle_folder is not None:
                    index_content = build_bundle_index(
                        source_path=source_path,
                        main_markdown=output_path,
                        attachments=attachments,
                        metadata=metadata,
                    )
                    index_path = output_paths.bundle_folder / "README_index.md"
                    index_path.write_text(index_content, encoding="utf-8", newline="\n")
                elapsed = time.monotonic() - started
                self._logger.info("Succeeded: %s -> %s", source_path, output_path)
                return ConversionResult(
                    source_path=source_path,
                    output_path=output_path,
                    status=ConversionStatus.SUCCEEDED,
                    message="Converted",
                    elapsed_seconds=elapsed,
                )

            except Exception as exc:
                elapsed = time.monotonic() - started
                self._logger.exception("Failed: %s", source_path)
                try:
                    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
                    if temp_path.exists():
                        temp_path.unlink()
                except OSError:
                    self._logger.debug("Could not remove temporary output file")
                return ConversionResult(
                    source_path=source_path,
                    output_path=output_path,
                    status=ConversionStatus.FAILED,
                    message=str(exc),
                    elapsed_seconds=elapsed,
                )

    def _read_body_markdown(
        self,
        source_path: Path,
        metadata: EmailMetadata,
        options: ConversionOptions,
    ) -> tuple[str, str | None]:
        if options.prefer_plain_text_body:
            plain_body = read_msg_plain_body(source_path)
            if plain_body.strip():
                return clean_email_markdown(plain_body), metadata.subject

        markdown, title = self._backend.convert(source_path)
        return clean_email_markdown(markdown), title


def find_msg_files(input_path: Path, recursive: bool) -> list[Path]:
    """Return eligible .msg files from a file or folder."""

    if input_path.is_file():
        return [] if is_temporary_or_unsupported(input_path) else [input_path]

    pattern = "**/*.msg" if recursive else "*.msg"
    return [
        path
        for path in sorted(input_path.glob(pattern), key=lambda item: str(item).lower())
        if path.is_file() and not is_temporary_or_unsupported(path)
    ]


def destination_for(
    source_path: Path,
    input_root: Path,
    output_folder: Path,
    output_stem: str | None = None,
) -> Path:
    """Build the Markdown output path for a source file."""

    try:
        relative_path = source_path.relative_to(input_root)
    except ValueError:
        relative_path = Path(source_path.name)
    if output_stem:
        relative_path = relative_path.with_name(f"{output_stem}.md")
    else:
        relative_path = relative_path.with_suffix(".md")
    return output_folder / relative_path


def output_paths_for(
    source_path: Path,
    input_root: Path,
    output_folder: Path,
    output_stem: str | None = None,
    has_attachments: bool | None = None,
) -> OutputPaths:
    """Resolve either a plain Markdown path or an AI bundle folder path."""

    legacy_markdown = destination_for(
        source_path,
        input_root,
        output_folder,
        output_stem=output_stem,
    )
    if has_attachments is None:
        has_attachments = msg_has_visible_attachments(source_path)
    if not has_attachments:
        return OutputPaths(main_markdown=legacy_markdown)

    try:
        relative_path = source_path.relative_to(input_root)
    except ValueError:
        relative_path = Path(source_path.name)
    bundle_name = safe_filename_part(
        output_stem or source_path.stem,
        fallback=source_path.stem,
        max_length=80,
    )
    bundle_relative = relative_path.parent / bundle_name
    bundle_folder = output_folder / bundle_relative
    return OutputPaths(
        main_markdown=bundle_folder / "main_email.md",
        bundle_folder=bundle_folder,
        legacy_markdown=legacy_markdown,
    )


def msg_has_visible_attachments(source_path: Path) -> bool:
    """Return true when a MSG has non-hidden attachments or embedded emails."""

    try:
        import olefile  # type: ignore[import-not-found]
    except ImportError:
        return False

    try:
        with olefile.OleFileIO(str(source_path)) as ole:
            return ole_has_visible_attachments(ole)
    except Exception:
        return False


def ole_has_visible_attachments(ole: object, storage_root: str = "") -> bool:
    """Return true for visible attachment storages under a MSG storage."""

    for root in find_msg_attachment_roots(ole, storage_root):
        hidden = read_msg_property_int(ole, root, 0x7FFE, 0x000B)
        if not hidden:
            return True
    return False


def extract_msg_metadata(source_path: Path) -> EmailMetadata:
    """Read key MAPI properties directly from a .msg compound file."""

    metadata = EmailMetadata()
    if source_path.suffix.lower() != ".msg":
        return metadata

    try:
        import olefile  # type: ignore[import-not-found]
    except ImportError:
        return metadata

    try:
        with olefile.OleFileIO(str(source_path)) as ole:
            metadata.subject = read_msg_subject(ole)
            metadata.received_at = read_msg_property_time(
                ole,
                MSG_RECEIVED_TIME_PROPERTY_ID,
            )
            metadata.sent_at = read_msg_property_time(ole, MSG_SENT_TIME_PROPERTY_ID)
    except Exception:
        return metadata
    return metadata


def read_msg_subject(ole: object, storage_root: str = "") -> str | None:
    """Return PR_SUBJECT from common Unicode or ANSI MSG streams."""

    stream_options = (
        (join_msg_path(storage_root, "__substg1.0_0037001F"), "utf-16-le"),
        (join_msg_path(storage_root, "__substg1.0_0037001E"), "cp1252"),
    )
    for stream_name, encoding in stream_options:
        try:
            if not ole.exists(stream_name):  # type: ignore[attr-defined]
                continue
            raw = ole.openstream(stream_name).read()  # type: ignore[attr-defined]
        except Exception:
            continue
        try:
            subject = raw.decode(encoding, errors="replace").strip("\x00\r\n\t ")
        except LookupError:
            continue
        if subject:
            return subject
    return None


def read_msg_property_time(
    ole: object,
    property_id: int,
    storage_root: str = "",
) -> datetime | None:
    """Return a PT_SYSTIME value from the top-level MSG property table."""

    direct_value = read_direct_msg_time_stream(ole, property_id, storage_root)
    if direct_value is not None:
        return direct_value

    stream_name = join_msg_path(storage_root, "__properties_version1.0")
    try:
        if not ole.exists(stream_name):  # type: ignore[attr-defined]
            return None
        data = ole.openstream(stream_name).read()  # type: ignore[attr-defined]
    except Exception:
        return None

    property_tag = (property_id << 16) | MSG_SYSTIME_PROPERTY_TYPE
    for offset in range(32, max(32, len(data) - 15), 16):
        tag, _flags, raw_value = struct.unpack_from("<IIQ", data, offset)
        if tag == property_tag:
            return filetime_to_datetime(raw_value)
    return None


def read_direct_msg_time_stream(
    ole: object,
    property_id: int,
    storage_root: str = "",
) -> datetime | None:
    """Read rare MSG time streams when a value is not inline in the property table."""

    stream_name = join_msg_path(
        storage_root,
        f"__substg1.0_{property_id:04X}{MSG_SYSTIME_PROPERTY_TYPE:04X}",
    )
    try:
        if not ole.exists(stream_name):  # type: ignore[attr-defined]
            return None
        data = ole.openstream(stream_name).read()  # type: ignore[attr-defined]
    except Exception:
        return None
    if len(data) < 8:
        return None
    return filetime_to_datetime(struct.unpack_from("<Q", data, 0)[0])


def join_msg_path(storage_root: str, stream_name: str) -> str:
    """Join MSG storage paths using OLE path separators."""

    return f"{storage_root}/{stream_name}" if storage_root else stream_name


def filetime_to_datetime(value: int) -> datetime | None:
    """Convert a Windows FILETIME integer to local time."""

    if value <= 0:
        return None
    try:
        seconds, remainder = divmod(value, 10_000_000)
        utc_value = FILETIME_EPOCH + timedelta(
            seconds=seconds,
            microseconds=remainder // 10,
        )
    except OverflowError:
        return None
    if utc_value.year < 1980 or utc_value.year > 9999:
        return None
    return utc_value.astimezone()


def output_stem_for(source_path: Path, metadata: EmailMetadata) -> str:
    """Build the requested received-time-prefixed Markdown filename stem."""

    timestamp = (
        metadata.received_at
        or metadata.sent_at
        or datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).astimezone()
    )
    date_prefix = timestamp.strftime("%H%M%S_%d-%m-%Y")
    subject = safe_filename_part(metadata.subject or source_path.stem)
    return safe_filename_part(f"{date_prefix} - {subject}", fallback=date_prefix)


def safe_filename_part(
    value: str,
    fallback: str = "email",
    max_length: int = 180,
) -> str:
    """Return a Windows-safe file name part."""

    cleaned = INVALID_FILENAME_CHARS.sub(" ", value)
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:max_length].rstrip(" .") or fallback


def read_msg_plain_body(source_path: Path) -> str:
    """Return the root PR_BODY stream from a .msg file when present."""

    try:
        import olefile  # type: ignore[import-not-found]
    except ImportError:
        return ""

    try:
        with olefile.OleFileIO(str(source_path)) as ole:
            return read_msg_plain_body_from_ole(ole)
    except Exception:
        return ""
    return ""


def read_msg_plain_body_from_ole(ole: object, storage_root: str = "") -> str:
    """Return PR_BODY from a root or embedded MSG storage."""

    for stream_name, encoding in MSG_BODY_STREAMS:
        full_stream_name = join_msg_path(storage_root, stream_name)
        try:
            if not ole.exists(full_stream_name):  # type: ignore[attr-defined]
                continue
            raw = ole.openstream(full_stream_name).read()  # type: ignore[attr-defined]
        except Exception:
            continue
        return normalise_email_text(raw.decode(encoding, errors="replace"))
    return ""


def normalise_email_text(value: str) -> str:
    """Normalise plain email text into Markdown-friendly line spacing."""

    value = value.replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() if line.strip() else "" for line in value.split("\n")]
    cleaned: list[str] = []
    blank_count = 0
    for line in lines:
        if line:
            blank_count = 0
            cleaned.append(line)
        else:
            blank_count += 1
            if blank_count <= 2:
                cleaned.append("")
    return "\n".join(cleaned).strip() + "\n"


def extract_msg_attachments(
    source_path: Path,
    output_path: Path,
    *,
    refresh: bool = False,
    target_folder: Path | None = None,
) -> list[AttachmentInfo]:
    """Extract visible MSG attachments and return details for Markdown listing."""

    try:
        import olefile  # type: ignore[import-not-found]
    except ImportError:
        return []

    attachments: list[AttachmentInfo] = []
    try:
        with olefile.OleFileIO(str(source_path)) as ole:
            if refresh:
                try:
                    reset_attachment_folder(output_path)
                except OSError:
                    pass
            attachments = extract_msg_attachments_from_ole(
                ole,
                output_path,
                target_folder=target_folder,
            )
    except Exception:
        return attachments
    return attachments


def attachment_folder_for_output(output_path: Path) -> Path:
    """Return the attachment folder paired with a Markdown output path."""

    return output_path.with_name(
        f"{safe_filename_part(output_path.stem, max_length=64)}_attachments"
    )


def reset_attachment_folder(output_path: Path) -> None:
    """Remove stale extracted attachments for an output file before regenerating."""

    folder = attachment_folder_for_output(output_path)
    try:
        resolved = folder.resolve()
        parent = output_path.parent.resolve()
    except OSError:
        return
    if resolved.parent != parent or not resolved.name.endswith("_attachments"):
        return
    if resolved.exists():
        shutil.rmtree(resolved, onexc=make_path_writable)


def make_path_writable(function: object, path: str, _excinfo: object) -> None:
    """Retry a failed delete after clearing read-only attributes."""

    try:
        os.chmod(path, stat.S_IWRITE)
        function(path)  # type: ignore[operator]
    except Exception:
        pass


def reset_bundle_folder(bundle_folder: Path) -> None:
    """Remove a generated bundle folder before replacing it."""

    try:
        resolved = bundle_folder.resolve()
        parent = bundle_folder.parent.resolve()
    except OSError:
        return
    if resolved.parent != parent or not resolved.name:
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, onexc=make_path_writable)
    elif resolved.exists():
        resolved.unlink()


def remove_legacy_output(legacy_markdown: Path | None) -> None:
    """Remove old single-file output for an email now written as a bundle."""

    if legacy_markdown is None:
        return
    try:
        if legacy_markdown.exists() and legacy_markdown.is_file():
            legacy_markdown.unlink()
        reset_attachment_folder(legacy_markdown)
    except OSError:
        pass


def extract_msg_attachments_from_ole(
    ole: object,
    output_path: Path,
    storage_root: str = "",
    depth: int = 0,
    target_folder: Path | None = None,
) -> list[AttachmentInfo]:
    """Extract/list attachments from a root or embedded MSG storage."""

    attachments: list[AttachmentInfo] = []
    roots = find_msg_attachment_roots(ole, storage_root)
    attachment_dir = target_folder or attachment_folder_for_output(output_path)
    for index, root in enumerate(roots, start=1):
        try:
            info = attachment_info_from_storage(
                ole,
                root,
                attachment_dir,
                index,
                depth,
                target_folder,
            )
        except Exception:
            info = AttachmentInfo(
                name=f"attachment-{index}",
                skipped_reason="could not extract",
            )
        if info is not None:
            attachments.append(info)
    return attachments


def find_msg_attachment_roots(ole: object, storage_root: str = "") -> list[str]:
    """Return direct attachment storage roots under the requested MSG storage."""

    prefix_parts = storage_root.split("/") if storage_root else []
    roots: set[str] = set()
    try:
        listing = ole.listdir()  # type: ignore[attr-defined]
    except Exception:
        return []

    for parts in listing:
        if len(parts) <= len(prefix_parts):
            continue
        if prefix_parts and list(parts[: len(prefix_parts)]) != prefix_parts:
            continue
        candidate = parts[len(prefix_parts)]
        if candidate.startswith("__attach_version1.0"):
            root_parts = [*prefix_parts, candidate]
            roots.add("/".join(root_parts))
    return sorted(roots)


def attachment_info_from_storage(
    ole: object,
    root: str,
    attachment_dir: Path,
    index: int,
    depth: int = 0,
    target_folder: Path | None = None,
) -> AttachmentInfo | None:
    """Return extracted/listable attachment info for one MSG attachment storage."""

    hidden = read_msg_property_int(ole, root, 0x7FFE, 0x000B)
    if hidden:
        return None

    name = (
        read_msg_string_stream(ole, f"{root}/__substg1.0_3707")
        or read_msg_string_stream(ole, f"{root}/__substg1.0_3704")
        or read_msg_string_stream(ole, f"{root}/__substg1.0_3001")
        or f"attachment-{index}"
    )
    name = safe_attachment_filename(name, fallback=f"attachment-{index}")
    method = read_msg_property_int(ole, root, 0x3705, 0x0003)
    embedded_stream = f"{root}/__substg1.0_3701000D"
    data_stream = f"{root}/__substg1.0_37010102"

    if method == 5 or ole.exists(embedded_stream):  # type: ignore[attr-defined]
        return convert_embedded_msg_attachment(
            ole,
            embedded_stream,
            attachment_dir,
            name,
            index,
            depth,
            target_folder,
        )

    if not ole.exists(data_stream):  # type: ignore[attr-defined]
        return AttachmentInfo(name=name, skipped_reason="not extracted")

    data = ole.openstream(data_stream).read()  # type: ignore[attr-defined]
    attachment_dir.mkdir(parents=True, exist_ok=True)
    saved_path = unique_attachment_path(attachment_dir, name)
    try:
        saved_path.write_bytes(data)
    except OSError as exc:
        return AttachmentInfo(name=name, skipped_reason=f"could not extract: {exc}")
    return AttachmentInfo(
        name=saved_path.name,
        size_bytes=len(data),
        saved_path=saved_path,
    )


def convert_embedded_msg_attachment(
    ole: object,
    embedded_root: str,
    attachment_dir: Path,
    attachment_name: str,
    index: int,
    depth: int,
    target_folder: Path | None = None,
) -> AttachmentInfo:
    """Convert an embedded MSG attachment storage to Markdown."""

    if depth >= 5:
        return AttachmentInfo(
            name=attachment_name,
            embedded_message=True,
            skipped_reason="embedded email depth limit",
        )

    metadata = extract_msg_metadata_from_ole(ole, embedded_root)
    body = clean_email_markdown(read_msg_plain_body_from_ole(ole, embedded_root))
    title = metadata.subject or attachment_name
    output_stem = embedded_output_stem(attachment_name, metadata, index)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    output_path = unique_attachment_path(attachment_dir, f"{output_stem}.md")
    nested_attachments = extract_msg_attachments_from_ole(
        ole,
        output_path,
        storage_root=embedded_root,
        depth=depth + 1,
        target_folder=target_folder or attachment_dir,
    )
    content = build_markdown_document(
        source_path=Path(f"{attachment_name}.msg"),
        input_root=Path("."),
        markdown=body,
        title=title,
        metadata=metadata,
        attachments=nested_attachments,
        output_path=output_path,
    )
    try:
        output_path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        return AttachmentInfo(
            name=attachment_name,
            embedded_message=True,
            skipped_reason=f"could not convert embedded email: {exc}",
        )
    return AttachmentInfo(
        name=attachment_name,
        saved_path=output_path,
        embedded_message=True,
    )


def extract_msg_metadata_from_ole(
    ole: object,
    storage_root: str = "",
) -> EmailMetadata:
    """Read key MAPI metadata from a root or embedded MSG storage."""

    return EmailMetadata(
        subject=read_msg_subject(ole, storage_root),
        received_at=read_msg_property_time(
            ole,
            MSG_RECEIVED_TIME_PROPERTY_ID,
            storage_root,
        ),
        sent_at=read_msg_property_time(
            ole,
            MSG_SENT_TIME_PROPERTY_ID,
            storage_root,
        ),
    )


def embedded_output_stem(
    attachment_name: str,
    metadata: EmailMetadata,
    index: int,
) -> str:
    """Build a compact Markdown filename for an embedded email attachment."""

    subject = metadata.subject or attachment_name or f"embedded-email-{index}"
    if metadata.received_at or metadata.sent_at:
        timestamp = metadata.received_at or metadata.sent_at
        assert timestamp is not None
        value = f"{timestamp.strftime('%H%M%S_%d-%m-%Y')} - {subject}"
    else:
        value = subject
    return safe_filename_part(value, fallback=f"embedded-email-{index}", max_length=90)


def read_msg_string_stream(ole: object, base_name: str) -> str | None:
    """Read a MSG string stream with Unicode preferred over ANSI."""

    for suffix, encoding in MSG_STRING_STREAM_TYPES:
        stream_name = f"{base_name}{suffix}"
        try:
            if not ole.exists(stream_name):  # type: ignore[attr-defined]
                continue
            raw = ole.openstream(stream_name).read()  # type: ignore[attr-defined]
        except Exception:
            continue
        value = raw.decode(encoding, errors="replace").strip("\x00\r\n\t ")
        if value:
            return value
    return None


def read_msg_property_int(
    ole: object,
    storage_root: str,
    property_id: int,
    property_type: int,
) -> int | None:
    """Read a fixed-width integer property from an attachment property table."""

    stream_name = f"{storage_root}/__properties_version1.0"
    try:
        if not ole.exists(stream_name):  # type: ignore[attr-defined]
            return None
        data = ole.openstream(stream_name).read()  # type: ignore[attr-defined]
    except Exception:
        return None

    property_tag = (property_id << 16) | property_type
    for offset in range(8, max(8, len(data) - 15), 16):
        tag, _flags, raw_value = struct.unpack_from("<IIQ", data, offset)
        if tag == property_tag:
            return int(raw_value)
    return None


def unique_attachment_path(folder: Path, filename: str) -> Path:
    """Return a non-colliding path for an extracted attachment."""

    candidate = folder / filename
    if not candidate.exists():
        return candidate

    path = Path(filename)
    stem = path.stem or "attachment"
    suffix = path.suffix
    counter = 2
    while True:
        candidate = folder / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def safe_attachment_filename(
    value: str,
    fallback: str = "attachment",
    max_length: int = 80,
) -> str:
    """Return a safe, short attachment filename while preserving extension."""

    cleaned = safe_filename_part(value, fallback=fallback, max_length=240)
    path = Path(cleaned)
    suffix = path.suffix
    stem = path.stem or fallback
    if len(cleaned) <= max_length:
        return cleaned

    stem_limit = max(1, max_length - len(suffix))
    return f"{stem[:stem_limit].rstrip(' .')}{suffix}" or fallback


def is_temporary_or_unsupported(path: Path) -> bool:
    """Return true for files that should not be converted."""

    name = path.name.lower()
    temporary_suffixes = (".tmp", ".temp", ".partial", ".crdownload")
    if path.suffix.lower() != ".msg":
        return True
    if name.startswith("~$") or name.startswith(".~"):
        return True
    return any(name.endswith(suffix) for suffix in temporary_suffixes)


def clean_email_markdown(markdown: str) -> str:
    """Remove common email signature and disclaimer blocks from Markdown."""

    markdown = normalise_outlook_safelinks(markdown)
    lines = markdown.splitlines()
    cleaned: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        cleaned.append(line)
        if is_email_signoff(line):
            end_index = find_signature_end(lines, index + 1)
            segment = lines[index + 1 : end_index]
            has_reply_after = end_index < len(lines)
            cleaned.extend(preserved_signature_identity(segment))
            trim_trailing_blank_lines(cleaned)
            if has_reply_after:
                cleaned.append("")
                index = skip_blank_lines(lines, end_index)
            else:
                index = end_index
            continue
        index += 1

    return "\n".join(cleaned).rstrip() + "\n"


def normalise_outlook_safelinks(markdown: str) -> str:
    """Remove Outlook safelink wrappers while preserving bare targets."""

    def replacement(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("<") and token.endswith(">"):
            return ""
        target = decoded_safelink_target(token)
        return target or ""

    return OUTLOOK_SAFELINK_RE.sub(replacement, markdown)


def decoded_safelink_target(url: str) -> str | None:
    """Return the original URL carried by an Outlook safelink."""

    cleaned = url.strip("<>")
    try:
        query = parse_qs(urlparse(cleaned).query)
    except ValueError:
        return None
    values = query.get("url")
    if not values:
        return None
    return unquote(values[0])


def is_email_signoff(line: str) -> bool:
    """Return true for short sign-off lines that often precede signatures."""

    return bool(EMAIL_SIGNOFF_RE.match(line))


def find_signature_end(lines: list[str], start_index: int) -> int:
    """Find the next reply boundary, or EOF, after a sign-off."""

    for index in range(start_index, len(lines)):
        if is_reply_boundary(lines[index]):
            while index > start_index and not lines[index - 1].strip():
                index -= 1
            return index
    return len(lines)


def is_reply_boundary(line: str) -> bool:
    """Return true for common quoted-message header boundaries."""

    stripped = line.strip()
    if not stripped:
        return False
    return bool(EMAIL_REPLY_HEADER_RE.match(line) or EMAIL_REPLY_INTRO_RE.match(line))


def skip_blank_lines(lines: list[str], start_index: int) -> int:
    """Move past blank spacer lines before a reply boundary."""

    index = start_index
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def preserved_signature_identity(lines: list[str]) -> list[str]:
    """Keep only sender name and a likely role line after a sign-off."""

    preserved: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if preserved:
                return preserved
            continue
        if is_signature_contact_or_footer_line(stripped):
            return preserved
        if not preserved:
            preserved.append(stripped)
            continue
        if len(preserved) == 1 and is_role_line(stripped):
            preserved.append(stripped)
        return preserved
    return preserved


def is_signature_contact_or_footer_line(line: str) -> bool:
    """Return true for signature lines beyond name/role details."""

    return bool(
        SIGNATURE_FOOTER_TOKEN_RE.search(line)
        or EMAIL_ADDRESS_RE.search(line)
        or PHONE_RE.search(line)
        or PHONE_NUMBER_ONLY_RE.search(line)
        or ADDRESS_TOKEN_RE.search(line)
        or URL_RE.search(line)
        or line.startswith("<https://")
    )


def is_role_line(line: str) -> bool:
    """Return true when a signature line looks like a job role/title."""

    return bool(ROLE_LINE_RE.search(line) and not is_signature_contact_or_footer_line(line))


def trim_trailing_blank_lines(lines: list[str]) -> None:
    """Remove blank lines from the end of a list in place."""

    while lines and not lines[-1].strip():
        lines.pop()


def build_markdown_document(
    source_path: Path,
    input_root: Path,
    markdown: str,
    title: str | None = None,
    metadata: EmailMetadata | None = None,
    attachments: list[AttachmentInfo] | None = None,
    output_path: Path | None = None,
) -> str:
    """Add YAML front matter before MarkItDown's Markdown output."""

    front_matter: dict[str, str] = {
        "document_type": "email",
        "source_file": source_path.name,
    }

    try:
        front_matter["source_path"] = source_path.relative_to(input_root).as_posix()
    except ValueError:
        front_matter["source_path"] = source_path.name

    if title:
        front_matter["title"] = title
    elif metadata and metadata.subject:
        front_matter["title"] = metadata.subject

    if metadata and metadata.received_at:
        front_matter["received_at"] = metadata.received_at.isoformat()
    if metadata and metadata.sent_at:
        front_matter["sent_at"] = metadata.sent_at.isoformat()
    if attachments:
        front_matter["attachment_count"] = str(len(attachments))

    yaml_lines = ["---"]
    yaml_lines.extend(
        f"{key}: {_yaml_scalar(value)}" for key, value in front_matter.items()
    )
    yaml_lines.append("---")
    yaml_lines.append("")
    body_parts: list[str] = []
    attachment_section = build_attachment_markdown_section(attachments, output_path)
    if attachment_section:
        body_parts.append(attachment_section)
    if markdown.strip():
        body_parts.append(markdown.strip())
    yaml_lines.append("\n\n".join(body_parts))
    if not yaml_lines[-1].endswith("\n"):
        yaml_lines[-1] = f"{yaml_lines[-1]}\n"
    return "\n".join(yaml_lines)


def build_attachment_markdown_section(
    attachments: list[AttachmentInfo] | None,
    output_path: Path | None,
) -> str:
    """Render extracted/listed attachments as a Markdown section."""

    if not attachments:
        return ""

    lines = ["## Attachments", ""]
    for attachment in attachments:
        details: list[str] = []
        if attachment.size_bytes is not None:
            details.append(format_file_size(attachment.size_bytes))
        if attachment.embedded_message:
            details.append(
                "converted embedded email"
                if attachment.saved_path
                else "embedded email"
            )
        if attachment.skipped_reason and not attachment.embedded_message:
            details.append(attachment.skipped_reason)

        suffix = f" ({', '.join(details)})" if details else ""
        if attachment.saved_path and output_path:
            target = markdown_link_target(attachment.saved_path, output_path.parent)
            lines.append(
                f"- [{escape_markdown_link_text(attachment.name)}]({target}){suffix}"
            )
        else:
            lines.append(f"- {attachment.name}{suffix}")
    return "\n".join(lines)


def build_bundle_index(
    source_path: Path,
    main_markdown: Path,
    attachments: list[AttachmentInfo],
    metadata: EmailMetadata,
) -> str:
    """Build the AI-friendly bundle index file."""

    embedded = [item for item in attachments if item.embedded_message]
    native = [item for item in attachments if not item.embedded_message]
    lines = [
        "# Email Bundle Index",
        "",
        "This folder contains one converted email and its visible attachments.",
        "",
        "## Read Order",
        "",
        f"1. [main_email.md]({markdown_link_target(main_markdown, main_markdown.parent)})",
    ]

    order = 2
    indexed_files = [main_markdown]
    for attachment in embedded:
        if attachment.saved_path:
            lines.append(
                f"{order}. [{escape_markdown_link_text(attachment.name)}]"
                f"({markdown_link_target(attachment.saved_path, main_markdown.parent)})"
                " - converted embedded email"
            )
            indexed_files.append(attachment.saved_path)
            order += 1
    for attachment in native:
        if attachment.saved_path:
            lines.append(
                f"{order}. [{escape_markdown_link_text(attachment.name)}]"
                f"({markdown_link_target(attachment.saved_path, main_markdown.parent)})"
                " - native attachment"
            )
            indexed_files.append(attachment.saved_path)
            order += 1

    extra_files = bundle_extra_files(main_markdown.parent, indexed_files)
    for file_path in extra_files:
        description = describe_extra_bundle_file(file_path)
        lines.append(
            f"{order}. [{escape_markdown_link_text(file_path.name)}]"
            f"({markdown_link_target(file_path, main_markdown.parent)})"
            f" - {description}"
        )
        order += 1

    lines.extend(["", "## Source", ""])
    lines.append(f"- Source MSG: `{source_path.name}`")
    if metadata.subject:
        lines.append(f"- Subject: {metadata.subject}")
    if metadata.received_at:
        lines.append(f"- Received: {metadata.received_at.isoformat()}")
    if metadata.sent_at:
        lines.append(f"- Sent: {metadata.sent_at.isoformat()}")

    lines.extend(["", "## Files", ""])
    lines.append("- `main_email.md` - parent email body and attachment list")
    for attachment in attachments:
        if attachment.saved_path:
            kind = "converted embedded email" if attachment.embedded_message else "native attachment"
            lines.append(f"- `{attachment.saved_path.name}` - {kind}")
        else:
            details = attachment.skipped_reason or "not extracted"
            lines.append(f"- `{attachment.name}` - {details}")
    for file_path in extra_files:
        lines.append(f"- `{file_path.name}` - {describe_extra_bundle_file(file_path)}")

    return "\n".join(lines).rstrip() + "\n"


def bundle_extra_files(bundle_folder: Path, indexed_files: list[Path]) -> list[Path]:
    """Return bundle files that were extracted by nested embedded emails."""

    indexed = {normalised_path_key(path) for path in indexed_files}
    try:
        files = [
            path
            for path in bundle_folder.iterdir()
            if path.is_file() and path.name.lower() != "readme_index.md"
        ]
    except OSError:
        return []
    return [
        path
        for path in sorted(files, key=lambda item: item.name.lower())
        if normalised_path_key(path) not in indexed
    ]


def normalised_path_key(path: Path) -> str:
    """Return a comparable key for paths that may include OneDrive placeholders."""

    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def describe_extra_bundle_file(path: Path) -> str:
    """Describe extracted files not represented by the parent MSG attachment list."""

    if path.suffix.lower() == ".md":
        return "additional Markdown file"
    return "additional extracted attachment"


def markdown_link_target(path: Path, relative_to: Path) -> str:
    """Return a URL-safe relative Markdown link target."""

    try:
        relative = path.relative_to(relative_to)
    except ValueError:
        relative = path
    return quote(relative.as_posix(), safe="/")


def escape_markdown_link_text(value: str) -> str:
    """Escape square brackets inside Markdown link text."""

    return value.replace("[", r"\[").replace("]", r"\]")


def format_file_size(size_bytes: int) -> str:
    """Format file sizes compactly for attachment lists."""

    size = float(size_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}"
        size /= 1024


def _yaml_scalar(value: object) -> str:
    """Format simple values safely for YAML front matter."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def wait_for_stable_file(
    path: Path,
    timeout_seconds: float = 120.0,
    interval_seconds: float = 0.75,
) -> bool:
    """Wait until a copied file appears to be complete and readable."""

    deadline = time.monotonic() + timeout_seconds
    last_size = -1
    stable_observations = 0

    while time.monotonic() < deadline:
        if not path.exists():
            time.sleep(interval_seconds)
            continue

        try:
            size = path.stat().st_size
            with path.open("rb"):
                pass
        except OSError:
            stable_observations = 0
            time.sleep(interval_seconds)
            continue

        if size == last_size:
            stable_observations += 1
        else:
            stable_observations = 0
            last_size = size

        if stable_observations >= 2:
            return True

        time.sleep(interval_seconds)

    return False


def format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""

    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
