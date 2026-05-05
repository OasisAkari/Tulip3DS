TARGET := 3DS

NAME := Tulip3DS-Client

BUILD_DIR := build
OUTPUT_DIR := output
INCLUDE_DIRS := include
SOURCE_DIRS := source
# ROMFS_DIR := romfs

LIBRARY_DIRS += $(DEVKITPRO)/libctru $(DEVKITPRO)/portlibs/armv6k $(DEVKITPRO)/portlibs/3ds
LIBRARIES += curl mbedtls mbedx509 mbedcrypto jansson z citro3d ctru

EXTRA_OUTPUT_FILES := servefiles

BUILD_FLAGS := -Wno-format-truncation

VERSION_PARTS := 0.0.1

VERSION_MAJOR := 0
VERSION_MINOR := 0
VERSION_MICRO := 1

DESCRIPTION := Assistant service for Tulip3DS
AUTHOR := OasisAkari

PRODUCT_CODE := Tulip3DS
UNIQUE_ID := 0xAFCB1

ICON_FLAGS := --flags visible,ratingrequired,recordusage --cero 153 --esrb 153 --usk 153 --pegigen 153 --pegiptr 153 --pegibbfc 153 --cob 153 --grb 153 --cgsrr 153

BANNER_AUDIO := meta/audio.wav
BANNER_IMAGE := meta/banner.png
ICON := meta/icon.png

#BANNER_AUDIO := meta/audio_3ds.wav
#BANNER_IMAGE := meta/banner_3ds.cgfx
#ICON := meta/icon_3ds.png
#LOGO := meta/logo_3ds.bcma.lz

# INTERNAL #

include buildtools/make_base
