import os
import re
import struct
import threading
import tkinter as tk
import zlib
import lzma
from collections import Counter, defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "LZZ5 Text-Optimized Archive Manager"

MAGIC_V5 = b"LZZ5"
VERSION_V5 = 5

# Backward compatibility
MAGIC_V4 = b"LZZ4"
VERSION_V4 = 4
MAGIC_V3 = b"LZZ3"
VERSION_V3 = 3

CODEC_DEFLATE = 1
CODEC_LZMA2 = 2
CODEC_TEXT_LZMA2 = 3

REQUEST_DEFLATE = 1
REQUEST_LZMA2 = 2
REQUEST_AUTO_TEXT_LZMA2 = 3

PROFILE_FAST = 1
PROFILE_BALANCED = 2
PROFILE_MAXIMUM = 3

ENTRY_FILE = 0
ENTRY_DIR = 1

IO_BUFFER_SIZE = 64 * 1024
TEXT_OPTIMIZE_MAX_SIZE = 32 * 1024 * 1024
TOKEN_MARKER = 0xFF
MAX_TOKENS = 250


# ============================================================
# Helpers
# ============================================================

def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{size} B"


def normalize_archive_path(path_text):
    text = str(path_text).replace("\\", "/")

    while text.startswith("/"):
        text = text[1:]

    parts = []

    for part in text.split("/"):
        if part in ("", "."):
            continue

        if part == "..":
            raise ValueError("Parent path '..' is not allowed.")

        parts.append(part)

    if not parts:
        raise ValueError("Invalid archive path.")

    return "/".join(parts)


def safe_output_path(root, archive_path):
    root = Path(root).resolve()
    normalized = normalize_archive_path(archive_path)

    candidate = (
        root / Path(*normalized.split("/"))
    ).resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Unsafe archive path:\n{archive_path}"
        )

    return candidate


def codec_name(codec):
    if codec == CODEC_DEFLATE:
        return "DEFLATE"
    if codec == CODEC_LZMA2:
        return "LZMA2"
    if codec == CODEC_TEXT_LZMA2:
        return "Text Optimize + LZMA2"
    return f"Unknown ({codec})"


def profile_name(profile):
    if profile == PROFILE_FAST:
        return "Fast"
    if profile == PROFILE_BALANCED:
        return "Balanced"
    if profile == PROFILE_MAXIMUM:
        return "Maximum"
    return f"Unknown ({profile})"


def next_power_of_two(value):
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def dictionary_size_for_file(profile, file_size):
    if profile == PROFILE_FAST:
        cap = 1 * 1024 * 1024
    elif profile == PROFILE_BALANCED:
        cap = 8 * 1024 * 1024
    else:
        cap = 64 * 1024 * 1024

    needed = max(
        64 * 1024,
        next_power_of_two(max(1, file_size))
    )

    return min(cap, needed)


def deflate_level(profile):
    if profile == PROFILE_FAST:
        return 1
    if profile == PROFILE_BALANCED:
        return 6
    return 9


# ============================================================
# Folder/file collection
# ============================================================

def collect_entries(selected_items):
    entries = []
    seen = set()

    def add_dir(archive_path):
        archive_path = normalize_archive_path(archive_path)
        key = ("D", archive_path.lower())

        if key in seen:
            return

        seen.add(key)

        entries.append({
            "type": ENTRY_DIR,
            "path": archive_path,
        })

    def add_file(source_path, archive_path):
        source_path = Path(source_path)
        archive_path = normalize_archive_path(archive_path)

        key = ("F", archive_path.lower())

        if key in seen:
            raise ValueError(
                f"Duplicate archive path:\n{archive_path}"
            )

        seen.add(key)

        entries.append({
            "type": ENTRY_FILE,
            "path": archive_path,
            "source": source_path,
        })

    for item in selected_items:
        path = Path(item)

        if not path.exists():
            raise FileNotFoundError(
                f"Input path not found:\n{path}"
            )

        if path.is_file():
            add_file(path, path.name)
            continue

        if not path.is_dir():
            continue

        root_name = path.name
        add_dir(root_name)

        for current_root, dir_names, file_names in os.walk(path):
            current_root = Path(current_root)
            relative_root = current_root.relative_to(path)

            if relative_root.parts:
                add_dir(
                    "/".join(
                        (root_name,) + relative_root.parts
                    )
                )

            for directory in dir_names:
                relative_dir = (
                    current_root / directory
                ).relative_to(path)

                add_dir(
                    "/".join(
                        (root_name,) + relative_dir.parts
                    )
                )

            for file_name in file_names:
                source_file = current_root / file_name
                relative_file = source_file.relative_to(path)

                add_file(
                    source_file,
                    "/".join(
                        (root_name,) + relative_file.parts
                    )
                )

    entries.sort(
        key=lambda entry: (
            0 if entry["type"] == ENTRY_DIR else 1,
            entry["path"].count("/"),
            entry["path"].lower(),
        )
    )

    return entries


# ============================================================
# LZMA2 tuning
# ============================================================

