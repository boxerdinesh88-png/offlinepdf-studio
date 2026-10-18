# Changelog

All notable changes to OfflinePDF Studio are documented in this file.
The project is fully offline; there are no remote resources or network calls.

## [Unreleased]

### Added (scanned pages, round 3)

- **Make Text Editable** toolbar button; entering Edit mode recognises the
  visible scanned page automatically.
- **Revert page to scan** (drops the OCR layer and every edit on the page)
  and **Make document searchable** (invisible text layer on every scanned
  page without editing).
- Warning when a scan is below 150 DPI (recognition will be unreliable).
- **Per-word confidence:** lines the recogniser is unsure about are re-read
  word by word; only the doubtful words are highlighted (default below 70%,
  Tools > OCR Settings).
- Dropped word spaces are restored ("Note7731" -> "Note 7731") when the ink
  shows a gap and the per-word reading spells exactly the same characters.
- Phone photos: page-outline perspective correction and uneven-lighting
  flattening before recognition.
- Table cells separated by a ruling line stay separate editable lines.
- Page thumbnails show inserted images.

### Added (editing)

- **Mixed-style paragraph reflow:** editing one line of a paragraph that
  contains bold/coloured/differently-sized runs re-wraps the paragraph
  while every run keeps its own font, size and colour (`reflow_line`).
  Size is never reduced unless "allow shrink" is on.

### Fixed

- Reflowed paragraphs were written with a y-up text matrix in a y-down
  frame and rendered upside down.

### Fixed (scanned charts and forms)

- Ruling lines (axes, ticks, cell borders, underlines) are removed before
  text is measured (`ocr.layout.strip_rules`): chart labels no longer get
  2-3x font sizes or a wrong face.
- Vertical text (rotated axis titles) is recognised by reading the page
  turned 90° both ways and becomes rotated editable lines; vertical boxes
  misread as horizontal text are dropped.
- Unknown fonts (glyph overlap < 0.30 with every bundled face) and digit
  labels use neutral Liberation Sans; bold only for large, clearly heavy
  text. Short lines follow the same rule.
- Editing a recognised line replaces only the glyph pixels (soft-masked
  inpainted patch rendered with margin, tinted anti-aliasing included), so
  lines, ticks and borders under/around text survive and no ghost remains.
- Minimum recognition confidence 0.6 (logo placeholders, graphics).

### Fixed (edited text keeps its formatting)

- Original embedded **subset** fonts are reused again: coverage and
  re-encoding now come from the font's /ToUnicode map
  (`font_program.tounicode_pairs/tounicode_codes/present_codes`), verified
  glyph by glyph against the embedded program; a missing `cmap` table no
  longer aborts Type0 encoding. Previously every edit swapped in a look-alike.
- Reused-font widths come from the PDF's own /Widths or /W
  (`edit_engine._pdf_char_widths`), so wrapping and overflow are measured
  with the advances viewers actually use.
- Paragraph reflow wraps inside the paragraph's own column (not the
  page-wide widest line) and no longer shrinks text: `edit_block` defaults
  to `push_down`; auto-fit only when asked (`EditController.allow_shrink`).
- Edit mode: a line that grows past its single-style paragraph's width
  reflows the paragraph (`EditEngine.paragraph_for_line_edit`); mixed-style
  paragraphs keep per-run line edits so no run loses its style.
- WYSIWYG inline editor (`ui/fonts.py`): bundled fonts registered with Qt,
  the PDF's own embedded font loaded for the preview, weight/italic from the
  font program, Tz/Tc applied, span colour on paper white (the theme
  stylesheet had overridden it), editor sized to the real font.
- Fallback fonts are never silent: amber "⚠ Fallback font …" badge while
  typing and a "⚠ Fallback font used: X instead of Y" status after saving.
- Scanned pages: face (sans/serif/mono/Carlito), weight (stroke thickness),
  italic (glyph slant), exact size and baseline are fitted from the line's
  own glyphs (`ocr.layout._match_face`); italic variants embedded. OCR cache
  version bumped.
