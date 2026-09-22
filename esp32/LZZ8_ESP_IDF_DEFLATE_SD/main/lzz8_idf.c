/*
 * LZZ8 ESP-IDF SD Card Extractor
 *
 * Supports:
 *   - LZZ8
 *   - STORE (codec 0)
 *   - RAW DEFLATE (codec 1)
 *   - folders/subfolders
 *   - SD card via SDSPI
 *   - streaming extraction
 *   - CRC32 verification
 *
 * Create the Windows archive with:
 *   Method  : DEFLATE
 *   Profile : Maximum
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#include <inttypes.h>
#include <sys/stat.h>

#include "sdkconfig.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_vfs_fat.h"

#include "driver/spi_common.h"
#include "driver/sdspi_host.h"

#include "sdmmc_cmd.h"
#include "miniz.h"


static const char *TAG = "LZZ8";

#define MOUNT_POINT       "/sdcard"
#define INPUT_BUFFER_SIZE 4096U
#define MAX_ARCHIVE_PATH  512U

#define ENTRY_FILE 0U
#define ENTRY_DIR  1U

#define CODEC_STORE      0U
#define CODEC_DEFLATE    1U
#define CODEC_LZMA2      2U
#define CODEC_TEXT_LZMA2 3U
#define CODEC_PPMD7      4U
#define CODEC_BROTLI     5U
#define CODEC_ZPAQ       6U


static bool read_exact(FILE *f, void *buffer, size_t size)
{
    return fread(buffer, 1, size, f) == size;
}

static bool read_u8(FILE *f, uint8_t *value)
{
    return read_exact(f, value, 1);
}

static bool read_u16_le(FILE *f, uint16_t *value)
{
    uint8_t b[2];
    if (!read_exact(f, b, sizeof(b))) return false;
    *value = ((uint16_t)b[0]) |
             ((uint16_t)b[1] << 8);
    return true;
}

static bool read_u32_le(FILE *f, uint32_t *value)
{
    uint8_t b[4];
    if (!read_exact(f, b, sizeof(b))) return false;
    *value = ((uint32_t)b[0]) |
             ((uint32_t)b[1] << 8) |
             ((uint32_t)b[2] << 16) |
             ((uint32_t)b[3] << 24);
    return true;
}

static bool read_u64_le(FILE *f, uint64_t *value)
{
    uint8_t b[8];
    if (!read_exact(f, b, sizeof(b))) return false;

    *value =
        ((uint64_t)b[0]) |
        ((uint64_t)b[1] << 8) |
        ((uint64_t)b[2] << 16) |
        ((uint64_t)b[3] << 24) |
        ((uint64_t)b[4] << 32) |
        ((uint64_t)b[5] << 40) |
        ((uint64_t)b[6] << 48) |
        ((uint64_t)b[7] << 56);

    return true;
}


static void normalize_slashes(char *path)
{
    while (*path) {
        if (*path == '\\') *path = '/';
        ++path;
    }
}

static bool archive_path_is_safe(const char *path)
{
    if (!path || !path[0]) return false;
    if (path[0] == '/' || path[0] == '\\') return false;
    if (strchr(path, ':')) return false;

    const char *p = path;

    while (*p) {
        while (*p == '/') ++p;
        if (!*p) break;

        const char *start = p;
        while (*p && *p != '/') ++p;

        size_t len = (size_t)(p - start);

        if ((len == 1 && start[0] == '.') ||
            (len == 2 && start[0] == '.' && start[1] == '.')) {
            return false;
        }
    }

    return true;
}

static esp_err_t mkdir_recursive(const char *path)
{
    char *temp = strdup(path);
    if (!temp) return ESP_ERR_NO_MEM;

    size_t len = strlen(temp);

    for (size_t i = 1; i < len; ++i) {
        if (temp[i] == '/') {
            temp[i] = '\0';

            if (mkdir(temp, 0775) != 0 && errno != EEXIST) {
                ESP_LOGE(TAG, "mkdir failed: %s (errno=%d)", temp, errno);
                free(temp);
                return ESP_FAIL;
            }

            temp[i] = '/';
        }
    }

    if (mkdir(temp, 0775) != 0 && errno != EEXIST) {
        ESP_LOGE(TAG, "mkdir failed: %s (errno=%d)", temp, errno);
        free(temp);
        return ESP_FAIL;
    }

    free(temp);
    return ESP_OK;
}

static esp_err_t make_parent_directories(const char *file_path)
{
    char *temp = strdup(file_path);
    if (!temp) return ESP_ERR_NO_MEM;

    char *slash = strrchr(temp, '/');

    if (!slash || slash == temp) {
        free(temp);
        return ESP_OK;
    }

    *slash = '\0';

    esp_err_t err = mkdir_recursive(temp);

    free(temp);
    return err;
}

static char *make_output_path(const char *relative_path)
{
    size_t needed =
        strlen(MOUNT_POINT) + 1 +
        strlen(CONFIG_LZZ_OUTPUT_DIRECTORY) + 1 +
        strlen(relative_path) + 1;

    char *path = malloc(needed);
    if (!path) return NULL;

    snprintf(
        path,
        needed,
        "%s/%s/%s",
        MOUNT_POINT,
        CONFIG_LZZ_OUTPUT_DIRECTORY,
        relative_path
    );

    return path;
}


static esp_err_t extract_store(
    FILE *archive,
    uint64_t compressed_size,
    uint64_t original_size,
    const char *output_path,
    uint32_t expected_crc)
{
    if (compressed_size != original_size) {
        ESP_LOGE(TAG, "STORE size mismatch");
        return ESP_ERR_INVALID_SIZE;
    }

    esp_err_t err = make_parent_directories(output_path);
    if (err != ESP_OK) return err;

    remove(output_path);

    FILE *out = fopen(output_path, "wb");
    if (!out) {
        ESP_LOGE(TAG, "Cannot create %s", output_path);
        return ESP_FAIL;
    }

    uint8_t *buffer = malloc(INPUT_BUFFER_SIZE);
    if (!buffer) {
        fclose(out);
        return ESP_ERR_NO_MEM;
    }

    uint64_t remaining = compressed_size;
    uint64_t restored = 0;
    mz_ulong crc = MZ_CRC32_INIT;

    while (remaining > 0) {
        size_t amount =
            remaining > INPUT_BUFFER_SIZE
            ? INPUT_BUFFER_SIZE
            : (size_t)remaining;

        if (fread(buffer, 1, amount, archive) != amount) {
            ESP_LOGE(TAG, "Unexpected end of STORE payload");
            free(buffer);
            fclose(out);
            return ESP_FAIL;
        }

        if (fwrite(buffer, 1, amount, out) != amount) {
            ESP_LOGE(TAG, "Write failed: %s", output_path);
            free(buffer);
            fclose(out);
            return ESP_FAIL;
        }

        crc = mz_crc32(crc, buffer, amount);
        remaining -= amount;
        restored += amount;
    }

    free(buffer);
    fclose(out);

    ESP_LOGI(
        TAG,
        "STORE restored=%" PRIu64 " CRC expected=%08" PRIX32 " actual=%08lX",
        restored,
        expected_crc,
        (unsigned long)crc
    );

    if (restored != original_size) return ESP_ERR_INVALID_SIZE;
    if ((uint32_t)crc != expected_crc) return ESP_ERR_INVALID_CRC;

    return ESP_OK;
}


static esp_err_t extract_raw_deflate(
    FILE *archive,
    uint64_t compressed_size,
    uint64_t original_size,
    const char *output_path,
    uint32_t expected_crc)
{
    esp_err_t err = make_parent_directories(output_path);
    if (err != ESP_OK) return err;

    remove(output_path);

    FILE *out = fopen(output_path, "wb");
    if (!out) {
        ESP_LOGE(TAG, "Cannot create %s", output_path);
        return ESP_FAIL;
    }

    uint8_t *input_buffer = malloc(INPUT_BUFFER_SIZE);

    uint8_t *dict = heap_caps_malloc(
        TINFL_LZ_DICT_SIZE,
        MALLOC_CAP_8BIT
    );

    tinfl_decompressor *dec = heap_caps_malloc(
        sizeof(tinfl_decompressor),
        MALLOC_CAP_8BIT
    );

    if (!input_buffer || !dict || !dec) {
        ESP_LOGE(TAG, "Not enough RAM for DEFLATE decoder");
        free(input_buffer);
        free(dict);
        free(dec);
        fclose(out);
        return ESP_ERR_NO_MEM;
    }

    tinfl_init(dec);

    uint64_t unread_payload = compressed_size;
    size_t input_pos = 0;
    size_t input_available = 0;
    size_t dict_offset = 0;

    uint64_t restored = 0;
    mz_ulong crc = MZ_CRC32_INIT;
    esp_err_t result = ESP_FAIL;

    while (true) {
        if (input_available == 0 && unread_payload > 0) {
            size_t amount =
                unread_payload > INPUT_BUFFER_SIZE
                ? INPUT_BUFFER_SIZE
                : (size_t)unread_payload;

            size_t got = fread(
                input_buffer,
                1,
                amount,
                archive
            );

            if (got == 0) {
                ESP_LOGE(TAG, "Unexpected end of DEFLATE payload");
                break;
            }

            input_pos = 0;
            input_available = got;
            unread_payload -= got;
        }

        size_t in_bytes = input_available;
        size_t out_bytes = TINFL_LZ_DICT_SIZE - dict_offset;

        mz_uint32 flags = 0;

        /* LZZ8 stores RFC1951 RAW DEFLATE: no zlib header flag. */
        if (unread_payload > 0) {
            flags |= TINFL_FLAG_HAS_MORE_INPUT;
        }

        tinfl_status status = tinfl_decompress(
            dec,
            input_buffer + input_pos,
            &in_bytes,
            dict,
            dict + dict_offset,
            &out_bytes,
            flags
        );

        input_pos += in_bytes;
        input_available -= in_bytes;

        if (out_bytes > 0) {
            if (restored + out_bytes > original_size) {
                ESP_LOGE(TAG, "DEFLATE produced too much output");
                break;
            }

            if (fwrite(
                    dict + dict_offset,
                    1,
                    out_bytes,
                    out
                ) != out_bytes) {
                ESP_LOGE(TAG, "Write failed: %s", output_path);
                break;
            }

            crc = mz_crc32(
                crc,
                dict + dict_offset,
                out_bytes
            );

            restored += out_bytes;
            dict_offset += out_bytes;

            if (dict_offset == TINFL_LZ_DICT_SIZE) {
                dict_offset = 0;
            }
        }

        if (status == TINFL_STATUS_DONE) {
            if (input_available != 0 || unread_payload != 0) {
                ESP_LOGE(
                    TAG,
                    "DEFLATE ended before compressed_size boundary "
                    "(buffered=%u unread=%" PRIu64 ")",
                    (unsigned)input_available,
                    unread_payload
                );
                break;
            }

            result = ESP_OK;
            break;
        }

        if (status < TINFL_STATUS_DONE) {
            ESP_LOGE(TAG, "tinfl failed, status=%d", (int)status);
            break;
        }

        if (
            in_bytes == 0 &&
            out_bytes == 0 &&
            input_available == 0 &&
            unread_payload == 0
        ) {
            ESP_LOGE(TAG, "DEFLATE decoder made no progress");
            break;
        }
    }

    free(input_buffer);
    free(dict);
    free(dec);
    fclose(out);

    if (result != ESP_OK) {
        remove(output_path);
        return result;
    }

    ESP_LOGI(
        TAG,
        "DEFLATE restored=%" PRIu64 "/%" PRIu64
        " CRC expected=%08" PRIX32 " actual=%08lX",
        restored,
        original_size,
        expected_crc,
        (unsigned long)crc
    );

    if (restored != original_size) {
        remove(output_path);
        return ESP_ERR_INVALID_SIZE;
    }

    if ((uint32_t)crc != expected_crc) {
        remove(output_path);
        return ESP_ERR_INVALID_CRC;
    }

    return ESP_OK;
}