def lzma_filter_candidates(profile, dict_size):
    if profile == PROFILE_FAST:
        return [
            {
                "id": lzma.FILTER_LZMA2,
                "dict_size": dict_size,
                "lc": 3,
                "lp": 0,
                "pb": 2,
                "mode": lzma.MODE_FAST,
                "nice_len": 32,
                "mf": lzma.MF_HC4,
                "depth": 4,
            }
        ]

    if profile == PROFILE_BALANCED:
        return [
            {
                "id": lzma.FILTER_LZMA2,
                "dict_size": dict_size,
                "lc": 3,
                "lp": 0,
                "pb": 2,
                "mode": lzma.MODE_NORMAL,
                "nice_len": 64,
                "mf": lzma.MF_BT4,
                "depth": 0,
            },
            {
                "id": lzma.FILTER_LZMA2,
                "dict_size": dict_size,
                "lc": 3,
                "lp": 0,
                "pb": 0,
                "mode": lzma.MODE_NORMAL,
                "nice_len": 96,
                "mf": lzma.MF_BT4,
                "depth": 0,
            },
        ]

    # Maximum: try several text-friendly LZMA2 models and keep the smallest.
    return [
        {
            "id": lzma.FILTER_LZMA2,
            "dict_size": dict_size,
            "lc": 3,
            "lp": 0,
            "pb": 2,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 273,
            "mf": lzma.MF_BT4,
            "depth": 0,
        },
        {
            "id": lzma.FILTER_LZMA2,
            "dict_size": dict_size,
            "lc": 3,
            "lp": 0,
            "pb": 0,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 273,
            "mf": lzma.MF_BT4,
            "depth": 0,
        },
        {
            "id": lzma.FILTER_LZMA2,
            "dict_size": dict_size,
            "lc": 4,
            "lp": 0,
            "pb": 0,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 273,
            "mf": lzma.MF_BT4,
            "depth": 0,
        },
        {
            "id": lzma.FILTER_LZMA2,
            "dict_size": dict_size,
            "lc": 4,
            "lp": 0,
            "pb": 2,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 273,
            "mf": lzma.MF_BT4,
            "depth": 0,
        },
    ]


def compress_lzma2_bytes_best(data, profile, dict_size):
    best = None

    for filter_spec in lzma_filter_candidates(
        profile,
        dict_size
    ):
        compressor = lzma.LZMACompressor(
            format=lzma.FORMAT_RAW,
            filters=[filter_spec],
        )

        compressed = (
            compressor.compress(data)
            + compressor.flush()
        )

        if best is None or len(compressed) < len(best):
            best = compressed

    return best


# ============================================================
# Automatic text preprocessor
# ============================================================

def looks_like_text(data):
    if not data:
        return False

    sample = data[:min(len(data), 256 * 1024)]

    if b"\x00" in sample:
        return False

    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False

    return True


def build_text_tokens(data):
    """
    Build a small reversible token dictionary from repeated XML tags,
    JSON-style keys and identifiers.

    Token encoding:
        FF 00 = literal FF
        FF 01..FA = dictionary token 1..250
    """

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    counter = Counter()

    # XML/HTML-like tags, exact spellings.
    for match in re.finditer(r"<[^<>]{3,160}>", text):
        token = match.group(0)
        counter[token] += 1

    # JSON keys including quotes and colon.
    for match in re.finditer(
        r'"(?:[^"\\]|\\.){2,96}"\s*:',
        text
    ):
        counter[match.group(0)] += 1

    # Long identifiers/technical words.
    for match in re.finditer(
        r"[A-Za-z_][A-Za-z0-9_.:\-]{4,80}",
        text
    ):
        counter[match.group(0)] += 1

    scored = []

    for token_text, count in counter.items():
        if count < 3:
            continue

        token = token_text.encode("utf-8")

        if len(token) < 5 or len(token) > 255:
            continue

        if bytes([TOKEN_MARKER]) in token:
            continue

        # Approximate savings before LZMA2:
        # token becomes 2 bytes; dictionary has 2-byte length + token data.
        score = (
            count * (len(token) - 2)
            - (len(token) + 2)
        )

        if score > 8:
            scored.append(
                (score, len(token), count, token)
            )

    scored.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2]
        ),
        reverse=True
    )

    # Avoid obvious redundancy: if a shorter token is fully contained in
    # a stronger already-selected token and has much lower score, skip it.
    selected = []

    for score, length, count, token in scored:
        if len(selected) >= MAX_TOKENS:
            break

        redundant = False

        for chosen_score, chosen in selected:
            if (
                token in chosen
                and chosen_score >= score * 2
            ):
                redundant = True
                break

        if not redundant:
            selected.append(
                (score, token)
            )

    return [
        token
        for _, token in selected
    ]


def transform_with_tokens(data, tokens):
    if not tokens:
        return data

    by_first_byte = defaultdict(list)

    for token_id, token in enumerate(
        tokens,
        start=1
    ):
        by_first_byte[token[0]].append(
            (token, token_id)
        )

    for first_byte in by_first_byte:
        by_first_byte[first_byte].sort(
            key=lambda pair: len(pair[0]),
            reverse=True
        )

    output = bytearray()
    i = 0
    data_len = len(data)

    while i < data_len:
        value = data[i]

        if value == TOKEN_MARKER:
            output.extend(
                (TOKEN_MARKER, 0)
            )
            i += 1
            continue

        matched = False

        for token, token_id in by_first_byte.get(
            value,
            ()
        ):
            if data.startswith(token, i):
                output.extend(
                    (TOKEN_MARKER, token_id)
                )
                i += len(token)
                matched = True
                break

        if not matched:
            output.append(value)
            i += 1

    return bytes(output)


