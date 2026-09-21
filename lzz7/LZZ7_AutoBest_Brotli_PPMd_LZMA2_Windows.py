import os
import struct
import threading
import tkinter as tk
import zlib
import lzma
import tempfile
import shutil
import subprocess
import sys
import importlib
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "LZZ7 Auto Best Archive Manager"

# ============================================================
# LZZ7
# ============================================================

MAGIC_V7 = b"LZZ7"
VERSION_V7 = 7

# Backward compatibility
MAGIC_V6 = b"LZZ6"
VERSION_V6 = 6
MAGIC_V5 = b"LZZ5"
VERSION_V5 = 5
MAGIC_V4 = b"LZZ4"
VERSION_V4 = 4
MAGIC_V3 = b"LZZ3"
VERSION_V3 = 3

CODEC_STORE = 0
CODEC_DEFLATE = 1
CODEC_LZMA2 = 2
CODEC_TEXT_LZMA2 = 3   # LZZ5 compatibility
CODEC_PPMD7 = 4
CODEC_BROTLI = 5

REQUEST_AUTO_BEST = 1
REQUEST_LZMA2 = 2
REQUEST_PPMD = 3
REQUEST_BROTLI = 4
REQUEST_DEFLATE = 5

PROFILE_FAST = 1
PROFILE_BALANCED = 2
PROFILE_MAXIMUM = 3

ENTRY_FILE = 0
ENTRY_DIR = 1

IO_BUFFER_SIZE = 64 * 1024

# LZZ5 text-token compatibility
TOKEN_MARKER = 0xFF
MAX_TOKENS = 250


# ============================================================
# Optional third-party modules
# ============================================================

try:
    import pyppmd
except ImportError:
    pyppmd = None

try:
    import brotli
except ImportError:
    brotli = None


def reload_pyppmd():
    global pyppmd

    try:
        pyppmd = importlib.import_module("pyppmd")
        return True
    except ImportError:
        pyppmd = None
        return False


def reload_brotli():
    global brotli

    try:
        brotli = importlib.import_module("brotli")
        return True
    except ImportError:
        brotli = None
        return False


# ============================================================
# General helpers
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


def codec_name(codec):
    names = {
        CODEC_STORE: "STORE",
        CODEC_DEFLATE: "DEFLATE",
        CODEC_LZMA2: "LZMA2",
        CODEC_TEXT_LZMA2: "Text Optimize + LZMA2",
        CODEC_PPMD7: "PPMd7",
        CODEC_BROTLI: "Brotli",
    }

    return names.get(codec, f"Unknown ({codec})")


def profile_name(profile):
    names = {
        PROFILE_FAST: "Fast",
        PROFILE_BALANCED: "Balanced",
        PROFILE_MAXIMUM: "Maximum",
    }

    return names.get(profile, f"Unknown ({profile})")


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


def next_power_of_two(value):
    if value <= 1:
        return 1

    return 1 << (value - 1).bit_length()


def default_archive_name(items):
    if not items:
        return "archive.lzz"

    paths = [
        Path(item).resolve()
        for item in items
    ]

    if len(paths) == 1:
        path = paths[0]

        if path.is_dir():
            base = path.name
        else:
            base = path.stem

        return (base if base else "archive") + ".lzz"

    parents = {
        path.parent
        for path in paths
    }

    if len(parents) == 1:
        parent = next(iter(parents))
        base = parent.name
        return (base if base else "archive") + ".lzz"

    try:
        common = Path(
            os.path.commonpath(
                [str(path) for path in paths]
            )
        )

        if common.is_file():
            common = common.parent

        base = common.name

        if base:
            return base + ".lzz"

    except (ValueError, OSError):
        pass

    return "archive.lzz"


def default_archive_directory(items):
    if not items:
        return str(Path.cwd())

    paths = [
        Path(item).resolve()
        for item in items
    ]

    if len(paths) == 1:
        return str(paths[0].parent)

    try:
        common = Path(
            os.path.commonpath(
                [str(path) for path in paths]
            )
        )

        if common.is_file():
            common = common.parent

        return str(common)

    except (ValueError, OSError):
        return str(paths[0].parent)


# ============================================================
# Compression tuning
# ============================================================

def lzma_dictionary_size(profile, file_size):
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


