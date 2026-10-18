# Design decisions

This file records deliberate architecture and behaviour choices. Each
entry links to the milestone and the reasoning behind the choice, so
future work does not silently reverse a decision.

## Editing

- **The span is the edit unit.** Users edit rendered text spans, not raw
  PDF tokens. A span is mapped to one or more show-text tokens in the
  content stream; the engine rewrites those tokens and re-measures the
  span. Edits that span multiple fonts are handled span by span.
- **CFF (OTTO) fonts are rejected for embedding; simple TTF/woff are
  embedded.** CFF parses/interoperates poorly with pikepdf's font
  writer and with downstream renderers; embedded TrueType outline fonts
  round-trip reliably in all tested viewers (MuPDF, qpdf, browser
  renderers).
- **Target weight/italic come from the original font program, not the
  UI style profile.** The style-profile matching is a retrieval hint;
  the resolved @font-face is clamped to the weight and italic state the
  original document actually uses, so "replacing Helvetica-Bold" cannot
  produce a regular-weight replacement.
- **Same-text edits leave the document untouched (noop).** A no-op edit
  stores the same text and returns without touching fonts or streams;
  the pixel test (a) asserts zero-or-epsilon page diff for this path.
- **Refusals are explicit, not best-effort.** Type3 (drawn) fonts,
  pages with no text, and streams whose characters cannot be attributed
  to a show-text event return a structured, human-readable refusal and
  leave the document byte-identical. No silent partial edits.
- **RTL writes visual order back to the stream.** MuPDF extracts
  right-to-left runs in logical order (bidi reordering) while the
  content stream stores them in visual order. `_attribute_unmapped`
  first attributes extraction-only characters to their sole owning
  show-text event; when a line still has unmapped characters because a
  pure-RTL run was reordered, the engine maps them and writes the
  logical replacement text reordered into visual order via
  `_visual_rtl`. The extracted text after an RTL edit therefore equals
  the typed text, and the rendered glyph order matches the original
  document. (The original stream is already visual, so a same-text RTL
  edit is a noop - nothing needs rewriting.)
- **Weight/style fallback prefers the same family.** When a glyph is
  missing from a subset, the resolver first tries the same family at a
  different weight/width before switching families, and only falls back
  to a general (variable/static) font of any cover when no same-family
  option exists. Confidence and a non-"100%" badge signal the fallback
  to the user.
- **The badge is user-facing truth for the edit path.** "Original font
  reused (100%)" appears only for byte-identical original reuse with
  no embedded copy; every other path reports the embedded font's
  family/weight and a confidence value < 1.0.

## Persistence and validation

- **Encryption is preserved on save.** `PdfSession.to_bytes()` passes
  `encryption=True` when the source is encrypted and `None` otherwise,
  and save-time verification reopens with the session password, so an
  encrypted document stays encrypted after Save As instead of silently
  losing its protection.
- **Undo/redo compare rendered pixels and text, not raw bytes.** A
  rebuild can be byte-non-identical while being visually identical;
  the round-trip tests therefore compare pixel diffs and extracted
  text rather than file bytes.
- **qpdf (via pikepdf) is the validation gate.** `verify_roundtrip()` /
  `_verify_file()` reopen the serialized state with strict parsing and
  compare page count; syntax errors only warn. pikepdf's `check_pdf_syntax`
  was verified to catch the structural bugs the corpus builders would
  otherwise have shipped (e.g. pre-fix silhouette AcroForm metadata).

## Corpus and testing

- **The corpus is rebuilt from source assets, not committed PDFs.**
  `scripts/make_corpus.py` regenerates every fixture; `tests/conftest.py`
  rebuilds on fixture staleness so future edits to the builders are
  reflected in CI-style runs and never go stale silently.
- **Every round-trip test writes before/after PNGs to `tests/output/`.**
  Visual inspection is a first-class assertion alongside the pixel-diff
  ratio; the PNGs are the human-verifiable artifact of the spec.
- **CFF submissions rejected at runtime are covered by test.** The
  outlined (no text) and Type3 (drawn) refusals are exercised in the
  round-trip suite rather than treated as write-only outcomes.
## Style preservation on edit

- **ToUnicode is the source of truth for subset fonts.** Subset programs
  often have no Unicode cmap, so coverage and re-encoding use the inverse of
  /ToUnicode, keeping only codes whose glyph outline really exists (several
  codes may map one character; the lowest *present* code wins).
- **Widths come from /Widths or /W**, not the font program, for the same
  reason and because those are the advances viewers lay out with.
- **Reflow never shrinks by default.** Longer text wraps within the
  paragraph's own column and pushes following content down; horizontal
  squeezing (auto-fit) is opt-in.
