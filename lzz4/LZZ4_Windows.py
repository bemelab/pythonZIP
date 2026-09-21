import os
import struct
import threading
import tkinter as tk
import zlib
import lzma
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LZZ4 Archive Manager"

MAGIC_V4 = b"LZZ4"
VERSION_V4 = 4

# Backward compatibility with previous DEFLATE-only archives.
MAGIC_V3 = b"LZZ3"
VERSION_V3 = 3

METHOD_DEFLATE = 1
METHOD_LZMA2 = 2

PROFILE_FAST = 1
PROFILE_BALANCED = 2
PROFILE_MAXIMUM = 3

ENTRY_FILE = 0
ENTRY_DIR = 1

IO_BUFFER_SIZE = 64 * 1024


def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024


def normalize_archive_path(path_text):
    text = str(path_text).replace("\\", "/").lstrip("/")
    parts = []
    for part in text.split("/"):
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
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"Unsafe archive path: {archive_path}")
    return candidate


def method_name(method):
    if method == METHOD_DEFLATE:
        return "DEFLATE"
    if method == METHOD_LZMA2:
        return "LZMA2"
    return f"Unknown ({method})"


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


def lzma_dictionary_size(profile, max_file_size):
    if profile == PROFILE_FAST:
        cap = 1 * 1024 * 1024
    elif profile == PROFILE_BALANCED:
        cap = 8 * 1024 * 1024
    else:
        cap = 64 * 1024 * 1024

    needed = max(64 * 1024, next_power_of_two(max(1, max_file_size)))
    return min(cap, needed)


def deflate_level(profile):
    if profile == PROFILE_FAST:
        return 1
    if profile == PROFILE_BALANCED:
        return 6
    return 9


def collect_entries(selected_items):
    entries = []
    seen = set()

    def add_dir(archive_path):
        archive_path = normalize_archive_path(archive_path)
        key = ("D", archive_path.lower())
        if key not in seen:
            seen.add(key)
            entries.append({"type": ENTRY_DIR, "path": archive_path})

    def add_file(source_path, archive_path):
        source_path = Path(source_path)
        archive_path = normalize_archive_path(archive_path)
        key = ("F", archive_path.lower())
        if key in seen:
            raise ValueError(f"Duplicate archive path:\n{archive_path}")
        seen.add(key)
        entries.append({"type": ENTRY_FILE, "path": archive_path, "source": source_path})

    for item in selected_items:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(f"Input path not found:\n{path}")

        if path.is_file():
            add_file(path, path.name)
            continue

        if path.is_dir():
            root_name = path.name
            add_dir(root_name)

            for current_root, dir_names, file_names in os.walk(path):
                current_root = Path(current_root)
                rel_root = current_root.relative_to(path)

                if rel_root.parts:
                    add_dir("/".join((root_name,) + rel_root.parts))

                for directory in dir_names:
                    rel_dir = (current_root / directory).relative_to(path)
                    add_dir("/".join((root_name,) + rel_dir.parts))

                for file_name in file_names:
                    source_file = current_root / file_name
                    rel_file = source_file.relative_to(path)
                    add_file(source_file, "/".join((root_name,) + rel_file.parts))

    entries.sort(key=lambda e: (
        0 if e["type"] == ENTRY_DIR else 1,
        e["path"].count("/"),
        e["path"].lower(),
    ))
    return entries


def make_lzma2_filter(profile, dict_size):
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
            "nice_len": 64,
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


def compress_file_to_stream(source_path, destination, method, profile, method_param,
                            progress_callback=None):
    source_path = Path(source_path)
    original_size = source_path.stat().st_size
    processed = 0
    crc = 0
    payload_start = destination.tell()

    if method == METHOD_DEFLATE:
        compressor = zlib.compressobj(
            level=deflate_level(profile),
            method=zlib.DEFLATED,
            wbits=-15,
            memLevel=9,
            strategy=zlib.Z_DEFAULT_STRATEGY,
        )
        feed = compressor.compress
        finish = lambda: compressor.flush(zlib.Z_FINISH)

    elif method == METHOD_LZMA2:
        compressor = lzma.LZMACompressor(
            format=lzma.FORMAT_RAW,
            filters=[make_lzma2_filter(profile, method_param)],
        )
        feed = compressor.compress
        finish = compressor.flush

    else:
        raise ValueError(f"Unsupported compression method: {method}")

    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(IO_BUFFER_SIZE)
            if not chunk:
                break
            processed += len(chunk)
            crc = zlib.crc32(chunk, crc)
            out = feed(chunk)
            if out:
                destination.write(out)
            if progress_callback and original_size:
                progress_callback(processed)

        out = finish()
        if out:
            destination.write(out)

    compressed_size = destination.tell() - payload_start
    return original_size, compressed_size, crc & 0xFFFFFFFF