- `tests/test_edit_style.py`: bold heading, colour, italic, mixed-style
  line, CMYK (DeviceCMYK operator preserved), rotation, true partial
  subsets (reuse + same-family fallback), paragraph wrap without shrink,
  WYSIWYG editor, scanned-page styles.

### Changed (appearance)

- `offlinepdf/ui/theme.py` rebuilt: Fusion style in both modes, token-based
  light/dark palettes (all colour groups incl. disabled), one stylesheet for
  toolbar, menus, docks, tabs, group boxes, inputs, spin/combo arrows,
  checkboxes, sliders, progress, scrollbars, status bar and the OCR notice;
  semantic label roles (`muted`/`warning`/`error`/`notice`) replace
  hard-coded greys; arrow/check glyphs drawn to PNG (no URLs in source).
- Viewer: themed backdrop with soft page shadows; thumbnails fit landscape
  pages instead of cropping them and get a hairline edge.

### Fixed

- Opening another file no longer discards unsaved changes silently: the
  Save / Discard / Cancel prompt now runs first.
- Slides or flyers with a full-page background picture and some real text
  are no longer treated as scans (only near-zero real text counts).

### Added (Auto-Make Editable - scanned PDFs)

- `offlinepdf/core/ocr/detect.py`: per-page classification (`text`,
  `scanned`, `ocr`, `blank`) from extractable-text area vs. embedded image
  area; vector-outlined pages count as scanned.
- `offlinepdf/core/ocr/preprocess.py`: grayscale, projection-profile
  deskew (±15°, light-on-dark aware), median denoise, CLAHE contrast,
  optional adaptive binarisation; returns the affine back to the page.
- `offlinepdf/core/ocr/pipeline.py`: render at 300 DPI, recognise at
  150 DPI (measured best for the bundled PP-OCR models; tiny text retried
  at full resolution), map boxes back, drop lines over real text.
- `offlinepdf/core/ocr/layout.py`: segment merging into lines, calibrated
  size/baseline, text/background colour sampling, bold guess, tight ink
  box for covering, Liberation Sans/Serif/Mono matching by rendered-shape
  overlap, Noto fonts for Devanagari/Arabic/Hebrew/CJK.
- `offlinepdf/core/ocr/textlayer.py`: invisible (Tr 3) embedded-font text
  per line in `/OPDFOcr` marked content; `reveal_changed` covers the
  original pixels (flat colour or OpenCV-inpainted patch clipped to the
  line) and makes edited lines visible; `strip_unedited` for exports
  without a searchable layer; `make_page_editable` is one undo step.
- `offlinepdf/core/ocr/cache.py`: on-disk result cache keyed by page
  appearance + options.
- `EditEngine._post_commit`: OCR lines revealed inside the same undo step
  as the line/paragraph edit.
- `offlinepdf/ui/ocr_controller.py`: `MakeEditableController` - cancellable
  worker-thread queue, visible page first, private document copy.
- `offlinepdf/ui/ocr_review.py`: amber highlight of unedited lines below
  the confidence threshold (View ▸ Highlight Uncertain OCR Text).
- `offlinepdf/ui/ocr_settings.py`: Tools ▸ OCR Settings… (auto on/off,
  language, DPI, review threshold, searchable layer, clean-up steps,
  clear cache).
- `offlinepdf/ui/main_window.py`: notice bar with progress/Cancel, idle
  page scan on open, Tools ▸ Make Page Editable (Ctrl+Shift+E), and Edit
  mode double-click on a scanned page recognises it and opens the editor.
- `offlinepdf/ui/viewer.py`: multiple canvas delegates.
- `tests/test_ocr_editable.py`: detection, preprocessing, layout, layer,
  reveal/undo, export stripping, cache, recognise-edit-save-reopen on
  straight and skewed scans, mixed pages, window auto-run and cancel.

### Added (Images)

