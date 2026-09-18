import os
import struct
import threading
import tkinter as tk
import zlib
import tempfile
import shutil
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LZZ Archive Manager"

# ============================================================
# LZZ2 FORMAT
# ============================================================
#
# Header:
#   4 bytes  : MAGIC = LZZ2
#   2 bytes  : VERSION = 2
#   4 bytes  : entry count
#
# Entry:
#   1 byte   : type (0=file, 1=directory)
#   4 bytes  : UTF-8 relative path length
#   N bytes  : UTF-8 relative path
#
# If file:
#   8 bytes  : original size
#   8 bytes  : compressed size
#   4 bytes  : CRC32
#   N bytes  : LZSS payload
#
# Iterative wrapper remains:
#   LZI1 + version + sizes + CRC32 + LZSS payload
#
# ============================================================

BASE_MAGIC = b"LZZ2"
BASE_VERSION = 2

ITER_MAGIC = b"LZI1"
ITER_VERSION = 1

ENTRY_FILE = 0
ENTRY_DIR = 1

WINDOW_SIZE = 4095
MIN_MATCH = 3
MAX_MATCH = 18
AUTO_MAX_PASSES = 20


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
    p = str(path_text).replace("\\", "/")

    while p.startswith("/"):
        p = p[1:]

    parts = []

    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Parent path '..' is not allowed in archive entries.")
        parts.append(part)

    if not parts:
        raise ValueError("Invalid empty archive path.")

    return "/".join(parts)


def safe_output_path(root, archive_path):
    root = Path(root).resolve()

    normalized = normalize_archive_path(archive_path)

    candidate = (root / Path(*normalized.split("/"))).resolve()

    # Prevent path traversal outside extraction root.
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"Unsafe archive path: {archive_path}")

    return candidate


def add_to_window(window, data):
    window.extend(data)

    if len(window) > WINDOW_SIZE:
        del window[:len(window) - WINDOW_SIZE]


def fill_lookahead(source, lookahead):
    needed = MAX_MATCH - len(lookahead)

    if needed > 0:
        chunk = source.read(needed)

        if chunk:
            lookahead.extend(chunk)


def find_longest_match(window, lookahead):
    if len(window) < MIN_MATCH or len(lookahead) < MIN_MATCH:
        return 0, 0

    max_len = min(MAX_MATCH, len(lookahead), len(window))

    for length in range(max_len, MIN_MATCH - 1, -1):
        sequence = bytes(lookahead[:length])
        pos = window.rfind(sequence)

        if pos != -1:
            offset = len(window) - pos

            if 1 <= offset <= WINDOW_SIZE:
                return offset, length

    return 0, 0


class BoundedReader:
    def __init__(self, source, size):
        self.source = source
        self.remaining = size

    def read(self, count=-1):
        if self.remaining <= 0:
            return b""

        if count < 0 or count > self.remaining:
            count = self.remaining

        data = self.source.read(count)
        self.remaining -= len(data)

        return data


# ============================================================
# LZSS stream compression
# ============================================================

def compress_stream(source, destination, original_size, progress_callback=None):
    processed = 0
    window = bytearray()
    lookahead = bytearray()
    crc = 0

    fill_lookahead(source, lookahead)

    while lookahead:
        flags = 0
        tokens = bytearray()

        for bit in range(8):
            if not lookahead:
                break

            offset, length = find_longest_match(window, lookahead)

            if length >= MIN_MATCH:
                flags |= (1 << bit)

                encoded = (
                    (offset << 4)
                    |
                    (length - MIN_MATCH)
                )

                tokens.extend(
                    struct.pack(">H", encoded)
                )

                consumed = bytes(lookahead[:length])

                crc = zlib.crc32(consumed, crc)

                del lookahead[:length]

                add_to_window(window, consumed)

                processed += length

            else:
                value = lookahead[0]
                data = bytes([value])

                tokens.append(value)

                crc = zlib.crc32(data, crc)

                del lookahead[0]

                add_to_window(window, data)

                processed += 1

            fill_lookahead(source, lookahead)

        destination.write(bytes([flags]))
        destination.write(tokens)

        if progress_callback and original_size > 0:
            progress_callback(processed)

    return processed, crc & 0xFFFFFFFF