def reverse_tokens(data, tokens):
    output = bytearray()
    i = 0

    while i < len(data):
        value = data[i]

        if value != TOKEN_MARKER:
            output.append(value)
            i += 1
            continue

        if i + 1 >= len(data):
            raise ValueError(
                "Invalid text-preprocessor escape sequence."
            )

        token_id = data[i + 1]
        i += 2

        if token_id == 0:
            output.append(TOKEN_MARKER)
            continue

        if token_id < 1 or token_id > len(tokens):
            raise ValueError(
                "Invalid text-preprocessor token ID."
            )

        output.extend(
            tokens[token_id - 1]
        )

    return bytes(output)


def token_dictionary_overhead(tokens):
    # token_count uint16 + each token: uint16 length + token bytes
    return 2 + sum(
        2 + len(token)
        for token in tokens
    )


# ============================================================
# File compression
# ============================================================

def compress_deflate_stream(
    source_path,
    destination,
    profile,
    progress_callback=None,
):
    source_path = Path(source_path)
    original_size = source_path.stat().st_size
    crc = 0
    processed = 0

    compressor = zlib.compressobj(
        level=deflate_level(profile),
        method=zlib.DEFLATED,
        wbits=-15,
        memLevel=9,
        strategy=zlib.Z_DEFAULT_STRATEGY,
    )

    start = destination.tell()

    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)
            crc = zlib.crc32(chunk, crc)

            compressed = compressor.compress(chunk)

            if compressed:
                destination.write(compressed)

            if progress_callback and original_size > 0:
                progress_callback(processed)

        destination.write(
            compressor.flush(zlib.Z_FINISH)
        )

    return {
        "codec": CODEC_DEFLATE,
        "profile": profile,
        "method_param": 0,
        "original_size": original_size,
        "compressed_size": destination.tell() - start,
        "crc32": crc & 0xFFFFFFFF,
        "tokens": [],
        "optimized": False,
    }


def compress_lzma2_stream(
    source_path,
    destination,
    profile,
    progress_callback=None,
):
    """
    Plain streaming LZMA2.
    Uses the first filter candidate for the selected profile.
    Auto-text mode is where multiple LZMA parameter variants are tested.
    """

    source_path = Path(source_path)
    original_size = source_path.stat().st_size
    dict_size = dictionary_size_for_file(
        profile,
        original_size
    )

    filter_spec = lzma_filter_candidates(
        profile,
        dict_size
    )[0]

    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_RAW,
        filters=[filter_spec],
    )

    crc = 0
    processed = 0
    start = destination.tell()

    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)
            crc = zlib.crc32(chunk, crc)

            compressed = compressor.compress(chunk)

            if compressed:
                destination.write(compressed)

            if progress_callback and original_size > 0:
                progress_callback(processed)

        destination.write(
            compressor.flush()
        )

    return {
        "codec": CODEC_LZMA2,
        "profile": profile,
        "method_param": dict_size,
        "original_size": original_size,
        "compressed_size": destination.tell() - start,
        "crc32": crc & 0xFFFFFFFF,
        "tokens": [],
        "optimized": False,
    }


def compress_auto_text_lzma2(
    source_path,
    destination,
    profile,
    progress_callback=None,
):
    source_path = Path(source_path)
    original_size = source_path.stat().st_size

    # Huge files or clearly binary data fall back to streaming LZMA2.
    if original_size > TEXT_OPTIMIZE_MAX_SIZE:
        return compress_lzma2_stream(
            source_path,
            destination,
            profile,
            progress_callback
        )

    data = source_path.read_bytes()

    if progress_callback and original_size > 0:
        progress_callback(
            min(original_size, len(data))
        )

    crc = zlib.crc32(data) & 0xFFFFFFFF

    dict_size = dictionary_size_for_file(
        profile,
        original_size
    )

    # Plain LZMA2 candidate: also auto-tune LZMA model settings.
    raw_compressed = compress_lzma2_bytes_best(
        data,
        profile,
        dict_size
    )

    chosen_codec = CODEC_LZMA2
    chosen_payload = raw_compressed
    chosen_tokens = []

    if looks_like_text(data):
        tokens = build_text_tokens(data)

        if tokens:
            transformed = transform_with_tokens(
                data,
                tokens
            )

            transformed_compressed = (
                compress_lzma2_bytes_best(
                    transformed,
                    profile,
                    dict_size
                )
            )

            transformed_total = (
                len(transformed_compressed)
                + token_dictionary_overhead(tokens)
            )

            raw_total = len(raw_compressed) + 2

            # Only use preprocessing when the complete stored result,
            # including its dictionary, is actually smaller.
            if transformed_total < raw_total:
                chosen_codec = CODEC_TEXT_LZMA2
                chosen_payload = transformed_compressed
                chosen_tokens = tokens

    start = destination.tell()
    destination.write(chosen_payload)

    return {
        "codec": chosen_codec,
        "profile": profile,
        "method_param": dict_size,
        "original_size": original_size,
        "compressed_size": destination.tell() - start,
        "crc32": crc,
        "tokens": chosen_tokens,
        "optimized": (
            chosen_codec == CODEC_TEXT_LZMA2
        ),
    }


# ============================================================
# Decompression
# ============================================================