def lzma_filter(profile, dict_size):
    if profile == PROFILE_FAST:
        return {
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

    if profile == PROFILE_BALANCED:
        return {
            "id": lzma.FILTER_LZMA2,
            "dict_size": dict_size,
            "lc": 3,
            "lp": 0,
            "pb": 2,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 96,
            "mf": lzma.MF_BT4,
            "depth": 0,
        }

    return {
        "id": lzma.FILTER_LZMA2,
        "dict_size": dict_size,
        "lc": 3,
        "lp": 0,
        "pb": 2,
        "mode": lzma.MODE_NORMAL,
        "nice_len": 273,
        "mf": lzma.MF_BT4,
        "depth": 0,
    }


def ppmd_parameters(profile):
    if profile == PROFILE_FAST:
        return (
            16 * 1024 * 1024,
            6
        )

    if profile == PROFILE_BALANCED:
        return (
            32 * 1024 * 1024,
            8
        )

    return (
        64 * 1024 * 1024,
        12
    )


def brotli_quality(profile):
    if profile == PROFILE_FAST:
        return 4

    if profile == PROFILE_BALANCED:
        return 8

    return 11


def brotli_lgwin(profile, file_size):
    # Brotli lgwin range is typically 10..24.
    # Do not use a larger window than useful for the file.
    if profile == PROFILE_FAST:
        cap = 20      # 1 MB
    elif profile == PROFILE_BALANCED:
        cap = 22      # 4 MB
    else:
        cap = 24      # 16 MB

    if file_size <= 0:
        return 16

    required = max(
        10,
        (max(1, file_size) - 1).bit_length()
    )

    return min(cap, required)


def deflate_level(profile):
    if profile == PROFILE_FAST:
        return 1

    if profile == PROFILE_BALANCED:
        return 6

    return 9


# ============================================================
# Files / folders
# ============================================================

def collect_entries(selected_items):
    entries = []
    seen = set()

    def add_dir(archive_path):
        archive_path = normalize_archive_path(archive_path)

        key = (
            "D",
            archive_path.lower()
        )

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

        key = (
            "F",
            archive_path.lower()
        )

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
            add_file(
                path,
                path.name
            )
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
# Candidate compression
# ============================================================

def calculate_crc32(path):
    crc = 0

    with open(path, "rb") as source:
        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            crc = zlib.crc32(chunk, crc)

    return crc & 0xFFFFFFFF


def copy_raw_file(
    source_path,
    output_path,
    progress_callback=None
):
    source_path = Path(source_path)
    output_path = Path(output_path)

    size = source_path.stat().st_size
    processed = 0

    with open(source_path, "rb") as source, \
         open(output_path, "wb") as destination:

        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            destination.write(chunk)

            processed += len(chunk)

            if (
                progress_callback
                and size > 0
            ):
                progress_callback(processed)


def compress_deflate_candidate(
    source_path,
    output_path,
    profile,
    progress_callback=None
):
    source_path = Path(source_path)
    size = source_path.stat().st_size

    compressor = zlib.compressobj(
        level=deflate_level(profile),
        method=zlib.DEFLATED,
        wbits=-15,
        memLevel=9,
        strategy=zlib.Z_DEFAULT_STRATEGY,
    )

    processed = 0

    with open(source_path, "rb") as source, \
         open(output_path, "wb") as destination:

        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)

            output = compressor.compress(chunk)

            if output:
                destination.write(output)

            if (
                progress_callback
                and size > 0
            ):
                progress_callback(processed)

        tail = compressor.flush(
            zlib.Z_FINISH
        )

        if tail:
            destination.write(tail)

    return {
        "codec": CODEC_DEFLATE,
        "param_a": 0,
        "param_b": 0,
        "size": Path(output_path).stat().st_size,
    }


def compress_lzma2_candidate(
    source_path,
    output_path,
    profile,
    progress_callback=None
):
    source_path = Path(source_path)
    size = source_path.stat().st_size

    dict_size = lzma_dictionary_size(
        profile,
        size
    )

    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_RAW,
        filters=[
            lzma_filter(
                profile,
                dict_size
            )
        ],
    )

    processed = 0

    with open(source_path, "rb") as source, \
         open(output_path, "wb") as destination:

        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)

            output = compressor.compress(chunk)

            if output:
                destination.write(output)

            if (
                progress_callback
                and size > 0
            ):
                progress_callback(processed)

        tail = compressor.flush()

        if tail:
            destination.write(tail)

    return {
        "codec": CODEC_LZMA2,
        "param_a": dict_size,
        "param_b": 0,
        "size": Path(output_path).stat().st_size,
    }


