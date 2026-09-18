import os
import struct
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LZSS File Compressor"
MAGIC = b"LZS2"

WINDOW_SIZE = 4095
MIN_MATCH = 3
MAX_MATCH = 18


# ============================================================
# Utility functions
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


# ============================================================
# LZSS compressor
# ============================================================

def compress_file(input_filename, output_filename, progress_callback=None):
    input_path = Path(input_filename)
    output_path = Path(output_filename)

    original_size = input_path.stat().st_size

    processed = 0
    window = bytearray()
    lookahead = bytearray()

    with open(input_path, "rb") as source, \
         open(output_path, "wb") as destination:

        # Header
        # 4 bytes  : MAGIC = LZS2
        # 8 bytes  : original file size
        destination.write(MAGIC)
        destination.write(
            struct.pack("<Q", original_size)
        )

        fill_lookahead(source, lookahead)

        while lookahead:
            flags = 0
            tokens = bytearray()

            # One flag byte describes 8 tokens.
            for bit in range(8):

                if not lookahead:
                    break

                offset, length = find_longest_match(
                    window,
                    lookahead
                )

                # MATCH TOKEN
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

                    del lookahead[:length]

                    add_to_window(
                        window,
                        consumed
                    )

                    processed += length

                # LITERAL TOKEN
                else:
                    value = lookahead[0]

                    tokens.append(value)

                    del lookahead[0]

                    add_to_window(
                        window,
                        bytes([value])
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
                progress_callback(
                    processed * 100.0 / original_size
                )

    compressed_size = output_path.stat().st_size

    if original_size > 0:
        saving = (
            1.0 -
            compressed_size / original_size
        ) * 100.0
    else:
        saving = 0.0

    return {
        "original_size": original_size,
        "compressed_size": compressed_size,
        "saving": saving,
    }


# ============================================================
# LZSS decompressor
# ============================================================

def decompress_file(input_filename, output_filename, progress_callback=None):
    input_path = Path(input_filename)
    output_path = Path(output_filename)

    with open(input_path, "rb") as source, \
         open(output_path, "wb") as destination:

        magic = source.read(4)

        if magic != MAGIC:
            raise ValueError(
                "This file is not a valid LZS2 / .lzss file."
            )

        size_data = source.read(8)

        if len(size_data) != 8:
            raise ValueError(
                "Compressed file header is damaged."
            )

        original_size = struct.unpack(
            "<Q",
            size_data
        )[0]

        history = bytearray()
        restored = 0

        while restored < original_size:
            flag_data = source.read(1)

            if not flag_data:
                raise ValueError(
                    "Compressed file ended unexpectedly."
                )

            flags = flag_data[0]

            for bit in range(8):

                if restored >= original_size:
                    break

                is_match = (
                    flags >> bit
                ) & 1

                # LITERAL TOKEN
                if not is_match:
                    raw = source.read(1)

                    if not raw:
                        raise ValueError(
                            "Missing literal byte."
                        )

                    value = raw[0]

                    destination.write(
                        bytes([value])
                    )

                    add_to_window(
                        history,
                        bytes([value])
                    )

                    restored += 1

                # MATCH TOKEN
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
                            "LZSS offset exceeds history window."
                        )

                    for _ in range(length):

                        if restored >= original_size:
                            break

                        value = history[
                            len(history) - offset
                        ]

                        destination.write(
                            bytes([value])
                        )

                        add_to_window(
                            history,
                            bytes([value])
                        )

                        restored += 1

                if progress_callback and original_size > 0:
                    progress_callback(
                        restored * 100.0 / original_size
                    )

    return {
        "compressed_size": input_path.stat().st_size,
        "restored_size": original_size,
    }


# ============================================================
# GUI
# ============================================================

