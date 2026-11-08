# crux-installer: static AArch64 update-binary of the TWRP ZIP, plus a host test build.
#
#   make            build/crux-installer       (static; Ubuntu aarch64-linux-gnu- or the
#                                               macOS aarch64-unknown-linux-gnu- toolchain)
#   make host       build/crux-installer-host  (CRUX_TEST: --root fixture, --confirm)
#   make test       host build and the offline fault-injection tests

CROSS ?= $(if $(shell command -v aarch64-linux-gnu-gcc),aarch64-linux-gnu-,aarch64-unknown-linux-gnu-)
HOSTCC ?= cc
PYTHON ?= python3
OUT := build

ZSTD := third_party/zstd
MINCRYPT := third_party/mincrypt
SOURCES := crux_installer.c \
	$(MINCRYPT)/p256.c $(MINCRYPT)/p256_ec.c $(MINCRYPT)/p256_ecdsa.c $(MINCRYPT)/sha256.c \
	$(wildcard $(ZSTD)/common/*.c) $(wildcard $(ZSTD)/decompress/*.c)
CFLAGS := -std=gnu11 -Os -Wall -Wextra -I$(MINCRYPT)/includes -I$(ZSTD) -I$(ZSTD)/common \
	-DZSTD_DISABLE_ASM -DZSTD_LEGACY_SUPPORT=0 -DXXH_NAMESPACE=ZSTD_

.PHONY: all host test clean

all: $(OUT)/crux-installer

host: $(OUT)/crux-installer-host

$(OUT)/crux-installer: $(SOURCES) Makefile
	@mkdir -p $(OUT)
	$(CROSS)gcc $(CFLAGS) -static -s -pthread -o $@ $(SOURCES)

$(OUT)/crux-installer-host: $(SOURCES) Makefile
	@mkdir -p $(OUT)
	$(HOSTCC) $(CFLAGS) -DCRUX_TEST -Wno-unused-parameter -o $@ $(SOURCES)

test: host
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) test_installer.py -v

clean:
	rm -rf $(OUT)