def decompress_stream_to_file(source, compressed_size, original_size, output_path,
                              method, method_param, progress_callback=None):
    output_path = Path(output_path)

    if method == METHOD_DEFLATE:
        decompressor = zlib.decompressobj(wbits=-15)
        feed = decompressor.decompress
        finish = decompressor.flush
        is_complete = lambda: decompressor.eof

    elif method == METHOD_LZMA2:
        decompressor = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[{"id": lzma.FILTER_LZMA2, "dict_size": method_param}],
        )
        feed = decompressor.decompress
        finish = lambda: b""
        is_complete = lambda: decompressor.eof

    else:
        raise ValueError(f"Unsupported compression method: {method}")

    remaining = compressed_size
    restored = 0
    crc = 0

    with open(output_path, "wb") as destination:
        while remaining > 0:
            chunk = source.read(min(IO_BUFFER_SIZE, remaining))
            if not chunk:
                raise ValueError("Compressed payload ended unexpectedly.")
            remaining -= len(chunk)

            out = feed(chunk)
            if out:
                destination.write(out)
                restored += len(out)
                crc = zlib.crc32(out, crc)
                if progress_callback and original_size:
                    progress_callback(min(restored, original_size))

        tail = finish()
        if tail:
            destination.write(tail)
            restored += len(tail)
            crc = zlib.crc32(tail, crc)

    if not is_complete():
        raise ValueError("Invalid or incomplete compressed stream.")
    if restored != original_size:
        raise ValueError("Restored size does not match archive metadata.")

    return restored, crc & 0xFFFFFFFF


def write_v4_header(archive, method, profile, method_param, entry_count):
    archive.write(MAGIC_V4)
    archive.write(struct.pack("<H", VERSION_V4))
    archive.write(bytes([method]))
    archive.write(bytes([profile]))
    archive.write(struct.pack("<I", method_param))
    archive.write(struct.pack("<I", entry_count))


def read_archive_header(archive):
    magic = archive.read(4)

    if magic == MAGIC_V4:
        version_raw = archive.read(2)
        method_raw = archive.read(1)
        profile_raw = archive.read(1)
        param_raw = archive.read(4)
        count_raw = archive.read(4)

        if (len(version_raw) != 2 or len(method_raw) != 1 or
                len(profile_raw) != 1 or len(param_raw) != 4 or len(count_raw) != 4):
            raise ValueError("LZZ4 header is incomplete.")

        version = struct.unpack("<H", version_raw)[0]
        if version != VERSION_V4:
            raise ValueError(f"Unsupported LZZ4 version: {version}")

        return {
            "format": 4,
            "method": method_raw[0],
            "profile": profile_raw[0],
            "method_param": struct.unpack("<I", param_raw)[0],
            "entry_count": struct.unpack("<I", count_raw)[0],
        }

    if magic == MAGIC_V3:
        version_raw = archive.read(2)
        method_raw = archive.read(1)
        reserved_raw = archive.read(1)
        count_raw = archive.read(4)

        if (len(version_raw) != 2 or len(method_raw) != 1 or
                len(reserved_raw) != 1 or len(count_raw) != 4):
            raise ValueError("LZZ3 header is incomplete.")

        version = struct.unpack("<H", version_raw)[0]
        if version != VERSION_V3:
            raise ValueError(f"Unsupported LZZ3 version: {version}")
        if method_raw[0] != METHOD_DEFLATE:
            raise ValueError("Unsupported LZZ3 compression method.")

        return {
            "format": 3,
            "method": METHOD_DEFLATE,
            "profile": PROFILE_MAXIMUM,
            "method_param": 0,
            "entry_count": struct.unpack("<I", count_raw)[0],
        }

    raise ValueError("This is not a valid LZZ3/LZZ4 archive.")


