#!/bin/bash
# Build the Apple Vision OCR CLI next to this script (binary is gitignored).
set -euo pipefail
cd "$(dirname "$0")"
xcrun swiftc -O -parse-as-library -o visionocr main.swift 2>&1 | grep -v "^$" || true
ls -la visionocr