def decompress_stream(source, destination, original_size, progress_callback=None):
    history = bytearray()
    restored = 0
    crc = 0

    while restored < original_size:
        flag_data = source.read(1)

        if not flag_data:
            raise ValueError("Compressed payload ended unexpectedly.")

        flags = flag_data[0]

        for bit in range(8):
            if restored >= original_size:
                break

            is_match = (flags >> bit) & 1

            if not is_match:
                raw = source.read(1)

                if not raw:
                    raise ValueError("Missing literal byte.")

                value = raw[0]
                data = bytes([value])

                destination.write(data)
                crc = zlib.crc32(data, crc)

                add_to_window(history, data)

                restored += 1

            else:
                raw = source.read(2)

                if len(raw) != 2:
                    raise ValueError("Missing LZSS match data.")

                encoded = struct.unpack(">H", raw)[0]

                offset = encoded >> 4
                length = (encoded & 0x0F) + MIN_MATCH

                if offset == 0 or offset > len(history):
                    raise ValueError("Invalid LZSS offset.")

                for _ in range(length):
                    if restored >= original_size:
                        break

                    value = history[len(history) - offset]
                    data = bytes([value])

                    destination.write(data)
                    crc = zlib.crc32(data, crc)

                    add_to_window(history, data)

                    restored += 1

            if progress_callback and original_size > 0:
                progress_callback(restored)

    return restored, crc & 0xFFFFFFFF


# ============================================================
# Build entry list from selected files/folders
# ============================================================

def collect_entries(selected_items):
    """
    Returns a list of dicts:
        {"type": ENTRY_DIR,  "path": "Folder/Sub"}
        {"type": ENTRY_FILE, "path": "Folder/Sub/a.txt", "source": Path(...)}

    Selected folders are stored including their top-level folder name.
    Selected individual files are stored using only their file name.
    """

    entries = []
    seen = set()

    def add_dir(archive_path):
        archive_path = normalize_archive_path(archive_path)
        key = ("D", archive_path.lower())

        if key not in seen:
            seen.add(key)
            entries.append({
                "type": ENTRY_DIR,
                "path": archive_path
            })

    def add_file(source_path, archive_path):
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
            "source": Path(source_path)
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

        elif path.is_dir():
            root_name = path.name
            add_dir(root_name)

            for current_root, dir_names, file_names in os.walk(path):
                current_root = Path(current_root)

                relative = current_root.relative_to(path)

                if relative.parts:
                    current_archive_dir = "/".join(
                        (root_name,) + relative.parts
                    )
                    add_dir(current_archive_dir)
                else:
                    current_archive_dir = root_name

                # Explicitly record subdirectories so empty dirs survive.
                for directory in dir_names:
                    rel_dir = (
                        current_root / directory
                    ).relative_to(path)

                    archive_dir = "/".join(
                        (root_name,) + rel_dir.parts
                    )

                    add_dir(archive_dir)

                for file_name in file_names:
                    source_file = current_root / file_name

                    rel_file = source_file.relative_to(path)

                    archive_file = "/".join(
                        (root_name,) + rel_file.parts
                    )

                    add_file(
                        source_file,
                        archive_file
                    )

    # Directories first, shortest paths first, then files.
    entries.sort(
        key=lambda e: (
            0 if e["type"] == ENTRY_DIR else 1,
            e["path"].count("/"),
            e["path"].lower()
        )
    )

    return entries


# ============================================================
# Base LZZ2 archive create/extract
# ============================================================