class LZSSApp:

    def __init__(self, root):
        self.root = root

        self.root.title(APP_TITLE)
        self.root.geometry("780x520")
        self.root.minsize(780, 520)

        self.input_file = ""
        self.busy = False

        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(
            self.root,
            padding=24
        )

        outer.pack(
            fill="both",
            expand=True
        )

        title = ttk.Label(
            outer,
            text=APP_TITLE,
            font=("Segoe UI", 20, "bold")
        )

        title.pack(anchor="w")

        subtitle = ttk.Label(
            outer,
            text="General-purpose LZSS compression and decompression tool"
        )

        subtitle.pack(
            anchor="w",
            pady=(4, 18)
        )

        # File section
        file_box = ttk.LabelFrame(
            outer,
            text="Input File",
            padding=12
        )

        file_box.pack(fill="x")

        file_row = ttk.Frame(file_box)

        file_row.pack(fill="x")

        self.file_var = tk.StringVar(
            value="No file selected"
        )

        self.file_entry = ttk.Entry(
            file_row,
            textvariable=self.file_var,
            state="readonly"
        )

        self.file_entry.pack(
            side="left",
            fill="x",
            expand=True
        )

        self.select_button = ttk.Button(
            file_row,
            text="Select File",
            command=self.select_file
        )

        self.select_button.pack(
            side="left",
            padx=(10, 0)
        )

        # Action buttons
        button_frame = ttk.Frame(outer)

        button_frame.pack(
            pady=22
        )

        self.compress_button = ttk.Button(
            button_frame,
            text="COMPRESS",
            width=22,
            command=self.choose_compress_output
        )

        self.compress_button.grid(
            row=0,
            column=0,
            padx=10
        )

        self.decompress_button = ttk.Button(
            button_frame,
            text="DECOMPRESS",
            width=22,
            command=self.choose_decompress_output
        )

        self.decompress_button.grid(
            row=0,
            column=1,
            padx=10
        )

        # Progress
        progress_box = ttk.LabelFrame(
            outer,
            text="Progress",
            padding=12
        )

        progress_box.pack(fill="x")

        self.progress_var = tk.DoubleVar(
            value=0
        )

        self.progress = ttk.Progressbar(
            progress_box,
            maximum=100,
            variable=self.progress_var
        )

        self.progress.pack(fill="x")

        self.status_var = tk.StringVar(
            value="Ready"
        )

        ttk.Label(
            progress_box,
            textvariable=self.status_var
        ).pack(
            anchor="w",
            pady=(8, 0)
        )

        # Result area
        info_box = ttk.LabelFrame(
            outer,
            text="Result",
            padding=12
        )

        info_box.pack(
            fill="x",
            pady=(18, 0)
        )

        self.info_var = tk.StringVar(
            value=(
                "Input size: -\n"
                "Output size: -\n"
                "Saving: -"
            )
        )

        ttk.Label(
            info_box,
            textvariable=self.info_var,
            justify="left"
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Note: ZIP, RAR, JPG, PNG, MP4 and other already-compressed "
                "files may not become smaller."
            ),
            font=("Segoe UI", 9)
        ).pack(
            anchor="w",
            pady=(15, 0)
        )

    def select_file(self):
        if self.busy:
            return

        filename = filedialog.askopenfilename(
            title="Select a file",
            filetypes=[
                ("All files", "*.*")
            ]
        )

        if not filename:
            return

        self.input_file = filename
        self.file_var.set(filename)

        size = Path(filename).stat().st_size

        self.info_var.set(
            f"Input size: {human_size(size)}\n"
            f"Output size: -\n"
            f"Saving: -"
        )

        self.status_var.set(
            "File selected."
        )

        self.progress_var.set(0)

    def choose_compress_output(self):
        if not self.input_file:
            messagebox.showwarning(
                "Warning",
                "Select a file first."
            )
            return

        source_path = Path(
            self.input_file
        )

        output_file = filedialog.asksaveasfilename(
            title="Save compressed file",
            initialfile=source_path.name + ".lzss",
            defaultextension=".lzss",
            filetypes=[
                ("LZSS file", "*.lzss"),
                ("All files", "*.*")
            ]
        )

        if output_file:
            self.start_job(
                "compress",
                output_file
            )

    def choose_decompress_output(self):
        if not self.input_file:
            messagebox.showwarning(
                "Warning",
                "Select a .lzss file first."
            )
            return

        source_path = Path(
            self.input_file
        )

        if source_path.suffix.lower() == ".lzss":
            default_name = source_path.stem
        else:
            default_name = source_path.name + ".restored"

        output_file = filedialog.asksaveasfilename(
            title="Save decompressed file",
            initialfile=default_name,
            filetypes=[
                ("All files", "*.*")
            ]
        )

        if output_file:
            self.start_job(
                "decompress",
                output_file
            )

    def start_job(
        self,
        mode,
        output_file
    ):
        if self.busy:
            return

        self.busy = True
        self.set_controls(False)

        self.progress_var.set(0)

        if mode == "compress":
            self.status_var.set(
                "Compressing..."
            )
        else:
            self.status_var.set(
                "Decompressing..."
            )

        worker_thread = threading.Thread(
            target=self.worker,
            args=(
                mode,
                self.input_file,
                output_file
            ),
            daemon=True
        )

        worker_thread.start()

    def worker(
        self,
        mode,
        input_file,
        output_file
    ):
        try:

            def progress_callback(value):
                self.root.after(
                    0,
                    self.progress_var.set,
                    value
                )

            if mode == "compress":

                result = compress_file(
                    input_file,
                    output_file,
                    progress_callback
                )

                info = (
                    f"Input size: "
                    f"{human_size(result['original_size'])}\n"
                    f"Output size: "
                    f"{human_size(result['compressed_size'])}\n"
                    f"Saving: "
                    f"{result['saving']:.2f}%"
                )

                message = (
                    "Compression completed successfully.\n\n"
                    f"{output_file}"
                )

            else:

                result = decompress_file(
                    input_file,
                    output_file,
                    progress_callback
                )

                info = (
                    f"Compressed size: "
                    f"{human_size(result['compressed_size'])}\n"
                    f"Restored size: "
                    f"{human_size(result['restored_size'])}\n"
                    f"Status: Success"
                )

                message = (
                    "Decompression completed successfully.\n\n"
                    f"{output_file}"
                )

            self.root.after(
                0,
                self.finish_success,
                info,
                message
            )

        except Exception as error:
            self.root.after(
                0,
                self.finish_error,
                str(error)
            )

    def finish_success(
        self,
        info,
        message
    ):
        self.progress_var.set(100)
        self.info_var.set(info)

        self.status_var.set(
            "Completed."
        )

        self.busy = False

        self.set_controls(True)

        messagebox.showinfo(
            "Completed",
            message
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

        self.select_button.config(
            state=state
        )

        self.compress_button.config(
            state=state
        )

        self.decompress_button.config(
            state=state
        )


# ============================================================
# Program entry point
# ============================================================

def main():
    root = tk.Tk()

    app = LZSSApp(root)

    root.mainloop()


if __name__ == "__main__":
    main()
