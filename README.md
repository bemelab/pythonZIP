# pythonZIP — LZZ archiver (Python / Windows) and ESP32 extractor

A Tkinter archive manager for `.lzz` archives with folder support. Each version can open the archives of the versions before it; the latest is **LZZ8**.
`esp32/` holds an ESP-IDF project that extracts LZZ8 archives from an SD card.

## Quick try (Windows)

Download [`bin/pythonZIP.exe`](bin/pythonZIP.exe) and run it. No Python needed.
It is a single-file PyInstaller build of LZZ8 with all codecs (STORE, DEFLATE, LZMA2, PPMd, Brotli, ZPAQ).
The exe is not code-signed, so Windows SmartScreen may warn: *More info → Run anyway*.

SHA-256 of `bin/pythonZIP.exe`: `fc1dae47833165ddb5f2a3cbdb0e0ec4f3f3213bc7bc22bce01c0b47138eba15`

## Versions

| Version | File | What it added |
|---|---|---|
| LZSS (`LZS2`) | `legacy/lzz_windows_single.py` | Single file, LZSS (4095-byte window) |
| LZZ1 | `legacy/lzz_multi_windows.py` | Multi-file archive |
| LZZ1 | `legacy/lzz_iterative_windows.py` | Iterative compression experiment |
| LZZ1 | `legacy/lzz_folders_windows.py` | Folders / subfolders, archive manager |
| LZZ3 | `lzz3/lzz_deflate_windows.py`, `lzz3/deflate.py` | DEFLATE |
| LZZ4 | `lzz4/LZZ4_Windows.py` | LZMA2 |
| LZZ5 | `lzz5/LZZ5_TextOptimize_LZMA2_Windows.py` | Text token table + LZMA2 |
| LZZ6 | `lzz6/LZZ6_AutoBest_PPMd_LZMA2_Windows.py` | STORE, PPMd7; "Auto Best" (best codec per file) |
| LZZ7 | `lzz7/LZZ7_AutoBest_Brotli_PPMd_LZMA2_Windows.py` | Brotli |
| LZZ8 | `lzz8/LZZ8_AutoBest_ZPAQ_Brotli_PPMd_LZMA2_Windows.py` | ZPAQ |
| LZZ8 extractor | `esp32/LZZ8_ESP_IDF_DEFLATE_SD/` | ESP-IDF, SD card (SDSPI), STORE + DEFLATE, CRC32 |

## Run from source

```
pip install pyppmd brotli zpaq
python lzz8\LZZ8_AutoBest_ZPAQ_Brotli_PPMd_LZMA2_Windows.py
```

The extra packages are optional; a codec whose package is missing is disabled.

Build the exe yourself: `pip install pyinstaller`, then `cd lzz8` → `pyinstaller pythonZIP.spec` → `dist\pythonZIP.exe`.

ESP32 project: see `esp32/LZZ8_ESP_IDF_DEFLATE_SD/README.txt`.
