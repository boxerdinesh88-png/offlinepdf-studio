# Third-party components

OfflinePDF Studio itself is AGPL-3.0-or-later. The components below are
bundled as files and are licensed under their own terms. Every font is
vendored into `assets/fonts/` by `scripts/vendor_fonts.py` (development
time only); the application never downloads fonts at runtime.

## Font library (`assets/fonts/`)

| Family | Files | Licence | Purpose |
| --- | ---: | --- | --- |
| Caladea | 4 | OFL-1.1 | metric-compatible replacement for Cambria |
| Carlito | 4 | OFL-1.1 | metric-compatible replacement for Calibri |
| LiberationMono | 4 | OFL-1.1 | metric-compatible replacement for Courier New |
| LiberationSans | 4 | OFL-1.1 | metric-compatible replacement for Arial |
| LiberationSerif | 4 | OFL-1.1 | metric-compatible replacement for Times New Roman |
| NotoSans | 2 | OFL-1.1 | Latin / Greek / Cyrillic script coverage |
| NotoSansArabic | 2 | OFL-1.1 | Arabic script coverage |
| NotoSansDevanagari | 2 | OFL-1.1 | Devanagari script coverage |
| NotoSansHebrew | 2 | OFL-1.1 | Hebrew script coverage |
| NotoSansSC | 2 | OFL-1.1 | Simplified Chinese (CJK) coverage |

Licence texts: `assets/fonts/<Family>/LICENSE.txt` (OFL-1.1) and
`assets/fonts/licenses/liberation-copyright.txt` (Debian copyright file
for the Liberation packages, which are also OFL-1.1).

SHA-256 checksums for every bundled file: `assets/fonts/manifest.json`.

## Python dependencies

See `requirements.lock` for the pinned stack (PySide6, PyMuPDF, pikepdf,
fontTools, uharfbuzz, numpy, Pillow, reportlab) and the `LICENSE` files
inside the installed distributions.

<!-- OCR models -->

- **PaddleOCR / PP-OCR models** (`assets/ocr/`) - Apache-2.0, Copyright (c) 2020 PaddlePaddle Authors. See <https://github.com/PaddlePaddle/PaddleOCR>.
- **RapidOCR** (`rapidocr-onnxruntime`) - Apache-2.0, Copyright (c) RapidAI. See <https://github.com/RapidAI/RapidOCR>.
- **ONNX Runtime** - MIT, Microsoft Corporation.
- **OpenCV** (`opencv-python`) - Apache-2.0, Copyright (c) 2000-2024 Intel Corporation / Itseez.
- **pyclipper** - MIT; **Shapely** - BSD-3-Clause; **PyYAML** - MIT; **six** - MIT; **protobuf** - BSD-3-Clause; **flatbuffers** - Apache-2.0.