def decompress_payload(
    source,
    codec,
    profile,
    method_param,
    compressed_size,
    original_size,
    tokens,
    output_path,
    progress_callback=None,
):
    compressed = source.read(
        compressed_size
    )

    if len(compressed) != compressed_size:
        raise ValueError(
            "Compressed payload ended unexpectedly."
        )

    if codec == CODEC_DEFLATE:
        try:
            restored = zlib.decompress(
                compressed,
                wbits=-15
            )
        except zlib.error as error:
            raise ValueError(
                f"DEFLATE decompression failed: {error}"
            )

    elif codec in (
        CODEC_LZMA2,
        CODEC_TEXT_LZMA2,
    ):
        try:
            restored = lzma.decompress(
                compressed,
                format=lzma.FORMAT_RAW,
                filters=[
                    {
                        "id": lzma.FILTER_LZMA2,
                        "dict_size": method_param,
                    }
                ],
            )
        except lzma.LZMAError as error:
            raise ValueError(
                f"LZMA2 decompression failed: {error}"
            )

        if codec == CODEC_TEXT_LZMA2:
            restored = reverse_tokens(
                restored,
                tokens
            )

    else:
        raise ValueError(
            f"Unsupported codec: {codec}"
        )

    if len(restored) != original_size:
        raise ValueError(
            "Restored size does not match archive metadata."
        )

    if progress_callback and original_size > 0:
        progress_callback(original_size)

    Path(output_path).write_bytes(
        restored
    )

    return restored


# ============================================================
# LZZ5 archive creation
# ============================================================

def create_archive(
    selected_items,
    archive_path,
    request_method=REQUEST_AUTO_TEXT_LZMA2,
    profile=PROFILE_MAXIMUM,
    progress_callback=None,
):
    entries = collect_entries(
        selected_items
    )

    if not entries:
        raise ValueError(
            "No files or folders selected."
        )

    file_entries = [
        entry
        for entry in entries
        if entry["type"] == ENTRY_FILE
    ]

    total_original_size = sum(
        entry["source"].stat().st_size
        for entry in file_entries
    )

    total_processed = 0
    file_index = 0

    archive_path = Path(archive_path)
    archive_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    optimized_files = 0
    plain_lzma_files = 0
    deflate_files = 0

    with open(archive_path, "wb") as archive:
        archive.write(MAGIC_V5)
        archive.write(
            struct.pack("<H", VERSION_V5)
        )
        archive.write(
            struct.pack("<H", 0)
        )  # flags/reserved
        archive.write(
            struct.pack("<I", len(entries))
        )

        for entry in entries:
            archive.write(
                bytes([entry["type"]])
            )

            path_bytes = entry["path"].encode(
                "utf-8"
            )

            archive.write(
                struct.pack(
                    "<I",
                    len(path_bytes)
                )
            )

            archive.write(
                path_bytes
            )

            if entry["type"] == ENTRY_DIR:
                continue

            file_index += 1
            source_path = entry["source"]

            def per_file_progress(current):
                if progress_callback:
                    progress_callback(
                        total_processed + current,
                        total_original_size,
                        file_index,
                        len(file_entries),
                        entry["path"],
                    )

            # Reserve all per-file metadata.
            metadata_position = archive.tell()

            # codec(1), profile(1), param(4),
            # original(8), compressed(8), crc(4), token_count(2)
            archive.write(
                b"\x00" * 28
            )

            # We do not yet know token dictionary. For auto-text, compress
            # into memory first, then write dictionary + payload.
            if request_method == REQUEST_DEFLATE:
                payload_temp_position = archive.tell()

                result = compress_deflate_stream(
                    source_path,
                    archive,
                    profile,
                    per_file_progress
                )

                deflate_files += 1

                token_bytes = b""

            elif request_method == REQUEST_LZMA2:
                payload_temp_position = archive.tell()

                result = compress_lzma2_stream(
                    source_path,
                    archive,
                    profile,
                    per_file_progress
                )

                plain_lzma_files += 1
                token_bytes = b""

            else:
                # Auto text mode needs dictionary before payload.
                # Compress to a temporary in-memory buffer by using a simple
                # temporary bytearray via BytesIO.
                import io

                temp_payload = io.BytesIO()

                result = compress_auto_text_lzma2(
                    source_path,
                    temp_payload,
                    profile,
                    per_file_progress
                )

                tokens = result["tokens"]

                token_table = bytearray()

                for token in tokens:
                    token_table.extend(
                        struct.pack(
                            "<H",
                            len(token)
                        )
                    )
                    token_table.extend(token)

                token_bytes = bytes(
                    token_table
                )

                archive.write(
                    token_bytes
                )

                payload_temp_position = archive.tell()

                archive.write(
                    temp_payload.getvalue()
                )

                if result["optimized"]:
                    optimized_files += 1
                else:
                    plain_lzma_files += 1

            end_position = archive.tell()

            # Fill metadata.
            archive.seek(
                metadata_position
            )

            archive.write(
                bytes([result["codec"]])
            )
            archive.write(
                bytes([result["profile"]])
            )
            archive.write(
                struct.pack(
                    "<I",
                    result["method_param"]
                )
            )
            archive.write(
                struct.pack(
                    "<Q",
                    result["original_size"]
                )
            )
            archive.write(
                struct.pack(
                    "<Q",
                    result["compressed_size"]
                )
            )
            archive.write(
                struct.pack(
                    "<I",
                    result["crc32"]
                )
            )
            archive.write(
                struct.pack(
                    "<H",
                    len(result["tokens"])
                )
            )

            archive.seek(
                end_position
            )

            total_processed += (
                result["original_size"]
            )

    archive_size = archive_path.stat().st_size

    if total_original_size > 0:
        saving = (
            1.0 -
            archive_size / total_original_size
        ) * 100.0
    else:
        saving = 0.0

    return {
        "entry_count": len(entries),
        "file_count": len(file_entries),
        "dir_count": len(entries) - len(file_entries),
        "original_size": total_original_size,
        "archive_size": archive_size,
        "saving": saving,
        "optimized_files": optimized_files,
        "plain_lzma_files": plain_lzma_files,
        "deflate_files": deflate_files,
        "request_method": request_method,
        "profile": profile,
    }


