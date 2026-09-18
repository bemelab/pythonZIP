import os
import struct
import threading
import tkinter as tk
import zlib
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LZZ Multi-File Archiver"

# Archive format:
#   4 bytes  : MAGIC = LZZ1
#   2 bytes  : VERSION = 1
#   4 bytes  : file count
#
# For every file:
#   4 bytes  : UTF-8 file name length
#   N bytes  : UTF-8 file name
#   8 bytes  : original size
#   8 bytes  : compressed size
#   4 bytes  : CRC32 of original file
#   N bytes  : LZSS-compressed payload

MAGIC = b"LZZ1"
VERSION = 1

WINDOW_SIZE = 4095
MIN_MATCH = 3
MAX_MATCH = 18


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

    max_len = min(
        MAX_MATCH,
        len(lookahead),
        len(window)
    )

    for length in range(max_len, MIN_MATCH - 1, -1):
        sequence = bytes(lookahead[:length])
        match_pos = window.rfind(sequence)

        if match_pos != -1:
            offset = len(window) - match_pos

            if 1 <= offset <= WINDOW_SIZE:
                return offset, length

    return 0, 0


def safe_extract_name(name):
    # This archive intentionally stores file names only, not absolute paths.
    # Still sanitize anything suspicious in case an external/corrupt archive
    # contains path separators.
    name = name.replace("\\", "/")
    name = name.split("/")[-1].strip()

    if not name:
        raise ValueError("Archive contains an invalid empty file name.")

    if name in {".", ".."}:
        raise ValueError("Archive contains an invalid file name.")

    return name


def unique_output_path(folder, filename):
    folder = Path(folder)
    candidate = folder / filename

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    number = 2

    while True:
        candidate = folder / f"{stem} ({number}){suffix}"

        if not candidate.exists():
            return candidate

        number += 1


def unique_archive_names(file_paths):
    used = set()
    result = []

    for file_path in file_paths:
        path = Path(file_path)
        original = path.name

        stem = path.stem
        suffix = path.suffix

        name = original
        number = 2

        while name.lower() in used:
            name = f"{stem} ({number}){suffix}"
            number += 1

        used.add(name.lower())
        result.append(name)

    return result


# ============================================================
# Bounded reader used during decompression
# ============================================================

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
# LZSS stream compressor
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

            offset, length = find_longest_match(
                window,
                lookahead
            )

            # Match token
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

                consumed = bytes(
                    lookahead[:length]
                )

                crc = zlib.crc32(consumed, crc)

                del lookahead[:length]

                add_to_window(
                    window,
                    consumed
                )

                processed += length

            # Literal token
            else:
                value = lookahead[0]
                consumed = bytes([value])

                tokens.append(value)

                crc = zlib.crc32(consumed, crc)

                del lookahead[0]

                add_to_window(
                    window,
                    consumed
                )

                processed += 1

            fill_lookahead(
                source,
                lookahead
            )

        destination.write(
            bytes([flags])
        )

        destination.write(tokens)

        if progress_callback and original_size > 0:
            progress_callback(processed)

    return processed, crc & 0xFFFFFFFF


# ============================================================
# LZSS stream decompressor
# ============================================================

def decompress_stream(
    source,
    destination,
    original_size,
    progress_callback=None
):
    history = bytearray()
    restored = 0
    crc = 0

    while restored < original_size:
        flag_data = source.read(1)

        if not flag_data:
            raise ValueError(
                "Compressed payload ended unexpectedly."
            )

        flags = flag_data[0]

        for bit in range(8):
            if restored >= original_size:
                break

            is_match = (
                flags >> bit
            ) & 1

            # Literal token
            if not is_match:
                raw = source.read(1)

                if not raw:
                    raise ValueError(
                        "Missing literal byte in compressed payload."
                    )

                value = raw[0]
                data = bytes([value])

                destination.write(data)
                crc = zlib.crc32(data, crc)

                add_to_window(
                    history,
                    data
                )

                restored += 1

            # Match token
            else:
                raw = source.read(2)

                if len(raw) != 2:
                    raise ValueError(
                        "Missing LZSS match data."
                    )

                encoded = struct.unpack(
                    ">H",
                    raw
                )[0]

                offset = (
                    encoded >> 4
                )

                length = (
                    encoded & 0x0F
                ) + MIN_MATCH

                if offset == 0:
                    raise ValueError(
                        "Invalid LZSS offset."
                    )

                if offset > len(history):
                    raise ValueError(
                        "LZSS offset exceeds the history window."
                    )

                for _ in range(length):
                    if restored >= original_size:
                        break

                    value = history[
                        len(history) - offset
                    ]

                    data = bytes([value])

                    destination.write(data)
                    crc = zlib.crc32(data, crc)

                    add_to_window(
                        history,
                        data
                    )

                    restored += 1

            if progress_callback and original_size > 0:
                progress_callback(restored)

    return restored, crc & 0xFFFFFFFF