static esp_err_t extract_lzz8(const char *archive_path)
{
    FILE *archive = fopen(archive_path, "rb");

    if (!archive) {
        ESP_LOGE(TAG, "Cannot open archive: %s", archive_path);
        return ESP_ERR_NOT_FOUND;
    }

    uint8_t magic[4];

    if (!read_exact(archive, magic, sizeof(magic))) {
        fclose(archive);
        return ESP_FAIL;
    }

    if (
        magic[0] != 'L' ||
        magic[1] != 'Z' ||
        magic[2] != 'Z' ||
        magic[3] != '8'
    ) {
        ESP_LOGE(
            TAG,
            "Unsupported magic: %c%c%c%c",
            magic[0], magic[1], magic[2], magic[3]
        );
        fclose(archive);
        return ESP_ERR_NOT_SUPPORTED;
    }

    uint16_t version = 0;
    uint16_t flags = 0;
    uint32_t entry_count = 0;

    if (
        !read_u16_le(archive, &version) ||
        !read_u16_le(archive, &flags) ||
        !read_u32_le(archive, &entry_count)
    ) {
        fclose(archive);
        return ESP_FAIL;
    }

    if (version != 8) {
        ESP_LOGE(TAG, "Unsupported LZZ8 version: %u", version);
        fclose(archive);
        return ESP_ERR_NOT_SUPPORTED;
    }

    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "LZZ8 ESP-IDF extractor");
    ESP_LOGI(TAG, "Version   : %u", version);
    ESP_LOGI(TAG, "Flags     : 0x%04X", flags);
    ESP_LOGI(TAG, "Entries   : %" PRIu32, entry_count);
    ESP_LOGI(TAG, "Free heap : %" PRIu32, esp_get_free_heap_size());
    ESP_LOGI(TAG, "========================================");

    char output_root[256];

    int n = snprintf(
        output_root,
        sizeof(output_root),
        "%s/%s",
        MOUNT_POINT,
        CONFIG_LZZ_OUTPUT_DIRECTORY
    );

    if (n <= 0 || n >= (int)sizeof(output_root)) {
        fclose(archive);
        return ESP_ERR_INVALID_SIZE;
    }

    esp_err_t err = mkdir_recursive(output_root);
    if (err != ESP_OK) {
        fclose(archive);
        return err;
    }

    for (uint32_t index = 0; index < entry_count; ++index) {
        uint8_t entry_type = 0;
        uint32_t path_length = 0;

        if (
            !read_u8(archive, &entry_type) ||
            !read_u32_le(archive, &path_length)
        ) {
            fclose(archive);
            return ESP_FAIL;
        }

        if (
            path_length == 0 ||
            path_length > MAX_ARCHIVE_PATH
        ) {
            ESP_LOGE(TAG, "Invalid path length: %" PRIu32, path_length);
            fclose(archive);
            return ESP_ERR_INVALID_SIZE;
        }

        char *relative_path = malloc(path_length + 1);
        if (!relative_path) {
            fclose(archive);
            return ESP_ERR_NO_MEM;
        }

        if (!read_exact(archive, relative_path, path_length)) {
            free(relative_path);
            fclose(archive);
            return ESP_FAIL;
        }

        relative_path[path_length] = '\0';
        normalize_slashes(relative_path);

        if (!archive_path_is_safe(relative_path)) {
            ESP_LOGE(TAG, "Unsafe path: %s", relative_path);
            free(relative_path);
            fclose(archive);
            return ESP_ERR_INVALID_ARG;
        }

        char *output_path = make_output_path(relative_path);

        if (!output_path) {
            free(relative_path);
            fclose(archive);
            return ESP_ERR_NO_MEM;
        }

        ESP_LOGI(
            TAG,
            "[%" PRIu32 "/%" PRIu32 "] %s",
            index + 1,
            entry_count,
            relative_path
        );

        if (entry_type == ENTRY_DIR) {
            err = mkdir_recursive(output_path);

            free(output_path);
            free(relative_path);

            if (err != ESP_OK) {
                fclose(archive);
                return err;
            }

            continue;
        }

        if (entry_type != ENTRY_FILE) {
            ESP_LOGE(TAG, "Unknown entry type: %u", entry_type);
            free(output_path);
            free(relative_path);
            fclose(archive);
            return ESP_ERR_NOT_SUPPORTED;
        }

        uint8_t codec = 0;
        uint8_t profile = 0;
        uint32_t param_a = 0;
        uint32_t param_b = 0;
        uint64_t original_size = 0;
        uint64_t compressed_size = 0;
        uint32_t expected_crc = 0;

        if (
            !read_u8(archive, &codec) ||
            !read_u8(archive, &profile) ||
            !read_u32_le(archive, &param_a) ||
            !read_u32_le(archive, &param_b) ||
            !read_u64_le(archive, &original_size) ||
            !read_u64_le(archive, &compressed_size) ||
            !read_u32_le(archive, &expected_crc)
        ) {
            free(output_path);
            free(relative_path);
            fclose(archive);
            return ESP_FAIL;
        }

        ESP_LOGI(TAG, "  codec      : %u", codec);
        ESP_LOGI(TAG, "  profile    : %u", profile);
        ESP_LOGI(TAG, "  param_a    : %" PRIu32, param_a);
        ESP_LOGI(TAG, "  param_b    : %" PRIu32, param_b);
        ESP_LOGI(TAG, "  original   : %" PRIu64, original_size);
        ESP_LOGI(TAG, "  compressed : %" PRIu64, compressed_size);

        if (codec == CODEC_STORE) {
            err = extract_store(
                archive,
                compressed_size,
                original_size,
                output_path,
                expected_crc
            );
        }
        else if (codec == CODEC_DEFLATE) {
            err = extract_raw_deflate(
                archive,
                compressed_size,
                original_size,
                output_path,
                expected_crc
            );
        }
        else {
            ESP_LOGE(
                TAG,
                "Codec %u unsupported in this first ESP-IDF test.",
                codec
            );

            if (codec == CODEC_LZMA2) {
                ESP_LOGE(
                    TAG,
                    "Re-create the Windows archive with Method=DEFLATE."
                );
            }

            free(output_path);
            free(relative_path);
            fclose(archive);
            return ESP_ERR_NOT_SUPPORTED;
        }

        if (err != ESP_OK) {
            ESP_LOGE(
                TAG,
                "Extraction failed for %s: %s",
                relative_path,
                esp_err_to_name(err)
            );

            free(output_path);
            free(relative_path);
            fclose(archive);
            return err;
        }

        ESP_LOGI(TAG, "  OK");

        free(output_path);
        free(relative_path);
    }

    fclose(archive);

    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, "ALL LZZ8 ENTRIES EXTRACTED SUCCESSFULLY");
    ESP_LOGI(TAG, "========================================");

    return ESP_OK;
}


