# M2 + M3 milestone report

Status: **M2 accepted** (style detection) + **M3 implemented** (font
library, resolver, single-line in-place editing, Edit-mode UI, round-trip
tests). All gates green. Awaiting review.

## What works

### Font assets (M3.1)
- Vendored offline library under `assets/fonts/` with per-family
  `LICENSE.txt`: Liberation Sans/Serif/Mono (metric-compatible Arial/
  Times/Courier), Carlito (Calibri), Caladea (Cambria), Noto Sans
  (Latin/Cyrillic/Greek), Noto Sans Arabic, Hebrew, Devanagari, and
  NotoSansSC (CJK subset). Scripts covered: Latin, Cyrillic, Greek,
  Arabic, Hebrew, Devanagari, CJK.
- `scripts/vendor_fonts.py` vendors/distributes the library;
  `assets/fonts/manifest.json` records sources and licenses; updates to
  `THIRD_PARTY_LICENSES.md` were made when vendored. All licenses (SIL
  OFL 1.1) permit redistribution.
- `core/font_index.py` scans bundled + system fonts offline and caches
  family/weight/width/slant/metrics/cmap coverage.

### Font resolver (M3.2)
- `core/font_resolver.py` implements spec section 8 steps 1-7 in order:
  original-reuse by byte comparison -> same-family weight/width ->
  nearest-weight same-family -> general cover -> reported failure.
  Returns `FontDecision`: strategy (`reuse`/`same-family`/`nearest`/
  `synthetic`), confidence, top-3 alternatives, human-readable trace.
  Cached per document.

### Edit engine (M3.3)
- `EditEngine.can_edit / decision_for / preview / edit_line` operate on
  one span. Preferred path rewrites the content-stream text operators
  (pikepdf), preserving graphics state, clipping, marked content, the
  transformation matrix (baseline/rotation), kerning/Tc/Tw/Tz, render
  mode, opacity and the original color space.
- Full undo/redo through the command history (byte snapshot + refresh).
- Only the needed glyph subset is embedded; a correct ToUnicode keeps
  the result searchable/copyable; the extracted text equals the typed
  text in every passing case.
- Over-wide text is allowed and flagged (no reflow until M4).
- Refusals are explicit and safe: Type3 (drawn) fonts and pages with no
  text return a structured warning and leave the document untouched.
- Encrypted documents keep their encryption on save; signed documents
  return a signature warning; both were bugs found and fixed while
  writing the tests (see `CHANGELOG.md`).

### Edit-mode UI (M3.4)
- Double-click a span opens `inline_editor.py` over the text, rendered
  live with the resolved style; Enter commits, Esc cancels; typed
  characters take the caret-preceding character style.
- `edit_controller.py` drives the engine, updates the status badge
  (reused / substitute / synthetic + confidence) and the top-3
  alternatives dropdown with the decision trace. Save As writes a new
  file and never overwrites the original.

## Test table (round-trip, corpus, spec section 5)

Every test writes `tests/output/<label>_before|after.png`.

| Case | Test | Result | Evidence |
|---|---|---|---|
| a | Same-text edit, pixel diff <= 0.5% | **PASS** | ratio 0.000000 (noop, 2.5 ms) |
| b | Only covered glyphs -> original font reused (100%) | **PASS** | strategy `reuse`, badge "original font reused (100%)", path in-place `tests/output/b_basic_*.png` |
| c | Missing glyphs in subset -> fallback, confidence, size/weight/color match | **PASS** | "café latte"->"Café Crème": strategy `same-family`, confidence 0.95, badge "(0% original)"; after re-extract size mu PDF == 18.0, flags/color preserved `tests/output/c_missing_glyph_*.png` |
| d | Rotated, CMYK, CID/Identity-H, justified, mixed Latin+Devanagari, RTL | **PASS** (6/6) | extracted text == typed text in every case; RTL verified both render and extraction order `tests/output/d_*.png` |
| e | Type3 + outlined: no crash, clear warning | **PASS** | Type3 refused with drawn-font warning; outlined: no spans, engine refusal; both leave bytes unchanged `tests/output/e_*.png` |
| f | qpdf/pikepdf checks, PyMuPDF reopen, text/toc/links/metadata intact | **PASS** | `verify_roundtrip()` true; toc == `[[1,'Heading',1],[1,'Page 2',2]]`; 1 link kept; title/author/subject/keywords intact; extracted text == typed `tests/output/f_basic_*.png` |
| g | Encrypted + signed: password / signature warning | **PASS** | open without pw raises; with `secret` edits and stays encrypted (reopen: PasswordError, `is_encrypted`); signed doc edits with "signature" warning `tests/output/g_*.png` |

All round-trip tests also covered by the pixel/noop checks.

## Gates

| Gate | Result |
|---|---|
| `pytest tests` | 64 passed (52 baseline + 12 round-trip) |
| `ruff check .` | All checks passed |
| `mypy offlinepdf` | no issues, 30 files |
| `python scripts/check_offline.py` | passed (no remote URLs/CDNs) |
| `python -m offlinepdf --selftest-offline` | 19/19 passed |

## Before/after images

`tests/output/` - 24 PNGs:
`a_basic`, `b_basic`, `c_missing_glyph`, `d_basic` (cmyk+justified),
`d_cid`, `d_devanagari`, `d_rtl`, `e_outlined`, `e_type3`, `f_basic`,
`g_encrypted`, `g_signed` (each `_before` / `_after`).

## Known limitations
- **RTL**: MuPDF extracts right-to-left runs in logical order while the
  stream stores visual order. The engine writes back visual order, so
  rendering and copy/paste are correct, but mixed-direction lines with
  embedded numbers/Latin atoms were only spot-tested.
- **CFF/OTTO source fonts**: refused for embedding (simple TTF/woff
  round-trip reliably in all tested renderers). Original-CFF documents
  still edit through the reuse path.
- **Justified paragraphs**: width is preserved by reusing kerning/width
  arrays, but adding many words can shift justification spacing; real
  reflow is M4.
- **Type3 and outlined text** are refused by design (would rasterize or
  drift); the corpus tests document the refusal messages.
- **Encrypted password is held per session**; opening encrypted files
  from the UI prompts, plaintext is never written to disk.
- **System-font scan** depends on what the local machine has installed;
  bundled Noto subsets guarantee Latin/Hebrew/Devanagari/Arabic/CJK(S)
  anywhere.
- **Signed documents**: edits return a signature warning ("This
  document is digitally signed; editing invalidates the signature.");
  no attempt is made to re-sign.