def create_base_archive(selected_items, archive_path, progress_callback=None):
    entries = collect_entries(selected_items)

    if not entries:
        raise ValueError("No files or folders selected.")

    file_entries = [
        e for e in entries
        if e["type"] == ENTRY_FILE
    ]

    total_original_size = sum(
        e["source"].stat().st_size
        for e in file_entries
    )

    total_processed = 0

    with open(archive_path, "wb") as archive:
        archive.write(BASE_MAGIC)
        archive.write(
            struct.pack("<H", BASE_VERSION)
        )
        archive.write(
            struct.pack("<I", len(entries))
        )

        current_file_index = 0

        for entry in entries:
            archive.write(
                bytes([entry["type"]])
            )

            path_bytes = entry["path"].encode("utf-8")

            archive.write(
                struct.pack("<I", len(path_bytes))
            )
            archive.write(path_bytes)

            if entry["type"] == ENTRY_DIR:
                continue

            current_file_index += 1

            source_path = entry["source"]
            original_size = source_path.stat().st_size

            archive.write(
                struct.pack("<Q", original_size)
            )

            compressed_size_pos = archive.tell()
            archive.write(
                struct.pack("<Q", 0)
            )

            crc_pos = archive.tell()
            archive.write(
                struct.pack("<I", 0)
            )

            payload_start = archive.tell()

            def per_file_progress(current):
                if progress_callback:
                    progress_callback(
                        total_processed + current,
                        total_original_size,
                        current_file_index,
                        len(file_entries),
                        entry["path"]
                    )

            with open(source_path, "rb") as source:
                processed, crc = compress_stream(
                    source,
                    archive,
                    original_size,
                    per_file_progress
                )

            payload_end = archive.tell()
            compressed_size = payload_end - payload_start

            archive.seek(compressed_size_pos)
            archive.write(
                struct.pack("<Q", compressed_size)
            )

            archive.seek(crc_pos)
            archive.write(
                struct.pack("<I", crc)
            )

            archive.seek(payload_end)

            total_processed += processed

    archive_size = Path(archive_path).stat().st_size

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
    }


def extract_base_archive(archive_path, output_folder, progress_callback=None):
    archive_path = Path(archive_path)
    output_folder = Path(output_folder)

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(archive_path, "rb") as archive:
        if archive.read(4) != BASE_MAGIC:
            raise ValueError(
                "This is not a valid LZZ2 archive."
            )

        version_raw = archive.read(2)

        if len(version_raw) != 2:
            raise ValueError("Archive header is damaged.")

        version = struct.unpack("<H", version_raw)[0]

        if version != BASE_VERSION:
            raise ValueError(
                f"Unsupported LZZ2 version: {version}"
            )

        count_raw = archive.read(4)

        if len(count_raw) != 4:
            raise ValueError("Archive entry count is missing.")

        entry_count = struct.unpack("<I", count_raw)[0]

        # First pass to compute total file bytes for progress.
        entries_start = archive.tell()
        total_original_size = 0
        total_file_count = 0

        for _ in range(entry_count):
            type_raw = archive.read(1)

            if len(type_raw) != 1:
                raise ValueError("Archive entry is damaged.")

            entry_type = type_raw[0]

            name_len_raw = archive.read(4)

            if len(name_len_raw) != 4:
                raise ValueError("Archive path length is missing.")

            name_len = struct.unpack("<I", name_len_raw)[0]

            if name_len > 1024 * 1024:
                raise ValueError("Archive path length is unreasonable.")

            archive.seek(name_len, os.SEEK_CUR)

            if entry_type == ENTRY_DIR:
                continue

            if entry_type != ENTRY_FILE:
                raise ValueError(
                    f"Unknown archive entry type: {entry_type}"
                )

            original_raw = archive.read(8)
            compressed_raw = archive.read(8)
            crc_raw = archive.read(4)

            if (
                len(original_raw) != 8
                or len(compressed_raw) != 8
                or len(crc_raw) != 4
            ):
                raise ValueError("Archive file metadata is damaged.")

            original_size = struct.unpack("<Q", original_raw)[0]
            compressed_size = struct.unpack("<Q", compressed_raw)[0]

            total_original_size += original_size
            total_file_count += 1

            archive.seek(
                compressed_size,
                os.SEEK_CUR
            )

        # Second pass: extract.
        archive.seek(entries_start)

        total_restored = 0
        current_file_index = 0
        dir_count = 0

        for _ in range(entry_count):
            entry_type = archive.read(1)[0]

            name_len = struct.unpack(
                "<I",
                archive.read(4)
            )[0]

            name_bytes = archive.read(name_len)

            try:
                archive_path_text = name_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Invalid UTF-8 archive path.")

            target = safe_output_path(
                output_folder,
                archive_path_text
            )

            if entry_type == ENTRY_DIR:
                target.mkdir(
                    parents=True,
                    exist_ok=True
                )
                dir_count += 1
                continue

            if entry_type != ENTRY_FILE:
                raise ValueError(
                    f"Unknown archive entry type: {entry_type}"
                )

            current_file_index += 1

            original_size = struct.unpack(
                "<Q",
                archive.read(8)
            )[0]

            compressed_size = struct.unpack(
                "<Q",
                archive.read(8)
            )[0]

            expected_crc = struct.unpack(
                "<I",
                archive.read(4)
            )[0]

            target.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            bounded = BoundedReader(
                archive,
                compressed_size
            )

            def per_file_progress(current):
                if progress_callback:
                    progress_callback(
                        total_restored + current,
                        total_original_size,
                        current_file_index,
                        total_file_count,
                        archive_path_text
                    )

            with open(target, "wb") as destination:
                restored, actual_crc = decompress_stream(
                    bounded,
                    destination,
                    original_size,
                    per_file_progress
                )

            if bounded.remaining != 0:
                raise ValueError(
                    f"Compressed size mismatch:\n{archive_path_text}"
                )

            if restored != original_size:
                raise ValueError(
                    f"Restored size mismatch:\n{archive_path_text}"
                )

            if actual_crc != expected_crc:
                raise ValueError(
                    f"CRC32 verification failed:\n{archive_path_text}"
                )

            total_restored += restored

    return {
        "entry_count": entry_count,
        "file_count": total_file_count,
        "dir_count": dir_count,
        "restored_size": total_restored,
    }