def create_archive(selected_items, archive_path, method=METHOD_LZMA2,
                   profile=PROFILE_MAXIMUM, progress_callback=None):
    if method not in (METHOD_DEFLATE, METHOD_LZMA2):
        raise ValueError("Unsupported compression method.")
    if profile not in (PROFILE_FAST, PROFILE_BALANCED, PROFILE_MAXIMUM):
        raise ValueError("Unsupported compression profile.")

    entries = collect_entries(selected_items)
    if not entries:
        raise ValueError("No files or folders selected.")

    file_entries = [e for e in entries if e["type"] == ENTRY_FILE]
    total_original_size = sum(e["source"].stat().st_size for e in file_entries)
    max_file_size = max((e["source"].stat().st_size for e in file_entries), default=0)
    method_param = lzma_dictionary_size(profile, max_file_size) if method == METHOD_LZMA2 else 0

    total_processed = 0
    current_file_index = 0
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with open(archive_path, "wb") as archive:
        write_v4_header(archive, method, profile, method_param, len(entries))

        for entry in entries:
            archive.write(bytes([entry["type"]]))
            path_bytes = entry["path"].encode("utf-8")
            archive.write(struct.pack("<I", len(path_bytes)))
            archive.write(path_bytes)

            if entry["type"] == ENTRY_DIR:
                continue

            current_file_index += 1
            source_path = entry["source"]
            original_size = source_path.stat().st_size
            archive.write(struct.pack("<Q", original_size))

            compressed_size_pos = archive.tell()
            archive.write(struct.pack("<Q", 0))
            crc_pos = archive.tell()
            archive.write(struct.pack("<I", 0))

            def per_file_progress(current, entry_path=entry["path"], idx=current_file_index):
                if progress_callback:
                    progress_callback(total_processed + current, total_original_size,
                                      idx, len(file_entries), entry_path)

            actual_original, compressed_size, crc = compress_file_to_stream(
                source_path, archive, method, profile, method_param,
                progress_callback=per_file_progress
            )

            if actual_original != original_size:
                raise ValueError(f"File changed while archiving:\n{source_path}")

            end_pos = archive.tell()
            archive.seek(compressed_size_pos)
            archive.write(struct.pack("<Q", compressed_size))
            archive.seek(crc_pos)
            archive.write(struct.pack("<I", crc))
            archive.seek(end_pos)
            total_processed += original_size

    archive_size = archive_path.stat().st_size
    saving = 0.0 if total_original_size == 0 else (1.0 - archive_size / total_original_size) * 100.0

    return {
        "entry_count": len(entries),
        "file_count": len(file_entries),
        "dir_count": len(entries) - len(file_entries),
        "original_size": total_original_size,
        "archive_size": archive_size,
        "saving": saving,
        "method": method,
        "profile": profile,
        "method_param": method_param,
    }