- `offlinepdf/core/image_ops.py`: inserted-image engine.
  - `load_image_source()` accepts PNG, JPG/JPEG and WebP (WebP and
    EXIF-rotated images are normalised; plain JPEGs are kept byte-for-byte).
  - `ImageItem` (frozen) / `ImageLayer` hold position, size, rotation,
    flips, non-destructive crop, opacity, border, rounded corners and
    above/behind-content stacking in view-space points.
  - `ImageLayerCommand`: cheap undoable layer swaps (mergeable for nudges
    and slider runs); `Command.mutates_document = False` lets the window
    repaint without re-rendering pages.
  - `bake_images()` writes each image as an `/OPDFImage` marked-content
    block with the exact view->PDF matrix (CropBox offset and /Rotate
    included); `lift_images()` turns them back into editable items on open.
  - Export compression: downscale above `max_px` (default 3000 px), JPEG
    pass-through or re-encode at the chosen quality, lossless Flate + SMask
    for PNG/WebP with transparency; one embedded copy per source.
- `PdfSession.images` + `export_bytes()`: save, Save As and autosave bake the
  layer into a copy; the in-memory document stays unbaked and editable.
- `offlinepdf/ui/image_tool.py`: on-canvas tool - select, move (snaps to
  page edges/centre and other images; Alt disables; drag across pages),
  corner/side resize (corner keeps ratio; Shift or the lock toggle frees
  it), free rotation handle (Shift snaps to 15°), crop mode (double-click,
  handles + frame pan, Enter/Esc), keyboard (Delete/Backspace, arrows,
  Shift+arrows, Ctrl+D), copy/cut/paste incl. system clipboard images,
  drag-and-drop of image files onto a page.
- `offlinepdf/ui/image_panel.py`: "Image" dock with exact X/Y/W/H/angle,
  page, rotate 90°/flip, crop buttons, opacity, border, corners, arrange
  and document-wide export quality.
- `offlinepdf/ui/viewer.py`: `CanvasDelegate` hook (paint below/above page
  content, first refusal on mouse events) and transparent page renders
  for images placed behind text.
- `offlinepdf/ui/main_window.py`: Insert ▸ Add Image… (Ctrl+Shift+I, page
  picker), Edit ▸ Cut/Copy/Paste/Duplicate/Delete Image, image drops.
- `config.py`: `image_max_px`, `image_jpeg_quality`, `image_png_lossless`.
- `tests/test_images.py`: sources, restacking, undo/merge, pixel-exact
  placement on 0/90/180/270° pages, crop/flip/opacity/border/corners,
  behind-text order, compression, save/reopen lifting, repeated save
  stability, encryption, recovery and offscreen UI tool tests.

### Added (M6 - OCR)

- `offlinepdf/core/ocr/` package: fully offline OCR on top of
  RapidOCR+ONNX, using the bundled model packs in `assets/ocr/`.
  - `manifest.py`: `LanguagePack` (det/rec model paths + embedded key
    list), `available_languages()`, `require_language(code)`,
    `verify_assets()` with hash-checked asset manifests.
  - `engine.py`: `OcrEngine` (lazy RapidOCR client, English default),
    `ocr_page(session, page_index, language, scale=2.0)` returning
    `OcrWord` boxes in PDF point coordinates.
  - 8 language packs vendored and verified (English, Chinese, Latin
    (multi), Cyrillic, Devanagari, Arabic, Japanese, Korean) with
    SHA-256 pinned in `assets/ocr/manifest.json`.
  - No runtime downloads: the ONNX models ship inside the app assets.
- `offlinepdf/ui/ocr_controller.py`: `OcrController` running recognition
  off the UI thread, plus `line_ordered()` for reading-order text.
- `offlinepdf/ui/main_window.py`: new Tools > "Recognize Text (OCR)…"
  action (Ctrl+Shift+R) recognizing the visible page and surfacing a
  text preview in the status bar. The action is disabled without a
  document and while a job is running.
- `tests/test_ocr.py`: language-pack discovery/verification, real-page
  recognition on the corpus, PDF-point coordinate check, PNG input,
  reading-order sorting and controller launch semantics.