static esp_err_t mount_sd_card(
    sdmmc_card_t **out_card,
    sdmmc_host_t *out_host)
{
    esp_vfs_fat_sdmmc_mount_config_t mount_config = {
        .format_if_mount_failed = false,
        .max_files = 5,
        .allocation_unit_size = 16 * 1024,
    };

    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    host.max_freq_khz = CONFIG_LZZ_SD_MAX_FREQ_KHZ;

    spi_bus_config_t bus_cfg = {
        .mosi_io_num = CONFIG_LZZ_SD_MOSI_GPIO,
        .miso_io_num = CONFIG_LZZ_SD_MISO_GPIO,
        .sclk_io_num = CONFIG_LZZ_SD_CLK_GPIO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };

    ESP_LOGI(
        TAG,
        "SD pins MOSI=%d MISO=%d CLK=%d CS=%d",
        CONFIG_LZZ_SD_MOSI_GPIO,
        CONFIG_LZZ_SD_MISO_GPIO,
        CONFIG_LZZ_SD_CLK_GPIO,
        CONFIG_LZZ_SD_CS_GPIO
    );

    esp_err_t ret = spi_bus_initialize(
        host.slot,
        &bus_cfg,
        SDSPI_DEFAULT_DMA
    );

    if (ret != ESP_OK) {
        ESP_LOGE(
            TAG,
            "spi_bus_initialize failed: %s",
            esp_err_to_name(ret)
        );
        return ret;
    }

    sdspi_device_config_t slot_config =
        SDSPI_DEVICE_CONFIG_DEFAULT();

    slot_config.gpio_cs = CONFIG_LZZ_SD_CS_GPIO;
    slot_config.host_id = host.slot;

    ret = esp_vfs_fat_sdspi_mount(
        MOUNT_POINT,
        &host,
        &slot_config,
        &mount_config,
        out_card
    );

    if (ret != ESP_OK) {
        ESP_LOGE(
            TAG,
            "SD mount failed: %s",
            esp_err_to_name(ret)
        );

        spi_bus_free(host.slot);
        return ret;
    }

    *out_host = host;
    return ESP_OK;
}