def extract_archive(archive_path, output_folder, progress_callback=None):
    archive_path = Path(archive_path)
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(archive_path, "rb") as archive:
        header = read_archive_header(archive)
        method = header["method"]
        method_param = header["method_param"]
        entry_count = header["entry_count"]
        entries_start = archive.tell()

        total_original_size = 0
        total_file_count = 0
        dir_count = 0

        # Pass 1: totals and structural validation.
        for _ in range(entry_count):
            type_raw = archive.read(1)
            name_len_raw = archive.read(4)
            if len(type_raw) != 1 or len(name_len_raw) != 4:
                raise ValueError("Archive entry is damaged.")

            entry_type = type_raw[0]
            name_len = struct.unpack("<I", name_len_raw)[0]
            if name_len > 1024 * 1024:
                raise ValueError("Archive path length is unreasonable.")
            if len(archive.read(name_len)) != name_len:
                raise ValueError("Archive path is incomplete.")

            if entry_type == ENTRY_DIR:
                dir_count += 1
                continue
            if entry_type != ENTRY_FILE:
                raise ValueError(f"Unknown archive entry type: {entry_type}")

            metadata = archive.read(20)
            if len(metadata) != 20:
                raise ValueError("Archive file metadata is incomplete.")

            original_size = struct.unpack("<Q", metadata[0:8])[0]
            compressed_size = struct.unpack("<Q", metadata[8:16])[0]
            total_original_size += original_size
            total_file_count += 1
            archive.seek(compressed_size, os.SEEK_CUR)

        # Pass 2: extraction.
        archive.seek(entries_start)
        total_restored = 0
        current_file_index = 0

        for _ in range(entry_count):
            entry_type_raw = archive.read(1)
            name_len_raw = archive.read(4)
            if len(entry_type_raw) != 1 or len(name_len_raw) != 4:
                raise ValueError("Archive entry is damaged.")

            entry_type = entry_type_raw[0]
            name_len = struct.unpack("<I", name_len_raw)[0]
            path_bytes = archive.read(name_len)
            if len(path_bytes) != name_len:
                raise ValueError("Archive path is incomplete.")

            try:
                archive_path_text = path_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Archive contains an invalid UTF-8 path.")

            target = safe_output_path(output_folder, archive_path_text)

            if entry_type == ENTRY_DIR:
                target.mkdir(parents=True, exist_ok=True)
                continue
            if entry_type != ENTRY_FILE:
                raise ValueError(f"Unknown archive entry type: {entry_type}")

            current_file_index += 1
            metadata = archive.read(20)
            if len(metadata) != 20:
                raise ValueError("Archive file metadata is incomplete.")

            original_size = struct.unpack("<Q", metadata[0:8])[0]
            compressed_size = struct.unpack("<Q", metadata[8:16])[0]
            expected_crc = struct.unpack("<I", metadata[16:20])[0]
            target.parent.mkdir(parents=True, exist_ok=True)

            def per_file_progress(current, label=archive_path_text, idx=current_file_index):
                if progress_callback:
                    progress_callback(total_restored + current, total_original_size,
                                      idx, total_file_count, label)

            restored, actual_crc = decompress_stream_to_file(
                archive, compressed_size, original_size, target,
                method, method_param, progress_callback=per_file_progress
            )

            if actual_crc != expected_crc:
                raise ValueError(f"CRC32 verification failed:\n{archive_path_text}")

            total_restored += restored

    return {
        "format": header["format"],
        "method": header["method"],
        "profile": header["profile"],
        "method_param": header["method_param"],
        "entry_count": entry_count,
        "file_count": total_file_count,
        "dir_count": dir_count,
        "restored_size": total_restored,
    }


class LZZApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x780")
        self.root.minsize(880, 680)
        self.items = []
        self.busy = False
        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Create one .lzz archive from files/folders. Use DEFLATE for speed or LZMA2 for maximum compression."
        ).pack(anchor="w", pady=(4, 16))

        content_box = ttk.LabelFrame(outer, text="Archive Contents", padding=10)
        content_box.pack(fill="both", expand=True)

        list_frame = ttk.Frame(content_box)
        list_frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED, font=("Segoe UI", 10))
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=scrollbar.set)

        controls = ttk.Frame(content_box)
        controls.pack(fill="x", pady=(10, 0))
        self.add_files_btn = ttk.Button(controls, text="Add Files...", command=self.add_files)
        self.add_files_btn.pack(side="left")
        self.add_folder_btn = ttk.Button(controls, text="Add Folder...", command=self.add_folder)
        self.add_folder_btn.pack(side="left", padx=(8, 0))
        self.remove_btn = ttk.Button(controls, text="Remove Selected", command=self.remove_selected)
        self.remove_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = ttk.Button(controls, text="Clear", command=self.clear_items)
        self.clear_btn.pack(side="left", padx=(8, 0))

        settings = ttk.LabelFrame(outer, text="Compression Settings", padding=10)
        settings.pack(fill="x", pady=(14, 0))

        ttk.Label(settings, text="Method:").grid(row=0, column=0, sticky="w")
        self.method_var = tk.StringVar(value="LZMA2")
        self.method_combo = ttk.Combobox(
            settings, textvariable=self.method_var, state="readonly", width=16,
            values=["DEFLATE", "LZMA2"]
        )
        self.method_combo.grid(row=0, column=1, padx=(10, 24), sticky="w")

        ttk.Label(settings, text="Profile:").grid(row=0, column=2, sticky="w")
        self.profile_var = tk.StringVar(value="Maximum")
        self.profile_combo = ttk.Combobox(
            settings, textvariable=self.profile_var, state="readonly", width=16,
            values=["Fast", "Balanced", "Maximum"]
        )
        self.profile_combo.grid(row=0, column=3, padx=(10, 24), sticky="w")

        self.setting_info_var = tk.StringVar(
            value="LZMA2 Maximum: strongest compression; dictionary scales automatically up to 64 MB."
        )
        ttk.Label(settings, textvariable=self.setting_info_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        self.method_combo.bind("<<ComboboxSelected>>", self.update_setting_info)
        self.profile_combo.bind("<<ComboboxSelected>>", self.update_setting_info)

        actions = ttk.Frame(outer)
        actions.pack(pady=16)
        self.create_btn = ttk.Button(actions, text="CREATE .LZZ ARCHIVE", width=28, command=self.choose_output)
        self.create_btn.grid(row=0, column=0, padx=8)
        self.extract_btn = ttk.Button(actions, text="EXTRACT .LZZ ARCHIVE", width=28, command=self.choose_extract)
        self.extract_btn.grid(row=0, column=1, padx=8)

        progress_box = ttk.LabelFrame(outer, text="Progress", padding=10)
        progress_box.pack(fill="x")
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(progress_box, maximum=100, variable=self.progress_var).pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(progress_box, textvariable=self.status_var).pack(anchor="w", pady=(7, 0))

        result_box = ttk.LabelFrame(outer, text="Result", padding=10)
        result_box.pack(fill="x", pady=(14, 0))
        self.result_var = tk.StringVar(value=(
            "Selected items: 0\nFiles: -\nDirectories: -\nOriginal size: -\n"
            "Archive size: -\nSaving: -\nMethod: -"
        ))
        ttk.Label(result_box, textvariable=self.result_var, justify="left").pack(anchor="w")

        ttk.Label(
            outer,
            text="LZZ4 | DEFLATE or RAW LZMA2 | CRC32 | Can extract previous LZZ3 DEFLATE archives",
            font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(12, 0))

    def update_setting_info(self, event=None):
        method = self.method_var.get()
        profile = self.profile_var.get()
        if method == "DEFLATE":
            level = {"Fast": 1, "Balanced": 6, "Maximum": 9}.get(profile, 9)
            self.setting_info_var.set(
                f"DEFLATE {profile}: zlib level {level}. Faster, but usually larger than LZMA2."
            )
        else:
            cap = {"Fast": "1 MB", "Balanced": "8 MB", "Maximum": "64 MB"}.get(profile, "64 MB")
            self.setting_info_var.set(
                f"LZMA2 {profile}: dictionary scales with the largest file, capped at {cap}."
            )

    def add_files(self):
        if self.busy:
            return
        files = filedialog.askopenfilenames(title="Select files", filetypes=[("All files", "*.*")])
        for file_path in files:
            resolved = str(Path(file_path).resolve())
            if resolved not in self.items:
                self.items.append(resolved)
        self.refresh_list()

    def add_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(title="Select folder")
        if folder:
            resolved = str(Path(folder).resolve())
            if resolved not in self.items:
                self.items.append(resolved)
        self.refresh_list()

    def remove_selected(self):
        if self.busy:
            return
        for index in reversed(list(self.listbox.curselection())):
            del self.items[index]
        self.refresh_list()

    def clear_items(self):
        if not self.busy:
            self.items.clear()
            self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        for item in self.items:
            path = Path(item)
            self.listbox.insert(tk.END, ("[DIR] " if path.is_dir() else "[FILE] ") + str(path))
        self.result_var.set(
            f"Selected items: {len(self.items)}\nFiles: -\nDirectories: -\nOriginal size: -\n"
            f"Archive size: -\nSaving: -\nMethod: -"
        )
        self.status_var.set(f"{len(self.items)} item(s) selected." if self.items else "Ready")

    def get_method(self):
        return METHOD_LZMA2 if self.method_var.get() == "LZMA2" else METHOD_DEFLATE

    def get_profile(self):
        if self.profile_var.get() == "Fast":
            return PROFILE_FAST
        if self.profile_var.get() == "Balanced":
            return PROFILE_BALANCED
        return PROFILE_MAXIMUM

    def choose_output(self):
        if not self.items:
            messagebox.showwarning("Warning", "Add at least one file or folder.")
            return
        output = filedialog.asksaveasfilename(
            title="Save LZZ4 archive", initialfile="archive.lzz", defaultextension=".lzz",
            filetypes=[("LZZ Archive", "*.lzz"), ("All files", "*.*")]
        )
        if output:
            self.start_create(output)

    def start_create(self, output):
        self.busy = True
        self.set_controls(False)
        self.progress_var.set(0)
        method = self.get_method()
        profile = self.get_profile()
        self.status_var.set(f"Creating {method_name(method)} archive...")
        threading.Thread(
            target=self.create_worker,
            args=(list(self.items), output, method, profile), daemon=True
        ).start()

    def create_worker(self, items, output, method, profile):
        try:
            def progress(current, total, file_index, file_count, label):
                percent = 100.0 if total == 0 else min(100.0, current * 100.0 / total)
                self.root.after(0, self.progress_var.set, percent)
                self.root.after(0, self.status_var.set,
                                f"Compressing {file_index}/{file_count}: {label}")

            result = create_archive(
                items, output, method=method, profile=profile, progress_callback=progress
            )
            self.root.after(0, self.finish_create, result, output)
        except Exception as error:
            self.root.after(0, self.finish_error, str(error))

    def finish_create(self, result, output):
        self.progress_var.set(100)
        extra = ""
        if result["method"] == METHOD_LZMA2:
            extra = f"\nLZMA2 dictionary: {human_size(result['method_param'])}"

        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: {result['file_count']}\n"
            f"Directories: {result['dir_count']}\n"
            f"Original size: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%\n"
            f"Method: {method_name(result['method'])} / {profile_name(result['profile'])}"
            f"{extra}"
        )
        self.status_var.set("Archive created successfully.")
        self.busy = False
        self.set_controls(True)
        messagebox.showinfo(
            "Completed",
            f"LZZ4 archive created.\n\n{output}\n\n"
            f"Method: {method_name(result['method'])} / {profile_name(result['profile'])}\n"
            f"Original: {human_size(result['original_size'])}\n"
            f"Archive: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%{extra}"
        )

    def choose_extract(self):
        if self.busy:
            return
        archive_file = filedialog.askopenfilename(
            title="Select LZZ archive",
            filetypes=[("LZZ Archive", "*.lzz"), ("All files", "*.*")]
        )
        if not archive_file:
            return
        output_folder = filedialog.askdirectory(title="Select extraction folder")
        if output_folder:
            self.start_extract(archive_file, output_folder)

    def start_extract(self, archive_file, output_folder):
        self.busy = True
        self.set_controls(False)
        self.progress_var.set(0)
        self.status_var.set("Extracting archive...")
        threading.Thread(
            target=self.extract_worker,
            args=(archive_file, output_folder), daemon=True
        ).start()

    def extract_worker(self, archive_file, output_folder):
        try:
            def progress(current, total, file_index, file_count, label):
                percent = 100.0 if total == 0 else min(100.0, current * 100.0 / total)
                self.root.after(0, self.progress_var.set, percent)
                self.root.after(0, self.status_var.set,
                                f"Extracting {file_index}/{file_count}: {label}")

            result = extract_archive(archive_file, output_folder, progress_callback=progress)
            self.root.after(0, self.finish_extract, result, output_folder)
        except Exception as error:
            self.root.after(0, self.finish_error, str(error))

    def finish_extract(self, result, output_folder):
        self.progress_var.set(100)
        extra = ""
        if result["method"] == METHOD_LZMA2:
            extra = f"\nLZMA2 dictionary: {human_size(result['method_param'])}"
        self.result_var.set(
            f"Files extracted: {result['file_count']}\n"
            f"Directories restored: {result['dir_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"Method: {method_name(result['method'])}\n"
            f"CRC32: Verified\nOutput: {output_folder}{extra}"
        )
        self.status_var.set("Archive extracted successfully.")
        self.busy = False
        self.set_controls(True)
        messagebox.showinfo(
            "Completed",
            f"Archive extracted successfully.\n\n"
            f"Method: {method_name(result['method'])}\n"
            f"Files: {result['file_count']}\n"
            f"Restored: {human_size(result['restored_size'])}\n{output_folder}"
        )

    def finish_error(self, error_message):
        self.status_var.set("Error.")
        self.busy = False
        self.set_controls(True)
        messagebox.showerror("Error", error_message)

    def set_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (
            self.add_files_btn, self.add_folder_btn, self.remove_btn,
            self.clear_btn, self.create_btn, self.extract_btn
        ):
            widget.config(state=state)
        combo_state = "readonly" if enabled else "disabled"
        self.method_combo.config(state=combo_state)
        self.profile_combo.config(state=combo_state)


def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