# ============================================================
# Create one .lzz archive from one or many files
# ============================================================

def create_archive(file_paths, archive_path, progress_callback=None):
    if not file_paths:
        raise ValueError("No input files were selected.")

    paths = [Path(p) for p in file_paths]

    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(
                f"Input file not found:\n{path}"
            )

    archive_names = unique_archive_names(paths)

    total_original_size = sum(
        path.stat().st_size
        for path in paths
    )

    total_processed = 0

    with open(archive_path, "wb") as archive:
        archive.write(MAGIC)
        archive.write(
            struct.pack("<H", VERSION)
        )
        archive.write(
            struct.pack("<I", len(paths))
        )

        file_results = []

        for index, (path, stored_name) in enumerate(
            zip(paths, archive_names),
            start=1
        ):
            name_bytes = stored_name.encode("utf-8")
            original_size = path.stat().st_size

            archive.write(
                struct.pack("<I", len(name_bytes))
            )
            archive.write(name_bytes)
            archive.write(
                struct.pack("<Q", original_size)
            )

            # Reserve space for compressed size and CRC32.
            compressed_size_position = archive.tell()
            archive.write(
                struct.pack("<Q", 0)
            )

            crc_position = archive.tell()
            archive.write(
                struct.pack("<I", 0)
            )

            payload_start = archive.tell()

            last_file_progress = 0

            def file_progress(current):
                nonlocal last_file_progress
                delta = current - last_file_progress
                last_file_progress = current

                if progress_callback:
                    progress_callback(
                        total_processed + current,
                        total_original_size,
                        index,
                        len(paths),
                        stored_name
                    )

            with open(path, "rb") as source:
                processed, crc = compress_stream(
                    source,
                    archive,
                    original_size,
                    file_progress
                )

            payload_end = archive.tell()
            compressed_size = (
                payload_end - payload_start
            )

            # Fill reserved fields.
            archive.seek(
                compressed_size_position
            )
            archive.write(
                struct.pack(
                    "<Q",
                    compressed_size
                )
            )

            archive.seek(crc_position)
            archive.write(
                struct.pack(
                    "<I",
                    crc
                )
            )

            archive.seek(payload_end)

            total_processed += processed

            file_results.append({
                "name": stored_name,
                "original_size": original_size,
                "compressed_size": compressed_size,
                "crc32": crc,
            })

    archive_size = Path(archive_path).stat().st_size

    if total_original_size > 0:
        saving = (
            1.0 -
            archive_size / total_original_size
        ) * 100.0
    else:
        saving = 0.0

    return {
        "file_count": len(paths),
        "original_size": total_original_size,
        "archive_size": archive_size,
        "saving": saving,
        "files": file_results,
    }


# ============================================================
# Extract all files from one .lzz archive
# ============================================================

