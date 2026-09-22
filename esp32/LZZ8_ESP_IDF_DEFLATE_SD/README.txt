LZZ8 ESP-IDF DEFLATE SD Test
============================

This is a complete ESP-IDF project for the first embedded LZZ8 test.

Supports:
- LZZ8
- STORE codec
- RAW DEFLATE codec
- files, directories, subdirectories
- SD card via SDSPI
- streaming decompression
- CRC32 and original-size verification

For the first test, create the Windows archive using:

    Method  : DEFLATE
    Profile : Maximum

Do NOT use Auto Best yet: it may choose LZMA2, which this extractor cannot read.

Copy the resulting archive to the SD card root:

    /archive.lzz

Build:

    idf.py set-target esp32c6
    idf.py menuconfig

Open:

    LZZ8 ESP-IDF Test Configuration

and set the actual SD wiring:
- MOSI
- MISO
- CLK
- CS

Default example values are:
- MOSI GPIO7
- MISO GPIO2
- CLK  GPIO6
- CS   GPIO10

Then:

    idf.py build
    idf.py -p COMx flash monitor

Extraction output is written to:

    /LZZ_OUT/

A successful run ends with:

    ALL LZZ8 ENTRIES EXTRACTED SUCCESSFULLY
    TEST RESULT: PASS

Main DEFLATE RAM:
- 32 KiB tinfl dictionary
- 4 KiB input buffer
- tinfl state
- small path buffers

The full decompressed file is not loaded into RAM.
