"""Wave-1 audit fixes: SAP-screenshot downscale (the raw 2x capture overflowed
the vision context window and silently killed the compare) and the per-field
copy values (copy policy == display policy — TIN never leaves masked)."""
from pathlib import Path

from mdmdoc import config
from mdmdoc.sap_compare import _downscaled
from mdmdoc.server.ui import _display_field


def _png(tmp_path: Path, w: int, h: int) -> Path:
    from PIL import Image
    p = tmp_path / f"shot_{w}x{h}.png"
    Image.new("RGB", (w, h), "white").save(p)
    return p


def test_sap_screenshot_downscaled(tmp_path):
    # a 2x Retina capture (real case: 3420x1260 ≈ 4k image tokens) must shrink
    im = _downscaled(_png(tmp_path, 3420, 1260))
    assert im is not None
    assert max(im.size) <= config.VISION_MAX_SIDE
    # aspect ratio preserved
    assert abs(im.width / im.height - 3420 / 1260) < 0.02


def test_small_screenshot_untouched(tmp_path):
    assert _downscaled(_png(tmp_path, 1200, 800)) is None


def test_copy_value_tin_stays_masked():
    # the TIN public dict never carries a full 'value' — copy gets the mask
    fields = {"tin": {"present": True, "masked": "XXX-XX-0693", "type": "SSN"}}
    assert _display_field(fields, "tin") == "XXX-XX-0693"
    assert _display_field(fields, "tin_raw") == "XXX-XX-0693"


def test_copy_value_bank_full_and_bools():
    fields = {"account_number": {"present": True, "masked": "…2757", "value": "1830042757"},
              "signed": True, "bank_name": "Intrust Bank"}
    assert _display_field(fields, "account_number") == "1830042757"  # full policy value
    assert _display_field(fields, "signed") == "yes"
    assert _display_field(fields, "bank_name") == "Intrust Bank"