def extract_archive(archive_path, output_folder, progress_callback=None):
    archive_path = Path(archive_path)
    output_folder = Path(output_folder)

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(archive_path, "rb") as archive:
        magic = archive.read(4)

        if magic != MAGIC:
            raise ValueError(
                "This is not a valid .lzz archive."
            )

        version_data = archive.read(2)

        if len(version_data) != 2:
            raise ValueError(
                "Archive header is damaged."
            )

        version = struct.unpack(
            "<H",
            version_data
        )[0]

        if version != VERSION:
            raise ValueError(
                f"Unsupported .lzz archive version: {version}"
            )

        count_data = archive.read(4)

        if len(count_data) != 4:
            raise ValueError(
                "Archive file count is missing."
            )

        file_count = struct.unpack(
            "<I",
            count_data
        )[0]

        # First pass: gather total original size for progress.
        entries_start = archive.tell()
        total_original_size = 0

        for _ in range(file_count):
            name_len_data = archive.read(4)

            if len(name_len_data) != 4:
                raise ValueError(
                    "Archive entry header is damaged."
                )

            name_len = struct.unpack(
                "<I",
                name_len_data
            )[0]

            if name_len > 1024 * 1024:
                raise ValueError(
                    "Archive contains an invalid file name length."
                )

            archive.seek(
                name_len,
                os.SEEK_CUR
            )

            original_size_data = archive.read(8)
            compressed_size_data = archive.read(8)
            crc_data = archive.read(4)

            if (
                len(original_size_data) != 8
                or len(compressed_size_data) != 8
                or len(crc_data) != 4
            ):
                raise ValueError(
                    "Archive entry metadata is damaged."
                )

            original_size = struct.unpack(
                "<Q",
                original_size_data
            )[0]

            compressed_size = struct.unpack(
                "<Q",
                compressed_size_data
            )[0]

            total_original_size += original_size

            archive.seek(
                compressed_size,
                os.SEEK_CUR
            )

        # Second pass: actual extraction.
        archive.seek(entries_start)

        total_restored = 0
        extracted_files = []

        for index in range(1, file_count + 1):
            name_len = struct.unpack(
                "<I",
                archive.read(4)
            )[0]

            name_bytes = archive.read(
                name_len
            )

            try:
                stored_name = name_bytes.decode(
                    "utf-8"
                )
            except UnicodeDecodeError:
                raise ValueError(
                    "Archive contains an invalid UTF-8 file name."
                )

            stored_name = safe_extract_name(
                stored_name
            )

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

            bounded = BoundedReader(
                archive,
                compressed_size
            )

            output_path = unique_output_path(
                output_folder,
                stored_name
            )

            last_file_progress = 0

            def file_progress(current):
                nonlocal last_file_progress
                last_file_progress = current

                if progress_callback:
                    progress_callback(
                        total_restored + current,
                        total_original_size,
                        index,
                        file_count,
                        stored_name
                    )

            with open(output_path, "wb") as destination:
                restored, actual_crc = decompress_stream(
                    bounded,
                    destination,
                    original_size,
                    file_progress
                )

            if bounded.remaining != 0:
                # Skip any unread bytes only so the next entry starts correctly,
                # but treat the archive as invalid.
                archive.seek(
                    bounded.remaining,
                    os.SEEK_CUR
                )
                raise ValueError(
                    f"Compressed size mismatch for:\n{stored_name}"
                )

            if restored != original_size:
                raise ValueError(
                    f"Restored size mismatch for:\n{stored_name}"
                )

            if actual_crc != expected_crc:
                raise ValueError(
                    f"CRC check failed for:\n{stored_name}"
                )

            total_restored += restored

            extracted_files.append(
                str(output_path)
            )

    return {
        "file_count": file_count,
        "restored_size": total_restored,
        "files": extracted_files,
    }


# ============================================================
# Windows GUI
# ============================================================

