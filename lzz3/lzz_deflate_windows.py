import os
import struct
import threading
import tkinter as tk
import zlib
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LZZ DEFLATE Archive Manager"
MAGIC = b"LZZ3"
VERSION = 3
METHOD_DEFLATE = 1
ENTRY_FILE = 0
ENTRY_DIR = 1
DEFAULT_LEVEL = 9
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

    entries.sort(key=lambda e: (0 if e["type"] == ENTRY_DIR else 1,
                                e["path"].count("/"), e["path"].lower()))
    return entries


def deflate_file_to_stream(source_path, destination, level=DEFAULT_LEVEL, progress_callback=None):
    source_path = Path(source_path)
    original_size = source_path.stat().st_size
    processed = 0
    crc = 0

    compressor = zlib.compressobj(
        level=level,
        method=zlib.DEFLATED,
        wbits=-15,          # raw DEFLATE / RFC 1951
        memLevel=9,
        strategy=zlib.Z_DEFAULT_STRATEGY,
    )

    payload_start = destination.tell()

    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(IO_BUFFER_SIZE)
            if not chunk:
                break
            processed += len(chunk)
            crc = zlib.crc32(chunk, crc)
            out = compressor.compress(chunk)
            if out:
                destination.write(out)
            if progress_callback and original_size:
                progress_callback(processed)

        out = compressor.flush(zlib.Z_FINISH)
        if out:
            destination.write(out)

    compressed_size = destination.tell() - payload_start
    return original_size, compressed_size, crc & 0xFFFFFFFF


def inflate_stream_to_file(source, compressed_size, original_size, output_path, progress_callback=None):
    output_path = Path(output_path)
    decompressor = zlib.decompressobj(wbits=-15)
    remaining = compressed_size
    restored = 0
    crc = 0

    with open(output_path, "wb") as destination:
        while remaining > 0:
            chunk = source.read(min(IO_BUFFER_SIZE, remaining))
            if not chunk:
                raise ValueError("Compressed payload ended unexpectedly.")
            remaining -= len(chunk)

            out = decompressor.decompress(chunk)
            if out:
                destination.write(out)
                restored += len(out)
                crc = zlib.crc32(out, crc)
                if progress_callback and original_size:
                    progress_callback(min(restored, original_size))

        out = decompressor.flush()
        if out:
            destination.write(out)
            restored += len(out)
            crc = zlib.crc32(out, crc)

    if not decompressor.eof:
        raise ValueError("Invalid or incomplete raw DEFLATE stream.")
    if restored != original_size:
        raise ValueError("Restored size does not match archive metadata.")

    return restored, crc & 0xFFFFFFFF


def create_archive(selected_items, archive_path, level=DEFAULT_LEVEL, progress_callback=None):
    if not 0 <= level <= 9:
        raise ValueError("DEFLATE level must be 0..9.")

    entries = collect_entries(selected_items)
    if not entries:
        raise ValueError("No files or folders selected.")

    file_entries = [e for e in entries if e["type"] == ENTRY_FILE]
    total_original_size = sum(e["source"].stat().st_size for e in file_entries)
    total_processed = 0
    current_file_index = 0
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with open(archive_path, "wb") as archive:
        archive.write(MAGIC)
        archive.write(struct.pack("<H", VERSION))
        archive.write(bytes([METHOD_DEFLATE, 0]))
        archive.write(struct.pack("<I", len(entries)))

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

            actual_original, compressed_size, crc = deflate_file_to_stream(
                source_path, archive, level=level, progress_callback=per_file_progress
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
        "compression_level": level,
    }


def _read_header(archive):
    if archive.read(4) != MAGIC:
        raise ValueError("This is not an LZZ3 DEFLATE archive.")
    version_raw = archive.read(2)
    method_raw = archive.read(1)
    reserved_raw = archive.read(1)
    count_raw = archive.read(4)
    if len(version_raw) != 2 or len(method_raw) != 1 or len(reserved_raw) != 1 or len(count_raw) != 4:
        raise ValueError("Archive header is damaged.")
    version = struct.unpack("<H", version_raw)[0]
    method = method_raw[0]
    entry_count = struct.unpack("<I", count_raw)[0]
    if version != VERSION:
        raise ValueError(f"Unsupported LZZ version: {version}")
    if method != METHOD_DEFLATE:
        raise ValueError(f"Unsupported compression method: {method}")
    return entry_count


def extract_archive(archive_path, output_folder, progress_callback=None):
    archive_path = Path(archive_path)
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    with open(archive_path, "rb") as archive:
        entry_count = _read_header(archive)
        entries_start = archive.tell()
        total_original_size = 0
        total_file_count = 0
        dir_count = 0

        # Pass 1: collect totals and validate structure.
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

            original_raw = archive.read(8)
            compressed_raw = archive.read(8)
            crc_raw = archive.read(4)
            if len(original_raw) != 8 or len(compressed_raw) != 8 or len(crc_raw) != 4:
                raise ValueError("Archive file metadata is incomplete.")

            original_size = struct.unpack("<Q", original_raw)[0]
            compressed_size = struct.unpack("<Q", compressed_raw)[0]
            total_original_size += original_size
            total_file_count += 1
            archive.seek(compressed_size, os.SEEK_CUR)

        # Pass 2: extract.
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
            original_raw = archive.read(8)
            compressed_raw = archive.read(8)
            crc_raw = archive.read(4)
            if len(original_raw) != 8 or len(compressed_raw) != 8 or len(crc_raw) != 4:
                raise ValueError("Archive file metadata is incomplete.")

            original_size = struct.unpack("<Q", original_raw)[0]
            compressed_size = struct.unpack("<Q", compressed_raw)[0]
            expected_crc = struct.unpack("<I", crc_raw)[0]
            target.parent.mkdir(parents=True, exist_ok=True)

            def per_file_progress(current, label=archive_path_text, idx=current_file_index):
                if progress_callback:
                    progress_callback(total_restored + current, total_original_size,
                                      idx, total_file_count, label)

            restored, actual_crc = inflate_stream_to_file(
                archive, compressed_size, original_size, target,
                progress_callback=per_file_progress
            )

            if actual_crc != expected_crc:
                raise ValueError(f"CRC32 verification failed:\n{archive_path_text}")

            total_restored += restored

    return {
        "entry_count": entry_count,
        "file_count": total_file_count,
        "dir_count": dir_count,
        "restored_size": total_restored,
    }


class LZZApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(860, 650)
        self.items = []
        self.busy = False
        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="RAW DEFLATE (RFC 1951) archive for Windows compression and SAMV71-friendly streaming decompression."
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

        settings = ttk.LabelFrame(outer, text="DEFLATE Settings", padding=10)
        settings.pack(fill="x", pady=(14, 0))
        ttk.Label(settings, text="Compression level:").pack(side="left")
        self.level_var = tk.StringVar(value="9 - Maximum")
        self.level_combo = ttk.Combobox(
            settings,
            textvariable=self.level_var,
            state="readonly",
            width=18,
            values=["1 - Fastest", "3 - Fast", "6 - Balanced", "9 - Maximum"]
        )
        self.level_combo.pack(side="left", padx=(10, 0))
        ttk.Label(
            settings,
            text="Level changes Windows compression time/ratio; SAMV71 decoding format stays identical."
        ).pack(side="left", padx=(18, 0))

        actions = ttk.Frame(outer)
        actions.pack(pady=16)
        self.create_btn = ttk.Button(actions, text="CREATE .LZZ ARCHIVE", width=27, command=self.choose_output)
        self.create_btn.grid(row=0, column=0, padx=8)
        self.extract_btn = ttk.Button(actions, text="EXTRACT .LZZ ARCHIVE", width=27, command=self.choose_extract)
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
            "Selected items: 0\nFiles: -\nDirectories: -\nOriginal size: -\nArchive size: -\nSaving: -"
        ))
        ttk.Label(result_box, textvariable=self.result_var, justify="left").pack(anchor="w")

        ttk.Label(
            outer,
            text="Format: LZZ3 | Compression: RAW DEFLATE | Integrity: CRC32 | Streaming I/O: 64 KB on Windows",
            font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(12, 0))

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
            prefix = "[DIR] " if path.is_dir() else "[FILE] "
            self.listbox.insert(tk.END, prefix + str(path))
        self.result_var.set(
            f"Selected items: {len(self.items)}\nFiles: -\nDirectories: -\nOriginal size: -\nArchive size: -\nSaving: -"
        )
        self.status_var.set(f"{len(self.items)} item(s) selected." if self.items else "Ready")

    def get_level(self):
        try:
            return int(self.level_var.get().split()[0])
        except Exception:
            return DEFAULT_LEVEL

    def choose_output(self):
        if not self.items:
            messagebox.showwarning("Warning", "Add at least one file or folder.")
            return
        output = filedialog.asksaveasfilename(
            title="Save LZZ3 archive",
            initialfile="archive.lzz",
            defaultextension=".lzz",
            filetypes=[("LZZ Archive", "*.lzz"), ("All files", "*.*")]
        )
        if output:
            self.start_create(output)

    def start_create(self, output):
        self.busy = True
        self.set_controls(False)
        self.progress_var.set(0)
        self.status_var.set("Creating DEFLATE archive...")
        threading.Thread(
            target=self.create_worker,
            args=(list(self.items), output, self.get_level()),
            daemon=True
        ).start()

    def create_worker(self, items, output, level):
        try:
            def progress(current, total, file_index, file_count, label):
                percent = 100.0 if total == 0 else min(100.0, current * 100.0 / total)
                self.root.after(0, self.progress_var.set, percent)
                self.root.after(0, self.status_var.set,
                                f"Compressing {file_index}/{file_count}: {label}")

            result = create_archive(items, output, level=level, progress_callback=progress)
            self.root.after(0, self.finish_create, result, output)
        except Exception as error:
            self.root.after(0, self.finish_error, str(error))

    def finish_create(self, result, output):
        self.progress_var.set(100)
        self.result_var.set(
            f"Selected items: {len(self.items)}\n"
            f"Files: {result['file_count']}\n"
            f"Directories: {result['dir_count']}\n"
            f"Original size: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%\n"
            f"DEFLATE level: {result['compression_level']}"
        )
        self.status_var.set("Archive created successfully.")
        self.busy = False
        self.set_controls(True)
        messagebox.showinfo(
            "Completed",
            f"LZZ3 DEFLATE archive created.\n\n{output}\n\n"
            f"Original: {human_size(result['original_size'])}\n"
            f"Archive: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%"
        )

    def choose_extract(self):
        if self.busy:
            return
        archive_file = filedialog.askopenfilename(
            title="Select LZZ3 archive",
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
        self.status_var.set("Extracting DEFLATE archive...")
        threading.Thread(
            target=self.extract_worker,
            args=(archive_file, output_folder),
            daemon=True
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
        self.result_var.set(
            f"Files extracted: {result['file_count']}\n"
            f"Directories restored: {result['dir_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"CRC32: Verified\nOutput: {output_folder}"
        )
        self.status_var.set("Archive extracted successfully.")
        self.busy = False
        self.set_controls(True)
        messagebox.showinfo("Completed", f"Archive extracted successfully.\n\n{output_folder}")

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
        self.level_combo.config(state="readonly" if enabled else "disabled")


def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