void app_main(void)
{
    ESP_LOGI(TAG, "LZZ8 ESP-IDF DEFLATE SD test starting");

    sdmmc_card_t *card = NULL;
    sdmmc_host_t host = {0};

    esp_err_t ret = mount_sd_card(
        &card,
        &host
    );

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "TEST RESULT: FAIL (SD mount)");
        return;
    }

    sdmmc_card_print_info(stdout, card);

    char archive_path[256];

    int n = snprintf(
        archive_path,
        sizeof(archive_path),
        "%s/%s",
        MOUNT_POINT,
        CONFIG_LZZ_ARCHIVE_FILENAME
    );

    if (n <= 0 || n >= (int)sizeof(archive_path)) {
        ESP_LOGE(TAG, "Archive path is too long");
        esp_vfs_fat_sdcard_unmount(MOUNT_POINT, card);
        spi_bus_free(host.slot);
        return;
    }

    ESP_LOGI(TAG, "Archive: %s", archive_path);

    struct stat st;

    if (stat(archive_path, &st) != 0) {
        ESP_LOGE(TAG, "Archive not found: %s", archive_path);
        esp_vfs_fat_sdcard_unmount(MOUNT_POINT, card);
        spi_bus_free(host.slot);
        return;
    }

    ESP_LOGI(
        TAG,
        "Archive size: %lld bytes",
        (long long)st.st_size
    );

    ret = extract_lzz8(archive_path);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "TEST RESULT: PASS");
    }
    else {
        ESP_LOGE(
            TAG,
            "TEST RESULT: FAIL (%s)",
            esp_err_to_name(ret)
        );
    }

    ESP_LOGI(
        TAG,
        "Free heap after extraction: %" PRIu32,
        esp_get_free_heap_size()
    );

    esp_vfs_fat_sdcard_unmount(
        MOUNT_POINT,
        card
    );

    spi_bus_free(host.slot);

    ESP_LOGI(TAG, "SD card unmounted");
}
