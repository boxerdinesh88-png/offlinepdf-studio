"""Inserted images: sources, layer/commands, bake/lift round trips, UI tool."""

from __future__ import annotations

import io

import numpy as np
import pikepdf
import pymupdf
import pytest
from PIL import Image

from offlinepdf.core.document import PdfSession
from offlinepdf.core.image_ops import (
    ExportOptions,
    ImageError,
    ImageItem,
    ImageLayer,
    ImageLayerCommand,
    item_matrix,
    load_image_source,
    mat_apply,
)


def _png(size=(200, 100), split=True, alpha=255) -> bytes:
    """Red image whose right half is blue (so crops/flips are visible)."""
    img = Image.new("RGBA", size, (255, 0, 0, alpha))
    if split:
        img.paste((0, 0, 255, alpha), (size[0] // 2, 0, size[0], size[1]))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _encoded(fmt: str, size=(64, 32), **kw) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 160, 0)).save(buf, fmt, **kw)
    return buf.getvalue()


def _doc(rotation=0, pages=1, text="Hello images") -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=300, height=400)
        page.insert_text((20, 40), text, fontsize=14)
        page.set_rotation(rotation)
    return doc.tobytes()


def _pixels(data: bytes, page=0) -> np.ndarray:
    pix = pymupdf.open(stream=data)[page].get_pixmap()
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _reds(arr):
    return (arr[:, :, 0] > 200) & (arr[:, :, 2] < 120)


def _blues(arr):
    return (arr[:, :, 2] > 200) & (arr[:, :, 0] < 120)


def _same_items(left, right) -> bool:
    """Equal apart from the source key (a lifted PNG is re-encoded)."""
    return [it.with_(source="") for it in left] == [it.with_(source="") for it in right]


def _session_with(item_kwargs: dict, data: bytes | None = None, **doc_kw) -> PdfSession:
    session = PdfSession.open_bytes(_doc(**doc_kw))
    source = session.images.add_source(load_image_source(data or _png()))
    defaults = {"id": "img1", "page": 0, "source": source.key, "cx": 150, "cy": 200, "width": 100, "height": 50}
    defaults.update(item_kwargs)
    session.history.push(ImageLayerCommand("insert", [], [ImageItem(**defaults)]))
    return session


# ---------------------------------------------------------------------- #
# sources
# ---------------------------------------------------------------------- #
def test_png_source_detects_alpha():
    assert load_image_source(_png()).has_alpha is False
    assert load_image_source(_png(alpha=128)).has_alpha is True


def test_jpeg_kept_byte_for_byte():
    data = _encoded("JPEG", quality=80)
    source = load_image_source(data)
    assert source.fmt == "jpeg" and source.data == data
    assert (source.width, source.height) == (64, 32)


def test_webp_converted_to_png():
    source = load_image_source(_encoded("WEBP"))
    assert source.fmt == "png"
    assert source.data.startswith(b"\x89PNG")


def test_unsupported_and_garbage_rejected():
    with pytest.raises(ImageError):
        load_image_source(b"definitely not an image")
    with pytest.raises(ImageError):
        load_image_source(_encoded("GIF"))


# ---------------------------------------------------------------------- #
# geometry, layer and commands
# ---------------------------------------------------------------------- #
def test_item_matrix_maps_box_corners():
    item = ImageItem(id="a", page=0, source="s", cx=100, cy=100, width=100, height=50, rotation=90)
    corners = [mat_apply(item_matrix(item), x, y) for x, y in ((0, 0), (100, 0), (100, 50))]
    expected = [(125, 50), (125, 150), (75, 150)]
    for (x, y), (ex, ey) in zip(corners, expected, strict=True):
        assert abs(x - ex) < 1e-9 and abs(y - ey) < 1e-9


def test_restack_crosses_page_content():
    layer = ImageLayer()
    a = ImageItem(id="a", page=0, source="s", cx=0, cy=0, width=1, height=1)
    b = a.with_(id="b")
    layer.set_items([a, b])
    layer.set_items(layer.restacked("b", "backward"))
    assert [it.id for it in layer.on_page(0)] == ["b", "a"]
    layer.set_items(layer.restacked("b", "backward"))  # bottom of above -> behind content
    assert layer.get("b").above_content is False
    layer.set_items(layer.restacked("b", "forward"))
    assert layer.get("b").above_content is True
    layer.set_items(layer.restacked("a", "back"))
    assert layer.on_page(0)[0].id == "a" and layer.get("a").above_content is False
    layer.set_items(layer.restacked("a", "front"))
    assert layer.on_page(0)[-1].id == "a" and layer.get("a").above_content is True


def test_layer_commands_undo_redo_and_merge():
    session = _session_with({})
    item = session.images.get("img1")
    for step in range(3):
        moved = item.with_(cx=item.cx + step + 1)
        session.history.push(
            ImageLayerCommand("move", session.images.items, [moved], merge_key="nudge")
        )
    assert session.images.get("img1").cx == item.cx + 3
    assert session.history.last_command.mutates_document is False
    session.history.undo()  # one undo reverts the merged nudge run
    assert session.images.get("img1").cx == item.cx
    session.history.undo()
    assert session.images.items == ()
    session.history.redo()
    session.history.redo()
    assert session.images.get("img1").cx == item.cx + 3
    assert session.is_dirty


# ---------------------------------------------------------------------- #
# export: exact placement
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_export_position_size_on_rotated_pages(rotation):
    session = _session_with({"cx": 100, "cy": 120}, rotation=rotation)
    arr = _pixels(session.export_bytes())
    # 100x50 pt box centred at (100, 120) in the displayed page.
    assert _bbox(_reds(arr) | _blues(arr)) == (50, 95, 150, 145)
    assert _bbox(_reds(arr))[0] == 50 and _bbox(_blues(arr))[2] == 150


def test_export_rotation_flip_crop_opacity():
    session = _session_with(
        {"cx": 100, "cy": 100, "rotation": 90, "flip_h": True, "crop": (0.0, 0.0, 0.5, 1.0),
         "width": 50, "opacity": 0.5}
    )
    arr = _pixels(session.export_bytes())
    pink = (arr[:, :, 0] > 240) & (abs(arr[:, :, 1].astype(int) - 128) < 12)
    assert _bbox(pink) == (75, 75, 125, 125)  # 50x50 cropped red half, rotated
    assert not _blues(arr).any()  # blue half cropped away


def test_border_and_rounded_corners_render():
    session = _session_with({"border_width": 4, "border_color": (0, 200, 0), "corner_radius": 20})
    arr = _pixels(session.export_bytes())
    green = (arr[:, :, 1] > 150) & (arr[:, :, 0] < 80) & (arr[:, :, 2] < 80)
    assert _bbox(green) == (100, 175, 200, 225)
    corner = arr[176, 101]
    assert corner.min() > 200  # rounded: the very corner is page white


def test_behind_content_keeps_text_on_top():
    big = {"cx": 150, "cy": 40, "width": 280, "height": 60}
    above = _pixels(_session_with(big).export_bytes())
    below = _pixels(_session_with({**big, "above_content": False}).export_bytes())
    dark = lambda a: ((a[:, :, 0] < 80) & (a[:, :, 1] < 80) & (a[:, :, 2] < 80)).sum()  # noqa: E731
    assert dark(above) == 0  # opaque image hides the text
    assert dark(below) > 20  # text painted over the image


def test_export_does_not_touch_session_document():
    session = _session_with({})
    before = session.to_bytes()
    session.export_bytes()
    assert session.to_bytes() == before
    assert pymupdf.open(stream=before)[0].get_images() == []


# ---------------------------------------------------------------------- #
# compression
# ---------------------------------------------------------------------- #
_KEEP: list[pikepdf.Pdf] = []  # keep opened PDFs alive while objects are inspected


def _embedded(data: bytes) -> list[pikepdf.Object]:
    pdf = pikepdf.open(io.BytesIO(data))
    _KEEP.append(pdf)
    return [
        obj for obj in pdf.objects
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name.Image
        and "/SMask" not in obj and obj.get("/ColorSpace") != pikepdf.Name.DeviceGray
    ]


def test_large_images_downscaled_to_max_px():
    session = _session_with({}, data=_png(size=(4000, 1000), split=False))
    session.images.options = ExportOptions(max_px=3000)
    (image,) = _embedded(session.export_bytes())
    assert (int(image.Width), int(image.Height)) == (3000, 750)


def test_jpeg_passthrough_and_quality_reencode():
    data = _encoded("JPEG", size=(400, 200), quality=95)
    session = _session_with({}, data=data)
    (image,) = _embedded(session.export_bytes())
    assert image.Filter == pikepdf.Name.DCTDecode and image.read_raw_bytes() == data
    session.images.options = ExportOptions(jpeg_quality=40)
    (image,) = _embedded(session.export_bytes())
    assert image.read_raw_bytes() != data and len(image.read_raw_bytes()) < len(data)


def test_png_lossless_with_smask():
    session = _session_with({}, data=_png(alpha=100))
    pdf = pikepdf.open(io.BytesIO(session.export_bytes()))
    images = [o for o in pdf.objects if isinstance(o, pikepdf.Stream) and "/SMask" in o]
    assert len(images) == 1 and images[0].Filter == pikepdf.Name.FlateDecode


def test_duplicates_share_one_embedded_image():
    session = _session_with({})
    item = session.images.get("img1")
    copies = [item, item.with_(id="b", page=1), item.with_(id="c", cx=40)]
    session2 = PdfSession.open_bytes(_doc(pages=2))
    session2.images.add_source(session.images.sources[item.source])
    session2.images.set_items(copies)
    assert len(_embedded(session2.export_bytes())) == 1


# ---------------------------------------------------------------------- #
# save + reopen
# ---------------------------------------------------------------------- #
def test_save_reopen_lifts_items_for_editing(tmp_path):
    session = _session_with(
        {"rotation": 33.5, "flip_v": True, "crop": (0.1, 0.2, 0.9, 0.8), "opacity": 0.7,
         "border_width": 2, "border_color": (10, 20, 30), "corner_radius": 6}
    )
    original = session.images.items
    target = session.save_as(tmp_path / "out.pdf")
    reopened = PdfSession.open(target)
    assert _same_items(reopened.images.items, original)
    assert not reopened.is_dirty
    # the lifted document no longer contains the baked block
    assert pymupdf.open(stream=reopened.to_bytes())[0].get_images() == []
    assert "Hello images" in reopened.mupdf[0].get_text()


def test_repeated_save_cycles_are_stable(tmp_path):
    session = _session_with({"above_content": True})
    path = tmp_path / "cycle.pdf"
    session.save_as(path)
    first = path.read_bytes()
    for _ in range(3):
        reopened = PdfSession.open(path)
        reopened.save_as(path, overwrite=True)
        reopened.close()
    assert abs(len(path.read_bytes()) - len(first)) < 200  # no wrapper/content build-up
    assert np.array_equal(_pixels(first), _pixels(path.read_bytes()))


def test_stacking_order_survives_reopen(tmp_path):
    session = _session_with({})
    item = session.images.get("img1")
    behind = item.with_(id="under", above_content=False, cx=120)
    top = item.with_(id="top", cx=180)
    session.images.set_items([item, behind, top])
    reopened = PdfSession.open(session.save_as(tmp_path / "order.pdf"))
    assert [it.id for it in reopened.images.on_page(0)] == ["under", "img1", "top"]


def test_encrypted_document_stays_encrypted(edit_corpus, tmp_path):
    session = PdfSession.open(edit_corpus / "encrypted.pdf", password="secret")
    source = session.images.add_source(load_image_source(_png()))
    session.images.set_items(
        [ImageItem(id="e", page=0, source=source.key, cx=100, cy=100, width=50, height=25)]
    )
    target = session.save_as(tmp_path / "enc.pdf")
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(target)
    assert len(PdfSession.open(target, password="secret").images.items) == 1


def test_recovery_snapshot_contains_images(tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(_doc())
    session = PdfSession.open(path)
    session.recovery = type(session.recovery)(tmp_path / "recovery")
    source = session.images.add_source(load_image_source(_png()))
    session.images.set_items(
        [ImageItem(id="r", page=0, source=source.key, cx=100, cy=100, width=50, height=25)]
    )
    assert session.write_recovery()
    restored = PdfSession.open_bytes(session.recovery.read(path), source_path=path)
    assert [it.id for it in restored.images.items] == ["r"]


# ---------------------------------------------------------------------- #
# UI tool (offscreen)
# ---------------------------------------------------------------------- #
@pytest.fixture
def window(qapp, tmp_path):
    from offlinepdf.config import Settings
    from offlinepdf.ui.main_window import MainWindow

    win = MainWindow(Settings(tmp_path / "settings.json"))
    path = tmp_path / "two.pdf"
    path.write_bytes(_doc(pages=2))
    assert win.open_path(path, restore_recovery=False)
    yield win
    if win.session is not None:
        win.session.mark_dirty = lambda: None  # type: ignore[method-assign]
        win.session._dirty = False
    win.close()


def _tool(win):
    return win._image_controller


def test_tool_insert_centres_and_selects(window):
    tool = _tool(window)
    assert tool.insert_bytes(_png(size=(2000, 1000)), page=1)
    item = tool.selected
    assert item is not None and item.page == 1
    assert (item.cx, item.cy) == (150, 200)
    assert item.width <= 300 * 0.6 + 1e-6 and abs(item.width / item.height - 2) < 1e-6
    assert window.act_copy.isEnabled() and window.act_duplicate.isEnabled()


def test_tool_actions_are_undoable(window):
    tool = _tool(window)
    history = window.session.history
    tool.insert_bytes(_png())
    start = tool.selected
    tool.nudge(1, 0)
    tool.nudge(1, 0)
    tool.seal_merge()
    tool.nudge(0, 10)
    assert (tool.selected.cx, tool.selected.cy) == (start.cx + 2, start.cy + 10)
    history.undo()
    assert tool.selected.cy == start.cy and tool.selected.cx == start.cx + 2
    history.undo()
    assert tool.selected.cx == start.cx
    tool.rotate_by(90)
    tool.flip("h")
    tool.update_selected("opacity", opacity=0.25)
    tool.restack("back")
    assert tool.selected.rotation == 90 and tool.selected.flip_h
    assert tool.selected.opacity == 0.25 and not tool.selected.above_content
    for _ in range(4):
        history.undo()
    assert tool.selected == start
    tool.duplicate_selected()
    assert len(window.session.images.items) == 2
    tool.delete_selected()
    assert len(window.session.images.items) == 1
    history.undo()
    assert len(window.session.images.items) == 2


def test_tool_copy_paste_and_move_to_page(window):
    tool = _tool(window)
    tool.insert_bytes(_png())
    tool.update_selected("rotate", rotation=30.0)
    original = tool.selected
    assert tool.copy_selected()
    assert tool.paste(page=1)
    pasted = tool.selected
    assert pasted.id != original.id and pasted.page == 1 and pasted.rotation == 30.0
    tool.move_to_page(0)
    assert tool.selected.page == 0
    window.session.history.undo()
    assert tool.selected.page == 1


def test_tool_set_box_respects_aspect_lock(window):
    tool = _tool(window)
    tool.insert_bytes(_png())
    item = tool.selected
    tool.set_box(width=item.width * 2)
    assert abs(tool.selected.height - item.height * 2) < 1e-6
    assert abs(tool.selected.cx - tool.selected.width / 2 - (item.cx - item.width / 2)) < 1e-6
    tool.lock_aspect = False
    tool.set_box(height=10)
    assert tool.selected.height == 10 and abs(tool.selected.width - item.width * 2) < 1e-6
    tool.set_box(x=5, y=6, rotation=200)
    assert tool.selected.rotation == -160


def test_resize_and_crop_geometry():
    from offlinepdf.ui.image_tool import _crop_drag, _full_center, _resize

    item = ImageItem(id="a", page=0, source="s", cx=100, cy=100, width=100, height=50, rotation=30)
    # Drag the bottom-right corner: the top-left corner must stay put.
    tl = mat_apply(item_matrix(item), 0, 0)
    bigger = _resize(item, (1, 1), mat_apply(item_matrix(item), 150, 60), keep_ratio=True)
    assert abs(bigger.width / bigger.height - 2) < 1e-9
    assert all(abs(a - b) < 1e-9 for a, b in zip(mat_apply(item_matrix(bigger), 0, 0), tl, strict=True))
    # Crop the left edge to the middle: the full image stays fixed on the page.
    center = _full_center(item)
    cropped = _crop_drag(item, (-1, 0), mat_apply(item_matrix(item), 50, 25), None)
    assert abs(cropped.crop[0] - 0.5) < 1e-9 and abs(cropped.width - 50) < 1e-9
    assert all(abs(a - b) < 1e-9 for a, b in zip(_full_center(cropped), center, strict=True))
    # Flipped images crop the *visible* side.
    flipped = item.with_(flip_h=True)
    cropped = _crop_drag(flipped, (-1, 0), mat_apply(item_matrix(flipped), 50, 25), None)
    assert abs(cropped.crop[2] - 0.5) < 1e-9 and cropped.crop[0] == 0.0


def test_ui_save_and_reopen_round_trip(window, tmp_path):
    tool = _tool(window)
    tool.insert_bytes(_png(), page=1)
    tool.update_selected("style", opacity=0.6, corner_radius=8.0)
    expected = window.session.images.items
    target = tmp_path / "saved.pdf"
    window.session.save_as(target)
    assert window.open_path(target, restore_recovery=False)
    assert _same_items(window.session.images.items, expected)


def test_thumbnail_shows_inserted_image(window):
    from PySide6.QtCore import Qt

    tool = _tool(window)
    tool.insert_bytes(_png(size=(400, 200), split=False), page=0)  # solid red
    model = window._thumbs.model()
    window._thumbs.refresh()
    pixmap = model.data(model.index(0), Qt.ItemDataRole.DecorationRole)
    image = pixmap.toImage()
    reds = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if (c := image.pixelColor(x, y)).red() > 200 and c.green() < 80 and c.blue() < 80
    )
    assert reds > 50