# ============================================================
# Archive header readers (LZZ5 + backward compatibility)
# ============================================================

def read_main_header(archive):
    magic = archive.read(4)

    if magic == MAGIC_V5:
        version_raw = archive.read(2)
        flags_raw = archive.read(2)
        count_raw = archive.read(4)

        if (
            len(version_raw) != 2
            or len(flags_raw) != 2
            or len(count_raw) != 4
        ):
            raise ValueError(
                "LZZ5 header is incomplete."
            )

        version = struct.unpack(
            "<H",
            version_raw
        )[0]

        if version != VERSION_V5:
            raise ValueError(
                f"Unsupported LZZ5 version: {version}"
            )

        return {
            "format": 5,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
        }

    if magic == MAGIC_V4:
        version_raw = archive.read(2)
        method_raw = archive.read(1)
        profile_raw = archive.read(1)
        param_raw = archive.read(4)
        count_raw = archive.read(4)

        if not all([
            len(version_raw) == 2,
            len(method_raw) == 1,
            len(profile_raw) == 1,
            len(param_raw) == 4,
            len(count_raw) == 4,
        ]):
            raise ValueError(
                "LZZ4 header is incomplete."
            )

        version = struct.unpack(
            "<H",
            version_raw
        )[0]

        if version != VERSION_V4:
            raise ValueError(
                f"Unsupported LZZ4 version: {version}"
            )

        return {
            "format": 4,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
            "global_codec": method_raw[0],
            "global_profile": profile_raw[0],
            "global_param": struct.unpack(
                "<I",
                param_raw
            )[0],
        }

    if magic == MAGIC_V3:
        version_raw = archive.read(2)
        method_raw = archive.read(1)
        reserved_raw = archive.read(1)
        count_raw = archive.read(4)

        if not all([
            len(version_raw) == 2,
            len(method_raw) == 1,
            len(reserved_raw) == 1,
            len(count_raw) == 4,
        ]):
            raise ValueError(
                "LZZ3 header is incomplete."
            )

        version = struct.unpack(
            "<H",
            version_raw
        )[0]

        if version != VERSION_V3:
            raise ValueError(
                f"Unsupported LZZ3 version: {version}"
            )

        return {
            "format": 3,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
            "global_codec": CODEC_DEFLATE,
            "global_profile": PROFILE_MAXIMUM,
            "global_param": 0,
        }

    raise ValueError(
        "This is not a valid LZZ3/LZZ4/LZZ5 archive."
    )


def read_entry_header(archive, main_header):
    type_raw = archive.read(1)

    if len(type_raw) != 1:
        raise ValueError(
            "Archive entry is damaged."
        )

    entry_type = type_raw[0]

    path_len_raw = archive.read(4)

    if len(path_len_raw) != 4:
        raise ValueError(
            "Archive path length is missing."
        )

    path_len = struct.unpack(
        "<I",
        path_len_raw
    )[0]

    if path_len > 1024 * 1024:
        raise ValueError(
            "Archive path length is unreasonable."
        )

    path_data = archive.read(
        path_len
    )

    if len(path_data) != path_len:
        raise ValueError(
            "Archive path data is incomplete."
        )

    try:
        path_text = path_data.decode(
            "utf-8"
        )
    except UnicodeDecodeError:
        raise ValueError(
            "Archive contains invalid UTF-8 path data."
        )

    if entry_type == ENTRY_DIR:
        return {
            "type": ENTRY_DIR,
            "path": path_text,
        }

    if entry_type != ENTRY_FILE:
        raise ValueError(
            f"Unknown archive entry type: {entry_type}"
        )

    if main_header["format"] == 5:
        metadata = archive.read(28)

        if len(metadata) != 28:
            raise ValueError(
                "LZZ5 file metadata is incomplete."
            )

        codec = metadata[0]
        profile = metadata[1]
        method_param = struct.unpack(
            "<I",
            metadata[2:6]
        )[0]
        original_size = struct.unpack(
            "<Q",
            metadata[6:14]
        )[0]
        compressed_size = struct.unpack(
            "<Q",
            metadata[14:22]
        )[0]
        crc32_value = struct.unpack(
            "<I",
            metadata[22:26]
        )[0]
        token_count = struct.unpack(
            "<H",
            metadata[26:28]
        )[0]

        tokens = []

        if token_count > MAX_TOKENS:
            raise ValueError(
                "Invalid token dictionary size."
            )

        for _ in range(token_count):
            token_len_raw = archive.read(2)

            if len(token_len_raw) != 2:
                raise ValueError(
                    "Token dictionary is incomplete."
                )

            token_len = struct.unpack(
                "<H",
                token_len_raw
            )[0]

            if token_len == 0 or token_len > 4096:
                raise ValueError(
                    "Invalid token length."
                )

            token = archive.read(
                token_len
            )

            if len(token) != token_len:
                raise ValueError(
                    "Token data is incomplete."
                )

            tokens.append(token)

        return {
            "type": ENTRY_FILE,
            "path": path_text,
            "codec": codec,
            "profile": profile,
            "method_param": method_param,
            "original_size": original_size,
            "compressed_size": compressed_size,
            "crc32": crc32_value,
            "tokens": tokens,
        }

    # LZZ3/LZZ4 old file metadata
    metadata = archive.read(20)

    if len(metadata) != 20:
        raise ValueError(
            "Legacy file metadata is incomplete."
        )

    return {
        "type": ENTRY_FILE,
        "path": path_text,
        "codec": main_header["global_codec"],
        "profile": main_header["global_profile"],
        "method_param": main_header["global_param"],
        "original_size": struct.unpack(
            "<Q",
            metadata[0:8]
        )[0],
        "compressed_size": struct.unpack(
            "<Q",
            metadata[8:16]
        )[0],
        "crc32": struct.unpack(
            "<I",
            metadata[16:20]
        )[0],
        "tokens": [],
    }