class LZZApp:

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x650")
        self.root.minsize(820, 580)

        self.selected_files = []
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
                "Pack one or many different file types into a single .lzz archive, "
                "or extract an existing .lzz archive."
            )
        ).pack(
            anchor="w",
            pady=(4, 16)
        )

        # ----------------------------------------------------
        # File list
        # ----------------------------------------------------

        files_box = ttk.LabelFrame(
            outer,
            text="Files to archive",
            padding=10
        )
        files_box.pack(
            fill="both",
            expand=True
        )

        list_frame = ttk.Frame(
            files_box
        )
        list_frame.pack(
            fill="both",
            expand=True
        )

        self.file_list = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            font=("Segoe UI", 10)
        )
        self.file_list.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.file_list.yview
        )
        scrollbar.pack(
            side="right",
            fill="y"
        )

        self.file_list.config(
            yscrollcommand=scrollbar.set
        )

        controls = ttk.Frame(
            files_box
        )
        controls.pack(
            fill="x",
            pady=(10, 0)
        )

        self.add_button = ttk.Button(
            controls,
            text="Add Files...",
            command=self.add_files
        )
        self.add_button.pack(
            side="left"
        )

        self.remove_button = ttk.Button(
            controls,
            text="Remove Selected",
            command=self.remove_selected
        )
        self.remove_button.pack(
            side="left",
            padx=(8, 0)
        )

        self.clear_button = ttk.Button(
            controls,
            text="Clear",
            command=self.clear_files
        )
        self.clear_button.pack(
            side="left",
            padx=(8, 0)
        )

        # ----------------------------------------------------
        # Main actions
        # ----------------------------------------------------

        action_frame = ttk.Frame(
            outer
        )
        action_frame.pack(
            pady=16
        )

        self.create_button = ttk.Button(
            action_frame,
            text="CREATE .LZZ ARCHIVE",
            width=25,
            command=self.choose_archive_output
        )
        self.create_button.grid(
            row=0,
            column=0,
            padx=8
        )

        self.extract_button = ttk.Button(
            action_frame,
            text="EXTRACT .LZZ ARCHIVE",
            width=25,
            command=self.choose_archive_to_extract
        )
        self.extract_button.grid(
            row=0,
            column=1,
            padx=8
        )

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

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

        self.progress = ttk.Progressbar(
            progress_box,
            maximum=100,
            variable=self.progress_var
        )
        self.progress.pack(
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

        # ----------------------------------------------------
        # Result
        # ----------------------------------------------------

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
                "Files: 0\n"
                "Original size: -\n"
                "Archive size: -\n"
                "Saving: -"
            )
        )

        ttk.Label(
            result_box,
            textvariable=self.result_var,
            justify="left"
        ).pack(
            anchor="w"
        )

    def add_files(self):
        if self.busy:
            return

        files = filedialog.askopenfilenames(
            title="Select one or more files",
            filetypes=[
                ("All files", "*.*")
            ]
        )

        if not files:
            return

        existing = {
            str(Path(p).resolve()).lower()
            for p in self.selected_files
        }

        for file_path in files:
            resolved = str(
                Path(file_path).resolve()
            )

            if resolved.lower() not in existing:
                self.selected_files.append(
                    resolved
                )
                existing.add(
                    resolved.lower()
                )

        self.refresh_file_list()

    def remove_selected(self):
        if self.busy:
            return

        indexes = list(
            self.file_list.curselection()
        )

        for index in reversed(indexes):
            del self.selected_files[index]

        self.refresh_file_list()

    def clear_files(self):
        if self.busy:
            return

        self.selected_files.clear()
        self.refresh_file_list()

    def refresh_file_list(self):
        self.file_list.delete(
            0,
            tk.END
        )

        total_size = 0

        for path in self.selected_files:
            p = Path(path)
            size = p.stat().st_size
            total_size += size

            self.file_list.insert(
                tk.END,
                f"{p.name}    [{human_size(size)}]    {p.parent}"
            )

        self.result_var.set(
            f"Files: {len(self.selected_files)}\n"
            f"Original size: {human_size(total_size) if self.selected_files else '-'}\n"
            f"Archive size: -\n"
            f"Saving: -"
        )

        self.status_var.set(
            f"{len(self.selected_files)} file(s) selected."
            if self.selected_files
            else "Ready"
        )

    def choose_archive_output(self):
        if not self.selected_files:
            messagebox.showwarning(
                "Warning",
                "Add at least one file first."
            )
            return

        output = filedialog.asksaveasfilename(
            title="Save .lzz archive",
            initialfile="archive.lzz",
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

    def choose_archive_to_extract(self):
        if self.busy:
            return

        archive_file = filedialog.askopenfilename(
            title="Select .lzz archive",
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

    def set_controls(self, enabled):
        state = (
            "normal"
            if enabled
            else "disabled"
        )

        self.add_button.config(
            state=state
        )
        self.remove_button.config(
            state=state
        )
        self.clear_button.config(
            state=state
        )
        self.create_button.config(
            state=state
        )
        self.extract_button.config(
            state=state
        )

    def start_create(self, output):
        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)
        self.status_var.set(
            "Creating archive..."
        )

        thread = threading.Thread(
            target=self.create_worker,
            args=(
                list(self.selected_files),
                output
            ),
            daemon=True
        )
        thread.start()

    def create_worker(
        self,
        files,
        output
    ):
        try:
            def progress(
                current,
                total,
                file_index,
                file_count,
                file_name
            ):
                percent = (
                    100.0
                    if total == 0
                    else current * 100.0 / total
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    f"Compressing {file_index}/{file_count}: {file_name}"
                )

            result = create_archive(
                files,
                output,
                progress
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

        self.result_var.set(
            f"Files: {result['file_count']}\n"
            f"Original size: {human_size(result['original_size'])}\n"
            f"Archive size: {human_size(result['archive_size'])}\n"
            f"Saving: {result['saving']:.2f}%"
        )

        self.status_var.set(
            "Archive created successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            (
                f"Archive created successfully.\n\n"
                f"{output}\n\n"
                f"Files: {result['file_count']}\n"
                f"Original: {human_size(result['original_size'])}\n"
                f"Archive: {human_size(result['archive_size'])}\n"
                f"Saving: {result['saving']:.2f}%"
            )
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

        thread = threading.Thread(
            target=self.extract_worker,
            args=(
                archive_file,
                output_folder
            ),
            daemon=True
        )
        thread.start()

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
                file_name
            ):
                percent = (
                    100.0
                    if total == 0
                    else current * 100.0 / total
                )

                self.root.after(
                    0,
                    self.progress_var.set,
                    percent
                )

                self.root.after(
                    0,
                    self.status_var.set,
                    f"Extracting {file_index}/{file_count}: {file_name}"
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
            f"Files extracted: {result['file_count']}\n"
            f"Restored size: {human_size(result['restored_size'])}\n"
            f"Output folder: {output_folder}\n"
            f"CRC verification: Passed"
        )

        self.status_var.set(
            "Archive extracted successfully."
        )

        self.busy = False
        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            (
                f"Archive extracted successfully.\n\n"
                f"Files: {result['file_count']}\n"
                f"Restored: {human_size(result['restored_size'])}\n\n"
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


def main():
    root = tk.Tk()
    LZZApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