def compress_ppmd_candidate(
    source_path,
    output_path,
    profile,
    progress_callback=None
):
    if pyppmd is None:
        raise RuntimeError(
            "PPMd requires the 'pyppmd' package.\n\n"
            "Install it with:\n"
            f'"{sys.executable}" -m pip install pyppmd'
        )

    source_path = Path(source_path)
    size = source_path.stat().st_size

    mem_size, max_order = ppmd_parameters(profile)

    encoder = pyppmd.Ppmd7Encoder(
        max_order=max_order,
        mem_size=mem_size,
    )

    processed = 0

    with open(source_path, "rb") as source, \
         open(output_path, "wb") as destination:

        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)

            output = encoder.encode(chunk)

            if output:
                destination.write(output)

            if (
                progress_callback
                and size > 0
            ):
                progress_callback(processed)

        tail = encoder.flush()

        if tail:
            destination.write(tail)

    return {
        "codec": CODEC_PPMD7,
        "param_a": mem_size,
        "param_b": max_order,
        "size": Path(output_path).stat().st_size,
    }


def compress_brotli_candidate(
    source_path,
    output_path,
    profile,
    progress_callback=None
):
    if brotli is None:
        raise RuntimeError(
            "Brotli requires the 'brotli' package.\n\n"
            "Install it with:\n"
            f'"{sys.executable}" -m pip install brotli'
        )

    source_path = Path(source_path)
    size = source_path.stat().st_size

    quality = brotli_quality(profile)
    lgwin = brotli_lgwin(
        profile,
        size
    )

    # Brotli's text mode often helps XML/JSON/text.
    # The Python Brotli API exposes MODE_TEXT.
    mode = getattr(
        brotli,
        "MODE_TEXT",
        getattr(
            brotli,
            "MODE_GENERIC",
            0
        )
    )

    compressor = brotli.Compressor(
        mode=mode,
        quality=quality,
        lgwin=lgwin,
    )

    processed = 0

    with open(source_path, "rb") as source, \
         open(output_path, "wb") as destination:

        while True:
            chunk = source.read(IO_BUFFER_SIZE)

            if not chunk:
                break

            processed += len(chunk)

            output = compressor.process(chunk)

            if output:
                destination.write(output)

            if (
                progress_callback
                and size > 0
            ):
                progress_callback(processed)

        tail = compressor.finish()

        if tail:
            destination.write(tail)

    return {
        "codec": CODEC_BROTLI,
        "param_a": quality,
        "param_b": lgwin,
        "size": Path(output_path).stat().st_size,
    }


def choose_candidate(
    source_path,
    temp_dir,
    request_method,
    profile,
    progress_callback=None
):
    source_path = Path(source_path)
    original_size = source_path.stat().st_size

    raw_path = (
        Path(temp_dir)
        / "store.bin"
    )

    copy_raw_file(
        source_path,
        raw_path
    )

    candidates = [{
        "codec": CODEC_STORE,
        "param_a": 0,
        "param_b": 0,
        "size": original_size,
        "path": raw_path,
        "label": "STORE",
    }]

    if request_method == REQUEST_DEFLATE:
        path = Path(temp_dir) / "deflate.bin"

        result = compress_deflate_candidate(
            source_path,
            path,
            profile,
            progress_callback
        )

        result.update({
            "path": path,
            "label": "DEFLATE",
        })

        candidates.append(result)

    elif request_method == REQUEST_LZMA2:
        path = Path(temp_dir) / "lzma2.bin"

        result = compress_lzma2_candidate(
            source_path,
            path,
            profile,
            progress_callback
        )

        result.update({
            "path": path,
            "label": "LZMA2",
        })

        candidates.append(result)

    elif request_method == REQUEST_PPMD:
        path = Path(temp_dir) / "ppmd.bin"

        result = compress_ppmd_candidate(
            source_path,
            path,
            profile,
            progress_callback
        )

        result.update({
            "path": path,
            "label": "PPMd7",
        })

        candidates.append(result)

    elif request_method == REQUEST_BROTLI:
        path = Path(temp_dir) / "brotli.bin"

        result = compress_brotli_candidate(
            source_path,
            path,
            profile,
            progress_callback
        )

        result.update({
            "path": path,
            "label": "Brotli",
        })

        candidates.append(result)

    else:
        # AUTO BEST:
        # LZMA2 + PPMd7 + Brotli.  STORE is always included.
        # Progress range is divided equally over the 3 real compressors.
        lzma_path = Path(temp_dir) / "lzma2.bin"
        ppmd_path = Path(temp_dir) / "ppmd.bin"
        brotli_path = Path(temp_dir) / "brotli.bin"

        def lzma_progress(current):
            if progress_callback and original_size > 0:
                progress_callback(
                    current / 3.0
                )

        def ppmd_progress(current):
            if progress_callback and original_size > 0:
                progress_callback(
                    original_size / 3.0
                    + current / 3.0
                )

        def brotli_progress(current):
            if progress_callback and original_size > 0:
                progress_callback(
                    2.0 * original_size / 3.0
                    + current / 3.0
                )

        lzma_result = compress_lzma2_candidate(
            source_path,
            lzma_path,
            profile,
            lzma_progress
        )

        lzma_result.update({
            "path": lzma_path,
            "label": "LZMA2",
        })

        candidates.append(lzma_result)

        ppmd_result = compress_ppmd_candidate(
            source_path,
            ppmd_path,
            profile,
            ppmd_progress
        )

        ppmd_result.update({
            "path": ppmd_path,
            "label": "PPMd7",
        })

        candidates.append(ppmd_result)

        brotli_result = compress_brotli_candidate(
            source_path,
            brotli_path,
            profile,
            brotli_progress
        )

        brotli_result.update({
            "path": brotli_path,
            "label": "Brotli",
        })

        candidates.append(brotli_result)

    winner = min(
        candidates,
        key=lambda item: item["size"]
    )

    sizes = {
        item["label"]: item["size"]
        for item in candidates
    }

    return winner, sizes