- **Only single-style paragraphs are reflowed from a line edit.** Reflow
  rebuilds a paragraph in one style; mixed-style paragraphs stay per-run line
  edits so no run changes appearance.
- **The editor previews with the face that will be saved**: the PDF's own
  embedded font when reused, otherwise the bundled fallback, which is always
  announced with a visible warning.

## Images

- **Inserted images are an editor-side layer, baked only on export.**
  `PdfSession.images` holds frozen `ImageItem`s; moves/crops/rotations are
  tiny list swaps on the shared undo history instead of whole-document byte
  snapshots, so dragging stays instant and crop is non-destructive.
  `export_bytes()` bakes into a throwaway copy for save/autosave.
- **Geometry lives in view space** (PyMuPDF `page.rect`, y down, /Rotate
  applied). `item_matrix()` is shared by the painter and the exporter;
  export appends `~(transformation_matrix * rotation_matrix)`, verified
  pixel-exact on 0/90/180/270° pages.
- **Baked images round-trip as editable items.** Each image is one
  `/OPDFImage <</P <json-hex>>> BDC … EMC` block; overlay content is
  isolated by an `/OPDFWrap BMC q … Q EMC` wrapper. Opening a file lifts
  both back out, so repeated save/reopen cycles don't accumulate wrappers.
- **Crop is a clip path, not a pixel crop**, so it can be re-adjusted after
  reopening (cost: cropped-away pixels stay embedded, after downscaling).
- **"Behind text" means behind the page's own content stream.** On screen
  the page is rendered with a transparent background over the image; a
  page with an opaque full-page background (e.g. a scan) hides it in the
  viewer and in the PDF alike.

## Auto-Make Editable (scanned pages)

- **Recognised text is real PDF text, not a parallel editor.** Each OCR
  line becomes invisible (render mode 3) text in an embedded subset of a
  bundled font, so hit-testing, the inline editor, reflow, font fallback
  and undo are the existing M2-M4 machinery. A line only turns visible
  (cover + fill mode) once its operators change, so untouched regions keep
  the original pixels and the page looks identical.
- **Edits are detected by fingerprint, not by tracking clicks.** The
  `/OPDFOcr` block stores a hash of its normalised text operators; after
  every engine commit `reveal_changed` reveals blocks whose hash differs.
  This also catches lines moved by paragraph reflow.
- **Recognition runs at 150 DPI even though pages render at 300.** The
  bundled PP-OCRv3 detector split and clipped large type at 300 DPI while
  still reading 5 pt text at 150 DPI; 300 DPI renders are kept for deskew,
  colour sampling and inpainting.
- **No cloud OCR.** The app is offline by design (netguard); every OCR
  dependency and model is bundled.
- **Opening a scan does not mark the document dirty.** The invisible
  layer changes nothing visible; it is saved only with the user's edits or
  an explicit save.

## OCR (M6)

- **Engine choice:** RapidOCR (ONNX) selected for fully offline OCR (bundled ONNX models, no runtime network access). Tesseract requires external binaries/traineddata and more complex bundling. Models are shipped or copied locally via language-pack manager; no automatic downloading. English bundled by default.

## OCR engine version and word confidence

- **RapidOCR stays on 1.2.3.** 3.9.2 was evaluated offline (netguard on,
  the bundled PP-OCRv4 models passed explicitly): same lines, same speed,
  no better text on the sample scan, the chart or the phone photo, and its
  only gain - word boxes - is obtained without it. Upgrading would add
  dependencies to the frozen build for nothing.
- **Per-word confidence by re-reading words.** Lines below 0.92 are split
  at ink gaps; if the gaps match the recognised words one to one, each word
  crop is scored by the recogniser alone and blended with the line score
  (geometric mean), because isolated short words score low even when right.
  Otherwise the line keeps its single score.
- **Space repair may only change spaces.** Per-word readings replace the
  line text only when they spell exactly the same characters.

## Mixed-style paragraph reflow

- `reflow_line` builds one plan per original span (so each keeps its font,
  size, colour, Tz/Tc/Ts), merges plans reusing the same original font into
  one slot, and wraps all runs together in the block's own column. Embedded
  fallback fonts stay one resource per run because each subset holds only
  that run's glyphs.

## Known limitations

- The inline editor is not rotated for rotated (e.g. vertical) text; the
  edit is applied correctly but typed in a horizontal box.
- OCR can misread decorative or tiny text (e.g. "Orifice Flow Rate" read as
  "Orifice How Rate", "(°K)" dropped); such words are highlighted for review.
- Hindi (Devanagari) recognition sometimes merges words.