# ============================================================
# Extraction
# ============================================================

def extract_archive(
    archive_path,
    output_folder,
    progress_callback=None,
):
    archive_path = Path(archive_path)
    output_folder = Path(output_folder)

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    # First pass
    with open(archive_path, "rb") as archive:
        main_header = read_main_header(
            archive
        )

        entries_start = archive.tell()

        total_original_size = 0
        file_count = 0
        dir_count = 0

        for _ in range(
            main_header["entry_count"]
        ):
            entry = read_entry_header(
                archive,
                main_header
            )

            if entry["type"] == ENTRY_DIR:
                dir_count += 1
                continue

            file_count += 1
            total_original_size += (
                entry["original_size"]
            )

            archive.seek(
                entry["compressed_size"],
                os.SEEK_CUR
            )

        # Second pass
        archive.seek(
            entries_start
        )

        restored_total = 0
        current_file_index = 0
        optimized_files = 0

        for _ in range(
            main_header["entry_count"]
        ):
            entry = read_entry_header(
                archive,
                main_header
            )

            target = safe_output_path(
                output_folder,
                entry["path"]
            )

            if entry["type"] == ENTRY_DIR:
                target.mkdir(
                    parents=True,
                    exist_ok=True
                )
                continue

            current_file_index += 1

            target.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            def per_file_progress(current):
                if progress_callback:
                    progress_callback(
                        restored_total + current,
                        total_original_size,
                        current_file_index,
                        file_count,
                        entry["path"],
                    )

            restored = decompress_payload(
                archive,
                entry["codec"],
                entry["profile"],
                entry["method_param"],
                entry["compressed_size"],
                entry["original_size"],
                entry["tokens"],
                target,
                per_file_progress,
            )

            actual_crc = zlib.crc32(
                restored
            ) & 0xFFFFFFFF

            if actual_crc != entry["crc32"]:
                raise ValueError(
                    f"CRC32 verification failed:\n{entry['path']}"
                )

            if entry["codec"] == CODEC_TEXT_LZMA2:
                optimized_files += 1

            restored_total += (
                len(restored)
            )

    return {
        "format": main_header["format"],
        "file_count": file_count,
        "dir_count": dir_count,
        "restored_size": restored_total,
        "optimized_files": optimized_files,
    }


# ============================================================
# GUI
# ============================================================