# ============================================================
# LZZ7 creation
# ============================================================

def create_archive(
    selected_items,
    archive_path,
    request_method=REQUEST_AUTO_BEST,
    profile=PROFILE_MAXIMUM,
    progress_callback=None,
):
    entries = collect_entries(selected_items)

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

    archive_path = Path(archive_path)

    archive_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    total_processed = 0
    file_index = 0

    winner_counts = {
        CODEC_STORE: 0,
        CODEC_DEFLATE: 0,
        CODEC_LZMA2: 0,
        CODEC_PPMD7: 0,
        CODEC_BROTLI: 0,
    }

    comparison_rows = []

    with tempfile.TemporaryDirectory(
        prefix="lzz7_",
        dir=str(archive_path.parent)
    ) as root_temp:

        with open(archive_path, "wb") as archive:
            archive.write(MAGIC_V7)
            archive.write(
                struct.pack(
                    "<H",
                    VERSION_V7
                )
            )
            archive.write(
                struct.pack(
                    "<H",
                    0
                )
            )
            archive.write(
                struct.pack(
                    "<I",
                    len(entries)
                )
            )

            for entry in entries:
                archive.write(
                    bytes([
                        entry["type"]
                    ])
                )

                path_bytes = entry["path"].encode("utf-8")

                archive.write(
                    struct.pack(
                        "<I",
                        len(path_bytes)
                    )
                )

                archive.write(path_bytes)

                if entry["type"] == ENTRY_DIR:
                    continue

                file_index += 1
                source_path = entry["source"]
                original_size = source_path.stat().st_size

                file_temp = (
                    Path(root_temp)
                    / f"file_{file_index}"
                )
                file_temp.mkdir()

                def local_progress(current):
                    if (
                        progress_callback
                        and total_original_size > 0
                    ):
                        progress_callback(
                            total_processed
                            + min(
                                original_size,
                                current
                            ),
                            total_original_size,
                            file_index,
                            len(file_entries),
                            entry["path"],
                        )

                winner, candidate_sizes = choose_candidate(
                    source_path,
                    file_temp,
                    request_method,
                    profile,
                    local_progress
                )

                crc = calculate_crc32(source_path)

                # Per-file metadata:
                # codec      uint8
                # profile    uint8
                # param_a    uint32
                # param_b    uint32
                # original   uint64
                # compressed uint64
                # crc32      uint32
                archive.write(
                    bytes([
                        winner["codec"]
                    ])
                )

                archive.write(
                    bytes([
                        profile
                    ])
                )

                archive.write(
                    struct.pack(
                        "<I",
                        winner["param_a"]
                    )
                )

                archive.write(
                    struct.pack(
                        "<I",
                        winner["param_b"]
                    )
                )

                archive.write(
                    struct.pack(
                        "<Q",
                        original_size
                    )
                )

                archive.write(
                    struct.pack(
                        "<Q",
                        winner["size"]
                    )
                )

                archive.write(
                    struct.pack(
                        "<I",
                        crc
                    )
                )

                with open(
                    winner["path"],
                    "rb"
                ) as payload_source:
                    shutil.copyfileobj(
                        payload_source,
                        archive,
                        length=IO_BUFFER_SIZE
                    )

                winner_counts[
                    winner["codec"]
                ] += 1

                comparison_rows.append({
                    "path": entry["path"],
                    "original": original_size,
                    "winner": codec_name(
                        winner["codec"]
                    ),
                    "sizes": candidate_sizes,
                })

                total_processed += original_size

    archive_size = archive_path.stat().st_size

    if total_original_size > 0:
        saving = (
            1.0
            - archive_size
            / total_original_size
        ) * 100.0
    else:
        saving = 0.0

    return {
        "file_count": len(file_entries),
        "dir_count": len(entries) - len(file_entries),
        "entry_count": len(entries),
        "original_size": total_original_size,
        "archive_size": archive_size,
        "saving": saving,
        "winner_counts": winner_counts,
        "comparisons": comparison_rows,
        "profile": profile,
        "request_method": request_method,
    }


