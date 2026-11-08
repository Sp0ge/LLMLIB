
PYTHON ?= python3
APP_NAME := PortableAI
ENTRY := app.py
OUTPUT := app_builds
BUILD_DIR := .pyinstaller_build
OS := $(shell uname -s)

.PHONY: build install clean build-macos build-windows

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install pyinstaller

build:
ifeq ($(OS),Darwin)
	$(MAKE) build-macos
else
	$(MAKE) build-windows
endif

build-macos:
	$(PYTHON) -m PyInstaller \
		--noconfirm \
		--clean \
		--onefile \
		--windowed \
		--name $(APP_NAME) \
		--distpath $(OUTPUT) \
		--workpath $(BUILD_DIR) \
		--specpath $(BUILD_DIR) \
		--collect-all PySide6 \
		$(ENTRY)

build-windows:
	$(PYTHON) -m PyInstaller \
		--noconfirm \
		--clean \
		--onefile \
		--windowed \
		--name $(APP_NAME) \
		--distpath $(OUTPUT) \
		--workpath $(BUILD_DIR) \
		--specpath $(BUILD_DIR) \
		--collect-all PySide6 \
		$(ENTRY)

clean:
	rm -rf $(BUILD_DIR) app_builds