# ============================================================
# Iterative wrapper
# ============================================================

def wrap_once(input_path, output_path, progress_callback=None):
    input_path = Path(input_path)

    original_size = input_path.stat().st_size

    with open(input_path, "rb") as source, \
         open(output_path, "wb") as destination:

        destination.write(ITER_MAGIC)
        destination.write(
            struct.pack("<H", ITER_VERSION)
        )
        destination.write(
            struct.pack("<Q", original_size)
        )

        compressed_size_pos = destination.tell()
        destination.write(
            struct.pack("<Q", 0)
        )

        crc_pos = destination.tell()
        destination.write(
            struct.pack("<I", 0)
        )

        payload_start = destination.tell()

        processed, crc = compress_stream(
            source,
            destination,
            original_size,
            progress_callback
        )

        payload_end = destination.tell()
        compressed_size = payload_end - payload_start

        destination.seek(compressed_size_pos)
        destination.write(
            struct.pack("<Q", compressed_size)
        )

        destination.seek(crc_pos)
        destination.write(
            struct.pack("<I", crc)
        )

        destination.seek(payload_end)

    return {
        "original_size": original_size,
        "final_size": Path(output_path).stat().st_size,
        "crc32": crc
    }


def unwrap_once(input_path, output_path, progress_callback=None):
    with open(input_path, "rb") as source:
        if source.read(4) != ITER_MAGIC:
            raise ValueError("Not an LZI1 wrapper.")

        version_raw = source.read(2)

        if len(version_raw) != 2:
            raise ValueError("Iterative header is damaged.")

        version = struct.unpack("<H", version_raw)[0]

        if version != ITER_VERSION:
            raise ValueError(
                f"Unsupported LZI1 version: {version}"
            )

        original_size = struct.unpack(
            "<Q",
            source.read(8)
        )[0]

        compressed_size = struct.unpack(
            "<Q",
            source.read(8)
        )[0]

        expected_crc = struct.unpack(
            "<I",
            source.read(4)
        )[0]

        bounded = BoundedReader(
            source,
            compressed_size
        )

        with open(output_path, "wb") as destination:
            restored, crc = decompress_stream(
                bounded,
                destination,
                original_size,
                progress_callback
            )

        if restored != original_size:
            raise ValueError("Iterative layer size mismatch.")

        if crc != expected_crc:
            raise ValueError("Iterative layer CRC32 failed.")

        if bounded.remaining != 0:
            raise ValueError("Iterative payload size mismatch.")