- `requirements.lock`: OCR dependencies pinned (rapidocr-onnxruntime,
  onnxruntime, opencv-python, pyclipper, shapely==2.1.2, PyYAML, six,
  protobuf, flatbuffers).

### Fixed

- `scripts/vendor_ocr.py`: removed an unused `# noqa: S310` directive
  (ruff RUF100).

### Added (M3 - Edit mode core)

- `offlinepdf/core/edit_engine.py`: span-level text editing engine.
  - `EditEngine.can_edit(page_index, span_id)`, `decision_for(...)`,
    `preview`, `edit_line(page_index, span_id, text)` returning
    `EditResult` (ok, warning, decision, path, badge, noop, overflow,
    original_width, new_width, old_text, new_text, duration_ms).
  - Original-font reuse: byte-wise compare of the embedded font program
    with the edit text; identical coverage keeps the original font and
    rewrites only the content stream ("original font reused (100%)").
  - Same-family / same-weight fallback: when glyphs are missing from a
    subset or the style deviates, a full or partial subset of the
    found family is embedded and the original font is unlinked from
    the affected page resources.
  - Style preservation: target weight/italic derive from the original
    font program (not the UI profile); size, color and transformation
    matrix are carried over to replacements.
  - Structure preservation: cid-fallbacks, kerning/width arrays, mark
    (cmap) dictionaries and content stream ADBE/unti xobjects are
    preserved when editing the matching PDFs.
  - RTL support: MuPDF extracts right-to-left runs in logical order
    while the content stream stores them in visual order.
    `_attribute_unmapped` maps extraction-only characters back to their
    sole owning show-text event, and `_visual_rtl` reorders the logical
    replacement text into the visual order the stream expects, so an
    edited Hebrew line still reads correctly and extracts back to the
    typed text.
  - Refusals are explicit and safe (Type3 drawn text, outlined
    (no text) pages, unmapped content) - the document is left
    unchanged and a human-readable warning is returned.
- `tests/test_edit_roundtrip.py`: spec section-5 round-trip tests
  (a) same-text pixel diff <= 0.5%, (b) "original font reused (100%)"
  badge, (c) missing-glyph fallback with reported confidence and
  matching size/style/color, (d) rotated, CMYK, CID (Identity-H),
  justified, mixed Latin+Devanagari and Hebrew RTL text, (e) Type3 and
  outlined refusal, (f) qpdf/pikepdf + PyMuPDF reopen, toc/links/
  metadata intact, extracted text equals typed text, (g) encrypted
  password required / re-opens with password / stays encrypted, signed
  document emits a signature warning. Every edit writes before/after
  PNGs to `tests/output/`.
- `tests/conftest.py`: `edit_corpus` session fixture rebuilding the
  corpus when source assets change.

### Fixed

- `PdfSession.to_bytes()` now preserves document encryption
  (`encryption=True` when the source is encrypted). Previously a saved
  encrypted document came back unencrypted, contradicting the engine
  warning.
- `PdfSession.refresh_view()` re-authenticates the rebuilt PyMuPDF
  view with the session password so encrypted documents continue to
  render after an edit. (PyMuPDF 1.28.2 has no `password=` on `open()`.)
- `PdfSession._verify_file()` and `verify_roundtrip()` pass the
  session password when reopening saves of encrypted documents.
- `scripts/make_corpus.py`:
  - `can.add_outline_entry` now called after every `bookmarkPage` so
    the corpus actually has a bookmark outline (toc is no longer empty).
  - `linkRect(contents, destination_name, Rect, relative=0)` uses the
    correct argument order.
  - pikepdf Name keys are assigned by index
    (`char_procs[pikepdf.Name("/A")] = stream`) instead of being passed
    to a dict constructor (which raised "bad cast").
  - the Type3 path pen is implemented locally as `_PDFPathPen` with
    quad-to-cubic conversion because this fontTools version ships no
    PDF path pen.

### Removed

- `pdfPathPen` dependency in `scripts/make_corpus.py` (not available in
  the installed fontTools).