# ============================================================
# LZZ5 token compatibility
# ============================================================

def reverse_lzz5_tokens(data, tokens):
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
                "Invalid LZZ5 token stream."
            )

        token_id = data[i + 1]
        i += 2

        if token_id == 0:
            output.append(TOKEN_MARKER)
            continue

        if token_id < 1 or token_id > len(tokens):
            raise ValueError(
                "Invalid LZZ5 token ID."
            )

        output.extend(
            tokens[token_id - 1]
        )

    return bytes(output)


# ============================================================
# Archive readers
# ============================================================

def read_main_header(archive):
    magic = archive.read(4)

    if magic == MAGIC_V7:
        version_raw = archive.read(2)
        flags_raw = archive.read(2)
        count_raw = archive.read(4)

        if (
            len(version_raw) != 2
            or len(flags_raw) != 2
            or len(count_raw) != 4
        ):
            raise ValueError(
                "LZZ7 header is incomplete."
            )

        version = struct.unpack(
            "<H",
            version_raw
        )[0]

        if version != VERSION_V7:
            raise ValueError(
                f"Unsupported LZZ7 version: {version}"
            )

        return {
            "format": 7,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
        }

    if magic == MAGIC_V6:
        version_raw = archive.read(2)
        flags_raw = archive.read(2)
        count_raw = archive.read(4)

        if (
            len(version_raw) != 2
            or len(flags_raw) != 2
            or len(count_raw) != 4
        ):
            raise ValueError(
                "LZZ6 header is incomplete."
            )

        return {
            "format": 6,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
        }

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

        return {
            "format": 4,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
            "global_codec": method_raw[0],
            "global_profile": profile_raw[0],
            "global_param_a": struct.unpack(
                "<I",
                param_raw
            )[0],
            "global_param_b": 0,
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

        return {
            "format": 3,
            "entry_count": struct.unpack(
                "<I",
                count_raw
            )[0],
            "global_codec": CODEC_DEFLATE,
            "global_profile": PROFILE_MAXIMUM,
            "global_param_a": 0,
            "global_param_b": 0,
        }

    raise ValueError(
        "This is not a valid "
        "LZZ3/LZZ4/LZZ5/LZZ6/LZZ7 archive."
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

    path_data = archive.read(path_len)

    if len(path_data) != path_len:
        raise ValueError(
            "Archive path data is incomplete."
        )

    try:
        path_text = path_data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(
            "Archive contains an invalid UTF-8 path."
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

    if main_header["format"] in (
        6,
        7,
    ):
        metadata = archive.read(30)

        if len(metadata) != 30:
            raise ValueError(
                "LZZ6/LZZ7 file metadata is incomplete."
            )

        return {
            "type": ENTRY_FILE,
            "path": path_text,
            "codec": metadata[0],
            "profile": metadata[1],
            "param_a": struct.unpack(
                "<I",
                metadata[2:6]
            )[0],
            "param_b": struct.unpack(
                "<I",
                metadata[6:10]
            )[0],
            "original_size": struct.unpack(
                "<Q",
                metadata[10:18]
            )[0],
            "compressed_size": struct.unpack(
                "<Q",
                metadata[18:26]
            )[0],
            "crc32": struct.unpack(
                "<I",
                metadata[26:30]
            )[0],
            "tokens": [],
        }

    if main_header["format"] == 5:
        metadata = archive.read(28)

        if len(metadata) != 28:
            raise ValueError(
                "LZZ5 file metadata is incomplete."
            )

        token_count = struct.unpack(
            "<H",
            metadata[26:28]
        )[0]

        if token_count > MAX_TOKENS:
            raise ValueError(
                "Invalid LZZ5 token count."
            )

        tokens = []

        for _ in range(token_count):
            token_len_raw = archive.read(2)

            if len(token_len_raw) != 2:
                raise ValueError(
                    "LZZ5 token table is incomplete."
                )

            token_len = struct.unpack(
                "<H",
                token_len_raw
            )[0]

            token = archive.read(token_len)

            if len(token) != token_len:
                raise ValueError(
                    "LZZ5 token data is incomplete."
                )

            tokens.append(token)

        return {
            "type": ENTRY_FILE,
            "path": path_text,
            "codec": metadata[0],
            "profile": metadata[1],
            "param_a": struct.unpack(
                "<I",
                metadata[2:6]
            )[0],
            "param_b": 0,
            "original_size": struct.unpack(
                "<Q",
                metadata[6:14]
            )[0],
            "compressed_size": struct.unpack(
                "<Q",
                metadata[14:22]
            )[0],
            "crc32": struct.unpack(
                "<I",
                metadata[22:26]
            )[0],
            "tokens": tokens,
        }

    metadata = archive.read(20)

    if len(metadata) != 20:
        raise ValueError(
            "Legacy file metadata is incomplete."
        )

    return {
        "type": ENTRY_FILE,
        "path": path_text,
        "codec": main_header[
            "global_codec"
        ],
        "profile": main_header[
            "global_profile"
        ],
        "param_a": main_header[
            "global_param_a"
        ],
        "param_b": main_header[
            "global_param_b"
        ],
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
# Payload decoding
# ============================================================

def decompress_ppmd(
    compressed,
    original_size,
    mem_size,
    max_order
):
    if pyppmd is None:
        raise RuntimeError(
            "This archive contains PPMd data, "
            "but 'pyppmd' is not installed.\n\n"
            "Install it with:\n"
            f'"{sys.executable}" -m pip install pyppmd'
        )

    decoder = pyppmd.Ppmd7Decoder(
        max_order=max_order,
        mem_size=mem_size,
    )

    result = decoder.decode(
        compressed,
        original_size
    )

    while len(result) < original_size:
        remaining = (
            original_size
            - len(result)
        )

        if decoder.needs_input:
            more = decoder.decode(
                b"\0",
                remaining
            )
        else:
            more = decoder.decode(
                b"",
                remaining
            )

        if not more:
            break

        result += more

    return result


def decompress_brotli(
    compressed
):
    if brotli is None:
        raise RuntimeError(
            "This archive contains Brotli data, "
            "but 'brotli' is not installed.\n\n"
            "Install it with:\n"
            f'"{sys.executable}" -m pip install brotli'
        )

    try:
        return brotli.decompress(
            compressed
        )
    except Exception as error:
        raise ValueError(
            f"Brotli decompression failed: {error}"
        )


def decode_payload(
    archive,
    entry
):
    compressed = archive.read(
        entry["compressed_size"]
    )

    if len(compressed) != entry["compressed_size"]:
        raise ValueError(
            "Compressed payload ended unexpectedly."
        )

    codec = entry["codec"]

    if codec == CODEC_STORE:
        restored = compressed

    elif codec == CODEC_DEFLATE:
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
                        "dict_size": entry["param_a"],
                    }
                ],
            )
        except lzma.LZMAError as error:
            raise ValueError(
                f"LZMA2 decompression failed: {error}"
            )

        if codec == CODEC_TEXT_LZMA2:
            restored = reverse_lzz5_tokens(
                restored,
                entry["tokens"]
            )

    elif codec == CODEC_PPMD7:
        restored = decompress_ppmd(
            compressed,
            entry["original_size"],
            entry["param_a"],
            entry["param_b"],
        )

    elif codec == CODEC_BROTLI:
        restored = decompress_brotli(
            compressed
        )

    else:
        raise ValueError(
            f"Unsupported codec: {codec}"
        )

    if len(restored) != entry["original_size"]:
        raise ValueError(
            "Restored size does not match archive metadata."
        )

    return restored


# ============================================================
# Archive extraction
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

    with open(archive_path, "rb") as archive:
        main_header = read_main_header(archive)
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
            total_original_size += entry["original_size"]

            archive.seek(
                entry["compressed_size"],
                os.SEEK_CUR
            )

        archive.seek(entries_start)

        restored_total = 0
        current_file_index = 0
        codec_counts = {}

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

            restored = decode_payload(
                archive,
                entry
            )

            actual_crc = (
                zlib.crc32(restored)
                & 0xFFFFFFFF
            )

            if actual_crc != entry["crc32"]:
                raise ValueError(
                    f"CRC32 verification failed:\n"
                    f"{entry['path']}"
                )

            target.write_bytes(restored)

            restored_total += len(restored)

            codec_counts[
                entry["codec"]
            ] = (
                codec_counts.get(
                    entry["codec"],
                    0
                )
                + 1
            )

            if (
                progress_callback
                and total_original_size > 0
            ):
                progress_callback(
                    restored_total,
                    total_original_size,
                    current_file_index,
                    file_count,
                    entry["path"],
                )

    return {
        "format": main_header["format"],
        "file_count": file_count,
        "dir_count": dir_count,
        "restored_size": restored_total,
        "codec_counts": codec_counts,
    }


# ============================================================
# GUI
# ============================================================

class LZZApp:

    def __init__(self, root):
        self.root = root

        self.root.title(APP_TITLE)
        self.root.geometry("1060x840")
        self.root.minsize(920, 720)

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
                "LZZ7 Auto Best tests LZMA2, PPMd7 and Brotli for every file "
                "and stores whichever result is smallest."
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

        list_frame = ttk.Frame(content_box)

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

        controls = ttk.Frame(content_box)

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
            value=(
                "Auto Best "
                "(LZMA2 vs PPMd vs Brotli)"
            )
        )

        self.method_combo = ttk.Combobox(
            settings,
            textvariable=self.method_var,
            state="readonly",
            width=38,
            values=[
                "Auto Best (LZMA2 vs PPMd vs Brotli)",
                "LZMA2",
                "PPMd7",
                "Brotli",
                "DEFLATE",
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

        self.module_status_var = tk.StringVar()

        ttk.Label(
            settings,
            textvariable=self.module_status_var
        ).grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0)
        )

        self.update_module_status()

        actions = ttk.Frame(outer)

        actions.pack(pady=16)

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

        progress_box.pack(fill="x")

        self.progress_var = tk.DoubleVar(
            value=0
        )

        ttk.Progressbar(
            progress_box,
            maximum=100,
            variable=self.progress_var
        ).pack(fill="x")

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
                "LZMA2 winners: -\n"
                "PPMd winners: -\n"
                "Brotli winners: -"
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
                "Trackable version: LZZ7 | Auto Best Brotli / PPMd7 / LZMA2 | "
                "CRC32 | Reads LZZ3-LZZ7"
            ),
            font=("Segoe UI", 9)
        ).pack(
            anchor="w",
            pady=(12, 0)
        )

    def update_module_status(self):
        ppmd_text = (
            "PPMd ready"
            if pyppmd is not None
            else "PPMd missing"
        )

        brotli_text = (
            "Brotli ready"
            if brotli is not None
            else "Brotli missing"
        )

        self.module_status_var.set(
            f"{ppmd_text} | {brotli_text}. "
            "Missing packages can be installed automatically."
        )

    def install_package(
        self,
        package_name,
        reload_function
    ):
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    package_name,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode != 0:
                messagebox.showerror(
                    "Installation Failed",
                    (
                        f"Could not install {package_name}.\n\n"
                        f"Run manually:\n"
                        f'"{sys.executable}" -m pip install {package_name}\n\n'
                        f"{result.stderr[-1500:]}"
                    )
                )
                return False

            if not reload_function():
                messagebox.showerror(
                    "Installation Failed",
                    (
                        f"{package_name} was installed but could not be imported.\n"
                        "Restart Python and try again."
                    )
                )
                return False

            return True

        except Exception as error:
            messagebox.showerror(
                "Installation Error",
                str(error)
            )
            return False

    def ensure_required_modules(self, request_method):
        need_ppmd = (
            request_method
            in (
                REQUEST_AUTO_BEST,
                REQUEST_PPMD,
            )
        )

        need_brotli = (
            request_method
            in (
                REQUEST_AUTO_BEST,
                REQUEST_BROTLI,
            )
        )

        missing = []

        if need_ppmd and pyppmd is None:
            missing.append("pyppmd")

        if need_brotli and brotli is None:
            missing.append("brotli")

        if not missing:
            return True

        answer = messagebox.askyesno(
            "Install Compression Support",
            (
                "The selected mode needs these Python packages:\n\n"
                + "\n".join(
                    f"• {name}"
                    for name in missing
                )
                + "\n\nInstall them now using your current Python?"
            )
        )

        if not answer:
            return False

        self.status_var.set(
            "Installing compression packages..."
        )
        self.root.update_idletasks()

        if "pyppmd" in missing:
            if not self.install_package(
                "pyppmd",
                reload_pyppmd
            ):
                return False

        if "brotli" in missing:
            if not self.install_package(
                "brotli",
                reload_brotli
            ):
                return False

        self.update_module_status()

        messagebox.showinfo(
            "Ready",
            "Required compression packages are installed."
        )

        return True

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
                self.items.append(resolved)

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
                self.items.append(resolved)

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
            f"LZMA2 winners: -\n"
            f"PPMd winners: -\n"
            f"Brotli winners: -"
        )

        self.status_var.set(
            (
                f"{len(self.items)} item(s) selected."
            )
            if self.items
            else "Ready"
        )

    def get_request_method(self):
        text = self.method_var.get()

        if text == "LZMA2":
            return REQUEST_LZMA2

        if text == "PPMd7":
            return REQUEST_PPMD

        if text == "Brotli":
            return REQUEST_BROTLI

        if text == "DEFLATE":
            return REQUEST_DEFLATE

        return REQUEST_AUTO_BEST

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

        request_method = self.get_request_method()

        if not self.ensure_required_modules(
            request_method
        ):
            return

        output = filedialog.asksaveasfilename(
            title="Save LZZ7 archive",
            initialdir=default_archive_directory(
                self.items
            ),
            initialfile=default_archive_name(
                self.items
            ),
            defaultextension=".lzz",
            filetypes=[
                ("LZZ Archive", "*.lzz"),
                ("All files", "*.*")
            ]
        )

        if output:
            self.start_create(
                output,
                request_method
            )

    def start_create(
        self,
        output,
        request_method
    ):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)

        self.status_var.set(
            "Creating LZZ7 archive..."
        )

        threading.Thread(
            target=self.create_worker,
            args=(
                list(self.items),
                output,
                request_method,
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
                        current
                        * 100.0
                        / total
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
                    (
                        f"Compressing "
                        f"{file_index}/{file_count}: "
                        f"{label}"
                    )
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
        self.progress_var.set(100)

        counts = result["winner_counts"]

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: {result['file_count']}\n"
            f"Directories: {result['dir_count']}\n"
            f"Original size: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%\n"
            f"LZMA2 winners: {counts.get(CODEC_LZMA2, 0)}\n"
            f"PPMd winners: {counts.get(CODEC_PPMD7, 0)}\n"
            f"Brotli winners: {counts.get(CODEC_BROTLI, 0)}\n"
            f"DEFLATE winners: {counts.get(CODEC_DEFLATE, 0)}\n"
            f"Stored uncompressed: {counts.get(CODEC_STORE, 0)}"
        )

        self.status_var.set(
            "Archive created successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            (
                "LZZ7 archive created successfully.\n\n"
                f"{output}\n\n"
                f"Original: "
                f"{human_size(result['original_size'])}\n"
                f"Archive: "
                f"{human_size(result['archive_size'])}\n"
                f"Saving: "
                f"{result['saving']:.2f}%\n\n"
                f"LZMA2 winners: "
                f"{counts.get(CODEC_LZMA2, 0)}\n"
                f"PPMd winners: "
                f"{counts.get(CODEC_PPMD7, 0)}\n"
                f"Brotli winners: "
                f"{counts.get(CODEC_BROTLI, 0)}"
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
            result = extract_archive(
                archive_file,
                output_folder,
                progress_callback=self.extraction_progress,
            )

            self.root.after(
                0,
                self.finish_extract,
                result,
                output_folder
            )

        except RuntimeError as error:
            self.root.after(
                0,
                self.finish_runtime_error,
                str(error)
            )

        except Exception as error:
            self.root.after(
                0,
                self.finish_error,
                str(error)
            )

    def extraction_progress(
        self,
        current,
        total,
        file_index,
        file_count,
        label
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
            (
                f"Extracting "
                f"{file_index}/{file_count}: "
                f"{label}"
            )
        )

    def finish_runtime_error(
        self,
        error_message
    ):
        self.busy = False
        self.set_controls(True)

        self.status_var.set(
            "Additional codec support required."
        )

        messagebox.showerror(
            "Codec Support Required",
            error_message
        )

    def finish_extract(
        self,
        result,
        output_folder
    ):
        self.progress_var.set(100)

        counts = result["codec_counts"]

        self.result_var.set(
            f"Archive format: LZZ{result['format']}\n"
            f"Files extracted: {result['file_count']}\n"
            f"Directories restored: {result['dir_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"LZMA2 files: {counts.get(CODEC_LZMA2, 0)}\n"
            f"PPMd files: {counts.get(CODEC_PPMD7, 0)}\n"
            f"Brotli files: {counts.get(CODEC_BROTLI, 0)}\n"
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
                f"Restored: "
                f"{human_size(result['restored_size'])}\n"
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


# ============================================================
# Main
# ============================================================

def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