def create_archive(
    selected_items,
    archive_path,
    progress_callback=None,
    max_passes=AUTO_MAX_PASSES
):
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with tempfile.TemporaryDirectory(
        prefix="lzz_auto_",
        dir=str(archive_path.parent)
    ) as temp_dir:
        temp_dir = Path(temp_dir)

        base_path = temp_dir / "base.lzz"

        base_result = create_base_archive(
            selected_items,
            base_path,
            progress_callback
        )

        current_path = base_path
        current_size = current_path.stat().st_size

        pass_sizes = [current_size]
        accepted_passes = 0

        for pass_number in range(
            1,
            max_passes + 1
        ):
            candidate = temp_dir / f"pass_{pass_number}.lzz"

            before_size = current_path.stat().st_size

            def outer_progress(current):
                if progress_callback:
                    progress_callback(
                        current,
                        before_size,
                        pass_number,
                        max_passes,
                        f"Optimization pass {pass_number}"
                    )

            wrap_once(
                current_path,
                candidate,
                outer_progress
            )

            candidate_size = candidate.stat().st_size

            if candidate_size < current_size:
                current_path = candidate
                current_size = candidate_size
                accepted_passes += 1
                pass_sizes.append(candidate_size)
            else:
                break

        shutil.copyfile(
            current_path,
            archive_path
        )

    final_size = archive_path.stat().st_size
    original_size = base_result["original_size"]

    if original_size > 0:
        saving = (
            1.0 -
            final_size / original_size
        ) * 100.0
    else:
        saving = 0.0

    result = dict(base_result)
    result.update({
        "base_archive_size": pass_sizes[0],
        "archive_size": final_size,
        "saving": saving,
        "iterative_passes": accepted_passes,
        "pass_sizes": pass_sizes,
    })

    return result


def extract_archive(
    archive_path,
    output_folder,
    progress_callback=None
):
    archive_path = Path(archive_path)

    with tempfile.TemporaryDirectory(
        prefix="lzz_extract_",
        dir=str(archive_path.parent)
    ) as temp_dir:
        temp_dir = Path(temp_dir)

        current = archive_path
        layers = 0

        while True:
            with open(current, "rb") as f:
                magic = f.read(4)

            if magic == BASE_MAGIC:
                break

            if magic != ITER_MAGIC:
                raise ValueError(
                    "This is not a valid LZZ2/LZI1 archive."
                )

            layers += 1

            next_file = temp_dir / f"unwrapped_{layers}.lzz"

            current_size = current.stat().st_size

            def unwrap_progress(value):
                if progress_callback:
                    progress_callback(
                        value,
                        current_size,
                        layers,
                        layers,
                        f"Unwrapping layer {layers}"
                    )

            unwrap_once(
                current,
                next_file,
                unwrap_progress
            )

            current = next_file

            if layers > AUTO_MAX_PASSES + 10:
                raise ValueError("Too many iterative layers.")

        result = extract_base_archive(
            current,
            output_folder,
            progress_callback
        )

    result["iterative_passes"] = layers

    return result


# ============================================================
# Windows GUI
# ============================================================

class LZZApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("950x680")
        self.root.minsize(850, 620)

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
                "Add individual files and/or complete folders. "
                "Subfolders and empty directories are preserved."
            )
        ).pack(
            anchor="w",
            pady=(4, 16)
        )

        box = ttk.LabelFrame(
            outer,
            text="Archive contents",
            padding=10
        )
        box.pack(
            fill="both",
            expand=True
        )

        list_frame = ttk.Frame(box)
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

        controls = ttk.Frame(box)
        controls.pack(
            fill="x",
            pady=(10, 0)
        )

        self.add_files_btn = ttk.Button(
            controls,
            text="Add Files...",
            command=self.add_files
        )
        self.add_files_btn.pack(side="left")

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
            command=self.clear
        )
        self.clear_btn.pack(
            side="left",
            padx=(8, 0)
        )

        actions = ttk.Frame(outer)
        actions.pack(pady=16)

        self.create_btn = ttk.Button(
            actions,
            text="CREATE .LZZ ARCHIVE",
            width=26,
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
            width=26,
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

        self.progress_var = tk.DoubleVar(value=0)

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
                "Archive size: -"
            )
        )

        ttk.Label(
            result_box,
            textvariable=self.result_var,
            justify="left"
        ).pack(anchor="w")

    def add_files(self):
        if self.busy:
            return

        files = filedialog.askopenfilenames(
            title="Select files",
            filetypes=[("All files", "*.*")]
        )

        for f in files:
            resolved = str(Path(f).resolve())

            if resolved not in self.items:
                self.items.append(resolved)

        self.refresh()

    def add_folder(self):
        if self.busy:
            return

        folder = filedialog.askdirectory(
            title="Select folder"
        )

        if folder:
            resolved = str(Path(folder).resolve())

            if resolved not in self.items:
                self.items.append(resolved)

        self.refresh()

    def remove_selected(self):
        indexes = list(
            self.listbox.curselection()
        )

        for i in reversed(indexes):
            del self.items[i]

        self.refresh()

    def clear(self):
        self.items.clear()
        self.refresh()

    def refresh(self):
        self.listbox.delete(
            0,
            tk.END
        )

        for item in self.items:
            p = Path(item)

            prefix = "[DIR] " if p.is_dir() else "[FILE] "

            self.listbox.insert(
                tk.END,
                prefix + str(p)
            )

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: -\n"
            f"Directories: -\n"
            f"Archive size: -"
        )

        self.status_var.set(
            f"{len(self.items)} item(s) selected."
            if self.items
            else "Ready"
        )

    def choose_output(self):
        if not self.items:
            messagebox.showwarning(
                "Warning",
                "Add at least one file or folder."
            )
            return

        output = filedialog.asksaveasfilename(
            title="Save .lzz archive",
            initialfile="archive.lzz",
            defaultextension=".lzz",
            filetypes=[
                ("LZZ archive", "*.lzz"),
                ("All files", "*.*")
            ]
        )

        if output:
            self.start_create(output)

    def choose_extract(self):
        archive_file = filedialog.askopenfilename(
            title="Select .lzz archive",
            filetypes=[
                ("LZZ archive", "*.lzz"),
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

    def set_controls(self, enabled):
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
            self.extract_btn
        ):
            widget.config(state=state)

    def start_create(self, output):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)
        self.status_var.set("Creating archive...")

        threading.Thread(
            target=self.create_worker,
            args=(list(self.items), output),
            daemon=True
        ).start()

    def create_worker(self, items, output):
        try:
            def progress(
                current,
                total,
                file_index,
                file_count,
                label
            ):
                percent = (
                    100.0
                    if total == 0
                    else min(100.0, current * 100.0 / total)
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    label
                )

            result = create_archive(
                items,
                output,
                progress
            )

            self.root.after(
                0,
                self.finish_create,
                result,
                output
            )

        except Exception as e:
            self.root.after(
                0,
                self.finish_error,
                str(e)
            )

    def finish_create(self, result, output):
        self.progress_var.set(100)

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: {result['file_count']}\n"
            f"Directories: {result['dir_count']}\n"
            f"Original file data: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%\n"
            f"Optimization passes: {result['iterative_passes']}"
        )

        self.status_var.set(
            "Archive created successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            f"Archive created successfully.\n\n{output}"
        )

    def start_extract(self, archive_file, output_folder):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)
        self.status_var.set("Extracting archive...")

        threading.Thread(
            target=self.extract_worker,
            args=(archive_file, output_folder),
            daemon=True
        ).start()

    def extract_worker(self, archive_file, output_folder):
        try:
            def progress(
                current,
                total,
                file_index,
                file_count,
                label
            ):
                percent = (
                    100.0
                    if total == 0
                    else min(100.0, current * 100.0 / total)
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    label
                )

            result = extract_archive(
                archive_file,
                output_folder,
                progress
            )

            self.root.after(
                0,
                self.finish_extract,
                result,
                output_folder
            )

        except Exception as e:
            self.root.after(
                0,
                self.finish_error,
                str(e)
            )

    def finish_extract(self, result, output_folder):
        self.progress_var.set(100)

        self.result_var.set(
            f"Files extracted: {result['file_count']}\n"
            f"Directories restored: {result['dir_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"Iterative layers removed: {result['iterative_passes']}\n"
            f"Output: {output_folder}"
        )

        self.status_var.set(
            "Archive extracted successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            f"Archive extracted successfully.\n\n{output_folder}"
        )

    def finish_error(self, text):
        self.status_var.set("Error.")

        self.busy = False
        self.set_controls(True)

        messagebox.showerror(
            "Error",
            text
        )


def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