class LZZApp:

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1020x800")
        self.root.minsize(900, 700)

        self.items = []
        self.busy = False

        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(
            self.root,
            padding=22
        )
        outer.pack(
            fill="both",
            expand=True
        )

        ttk.Label(
            outer,
            text=APP_TITLE,
            font=("Segoe UI", 20, "bold")
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "LZZ5 adds automatic structured-text preprocessing and "
                "LZMA2 parameter tuning for XML, JSON, TXT and ADS-style files."
            )
        ).pack(
            anchor="w",
            pady=(4, 16)
        )

        content_box = ttk.LabelFrame(
            outer,
            text="Archive Contents",
            padding=10
        )
        content_box.pack(
            fill="both",
            expand=True
        )

        list_frame = ttk.Frame(
            content_box
        )
        list_frame.pack(
            fill="both",
            expand=True
        )

        self.listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            font=("Segoe UI", 10)
        )
        self.listbox.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.listbox.yview
        )
        scrollbar.pack(
            side="right",
            fill="y"
        )

        self.listbox.config(
            yscrollcommand=scrollbar.set
        )

        controls = ttk.Frame(
            content_box
        )
        controls.pack(
            fill="x",
            pady=(10, 0)
        )

        self.add_files_btn = ttk.Button(
            controls,
            text="Add Files...",
            command=self.add_files
        )
        self.add_files_btn.pack(
            side="left"
        )

        self.add_folder_btn = ttk.Button(
            controls,
            text="Add Folder...",
            command=self.add_folder
        )
        self.add_folder_btn.pack(
            side="left",
            padx=(8, 0)
        )

        self.remove_btn = ttk.Button(
            controls,
            text="Remove Selected",
            command=self.remove_selected
        )
        self.remove_btn.pack(
            side="left",
            padx=(8, 0)
        )

        self.clear_btn = ttk.Button(
            controls,
            text="Clear",
            command=self.clear_items
        )
        self.clear_btn.pack(
            side="left",
            padx=(8, 0)
        )

        settings = ttk.LabelFrame(
            outer,
            text="Compression Settings",
            padding=10
        )
        settings.pack(
            fill="x",
            pady=(14, 0)
        )

        ttk.Label(
            settings,
            text="Method:"
        ).grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.method_var = tk.StringVar(
            value="LZMA2 + Auto Text Optimize"
        )

        self.method_combo = ttk.Combobox(
            settings,
            textvariable=self.method_var,
            state="readonly",
            width=30,
            values=[
                "DEFLATE",
                "LZMA2",
                "LZMA2 + Auto Text Optimize",
            ]
        )
        self.method_combo.grid(
            row=0,
            column=1,
            padx=(10, 24),
            sticky="w"
        )

        ttk.Label(
            settings,
            text="Profile:"
        ).grid(
            row=0,
            column=2,
            sticky="w"
        )

        self.profile_var = tk.StringVar(
            value="Maximum"
        )

        self.profile_combo = ttk.Combobox(
            settings,
            textvariable=self.profile_var,
            state="readonly",
            width=16,
            values=[
                "Fast",
                "Balanced",
                "Maximum",
            ]
        )
        self.profile_combo.grid(
            row=0,
            column=3,
            padx=(10, 0),
            sticky="w"
        )

        self.settings_info = tk.StringVar(
            value=(
                "Recommended test: Auto Text Optimize + Maximum. "
                "It keeps preprocessing only when the final stored file is smaller."
            )
        )

        ttk.Label(
            settings,
            textvariable=self.settings_info
        ).grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0)
        )

        actions = ttk.Frame(
            outer
        )
        actions.pack(
            pady=16
        )

        self.create_btn = ttk.Button(
            actions,
            text="CREATE .LZZ ARCHIVE",
            width=29,
            command=self.choose_output
        )
        self.create_btn.grid(
            row=0,
            column=0,
            padx=8
        )

        self.extract_btn = ttk.Button(
            actions,
            text="EXTRACT .LZZ ARCHIVE",
            width=29,
            command=self.choose_extract
        )
        self.extract_btn.grid(
            row=0,
            column=1,
            padx=8
        )

        progress_box = ttk.LabelFrame(
            outer,
            text="Progress",
            padding=10
        )
        progress_box.pack(
            fill="x"
        )

        self.progress_var = tk.DoubleVar(
            value=0
        )

        ttk.Progressbar(
            progress_box,
            maximum=100,
            variable=self.progress_var
        ).pack(
            fill="x"
        )

        self.status_var = tk.StringVar(
            value="Ready"
        )

        ttk.Label(
            progress_box,
            textvariable=self.status_var
        ).pack(
            anchor="w",
            pady=(7, 0)
        )

        result_box = ttk.LabelFrame(
            outer,
            text="Result",
            padding=10
        )
        result_box.pack(
            fill="x",
            pady=(14, 0)
        )

        self.result_var = tk.StringVar(
            value=(
                "Selected items: 0\n"
                "Files: -\n"
                "Directories: -\n"
                "Original size: -\n"
                "Archive size: -\n"
                "Saving: -\n"
                "Text-optimized files: -"
            )
        )

        ttk.Label(
            result_box,
            textvariable=self.result_var,
            justify="left"
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Trackable version: LZZ5 | Auto text preprocessing + LZMA2 tuning | "
                "CRC32 | Reads LZZ3/LZZ4/LZZ5"
            ),
            font=("Segoe UI", 9)
        ).pack(
            anchor="w",
            pady=(12, 0)
        )

    def add_files(self):
        if self.busy:
            return

        files = filedialog.askopenfilenames(
            title="Select files",
            filetypes=[
                ("All files", "*.*")
            ]
        )

        for file_path in files:
            resolved = str(
                Path(file_path).resolve()
            )

            if resolved not in self.items:
                self.items.append(
                    resolved
                )

        self.refresh_list()

    def add_folder(self):
        if self.busy:
            return

        folder = filedialog.askdirectory(
            title="Select folder"
        )

        if folder:
            resolved = str(
                Path(folder).resolve()
            )

            if resolved not in self.items:
                self.items.append(
                    resolved
                )

        self.refresh_list()

    def remove_selected(self):
        if self.busy:
            return

        indexes = list(
            self.listbox.curselection()
        )

        for index in reversed(indexes):
            del self.items[index]

        self.refresh_list()

    def clear_items(self):
        if self.busy:
            return

        self.items.clear()
        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(
            0,
            tk.END
        )

        for item in self.items:
            path = Path(item)

            prefix = (
                "[DIR] "
                if path.is_dir()
                else "[FILE] "
            )

            self.listbox.insert(
                tk.END,
                prefix + str(path)
            )

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: -\n"
            f"Directories: -\n"
            f"Original size: -\n"
            f"Archive size: -\n"
            f"Saving: -\n"
            f"Text-optimized files: -"
        )

        self.status_var.set(
            f"{len(self.items)} item(s) selected."
            if self.items
            else "Ready"
        )

    def get_request_method(self):
        text = self.method_var.get()

        if text == "DEFLATE":
            return REQUEST_DEFLATE

        if text == "LZMA2":
            return REQUEST_LZMA2

        return REQUEST_AUTO_TEXT_LZMA2

    def get_profile(self):
        text = self.profile_var.get()

        if text == "Fast":
            return PROFILE_FAST

        if text == "Balanced":
            return PROFILE_BALANCED

        return PROFILE_MAXIMUM

    def choose_output(self):
        if not self.items:
            messagebox.showwarning(
                "Warning",
                "Add at least one file or folder."
            )
            return

        output = filedialog.asksaveasfilename(
            title="Save LZZ5 archive",
            initialfile="archive_lzz5.lzz",
            defaultextension=".lzz",
            filetypes=[
                ("LZZ Archive", "*.lzz"),
                ("All files", "*.*")
            ]
        )

        if output:
            self.start_create(
                output
            )

    def start_create(self, output):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)

        self.status_var.set(
            "Creating LZZ5 archive..."
        )

        threading.Thread(
            target=self.create_worker,
            args=(
                list(self.items),
                output,
                self.get_request_method(),
                self.get_profile(),
            ),
            daemon=True
        ).start()

    def create_worker(
        self,
        items,
        output,
        request_method,
        profile
    ):
        try:
            def progress(
                current,
                total,
                file_index,
                file_count,
                label,
            ):
                percent = (
                    100.0
                    if total == 0
                    else min(
                        100.0,
                        current * 100.0 / total
                    )
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    f"Compressing {file_index}/{file_count}: {label}"
                )

            result = create_archive(
                items,
                output,
                request_method=request_method,
                profile=profile,
                progress_callback=progress,
            )

            self.root.after(
                0,
                self.finish_create,
                result,
                output
            )

        except Exception as error:
            self.root.after(
                0,
                self.finish_error,
                str(error)
            )

    def finish_create(
        self,
        result,
        output
    ):
        self.progress_var.set(
            100
        )

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: {result['file_count']}\n"
            f"Directories: {result['dir_count']}\n"
            f"Original size: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%\n"
            f"Text-optimized files: {result['optimized_files']}\n"
            f"Plain LZMA2 files: {result['plain_lzma_files']}\n"
            f"DEFLATE files: {result['deflate_files']}"
        )

        self.status_var.set(
            "Archive created successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            (
                "LZZ5 archive created successfully.\n\n"
                f"{output}\n\n"
                f"Original: {human_size(result['original_size'])}\n"
                f"Archive: {human_size(result['archive_size'])}\n"
                f"Saving: {result['saving']:.2f}%\n"
                f"Text-optimized files: {result['optimized_files']}"
            )
        )

    def choose_extract(self):
        if self.busy:
            return

        archive_file = filedialog.askopenfilename(
            title="Select LZZ archive",
            filetypes=[
                ("LZZ Archive", "*.lzz"),
                ("All files", "*.*")
            ]
        )

        if not archive_file:
            return

        output_folder = filedialog.askdirectory(
            title="Select extraction folder"
        )

        if not output_folder:
            return

        self.start_extract(
            archive_file,
            output_folder
        )

    def start_extract(
        self,
        archive_file,
        output_folder
    ):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)
        self.status_var.set(
            "Extracting archive..."
        )

        threading.Thread(
            target=self.extract_worker,
            args=(
                archive_file,
                output_folder,
            ),
            daemon=True
        ).start()

    def extract_worker(
        self,
        archive_file,
        output_folder
    ):
        try:
            def progress(
                current,
                total,
                file_index,
                file_count,
                label,
            ):
                percent = (
                    100.0
                    if total == 0
                    else min(
                        100.0,
                        current * 100.0 / total
                    )
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    f"Extracting {file_index}/{file_count}: {label}"
                )

            result = extract_archive(
                archive_file,
                output_folder,
                progress_callback=progress,
            )

            self.root.after(
                0,
                self.finish_extract,
                result,
                output_folder
            )

        except Exception as error:
            self.root.after(
                0,
                self.finish_error,
                str(error)
            )

    def finish_extract(
        self,
        result,
        output_folder
    ):
        self.progress_var.set(100)

        self.result_var.set(
            f"Archive format: LZZ{result['format']}\n"
            f"Files extracted: {result['file_count']}\n"
            f"Directories restored: {result['dir_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"Text-optimized files decoded: {result['optimized_files']}\n"
            f"CRC32: Verified\n"
            f"Output: {output_folder}"
        )

        self.status_var.set(
            "Archive extracted successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            (
                "Archive extracted successfully.\n\n"
                f"Format: LZZ{result['format']}\n"
                f"Files: {result['file_count']}\n"
                f"Restored: {human_size(result['restored_size'])}\n"
                f"{output_folder}"
            )
        )

    def finish_error(
        self,
        error_message
    ):
        self.status_var.set(
            "Error."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showerror(
            "Error",
            error_message
        )

    def set_controls(
        self,
        enabled
    ):
        state = (
            "normal"
            if enabled
            else "disabled"
        )

        for widget in (
            self.add_files_btn,
            self.add_folder_btn,
            self.remove_btn,
            self.clear_btn,
            self.create_btn,
            self.extract_btn,
        ):
            widget.config(
                state=state
            )

        combo_state = (
            "readonly"
            if enabled
            else "disabled"
        )

        self.method_combo.config(
            state=combo_state
        )

        self.profile_combo.config(
            state=combo_state
        )


def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
