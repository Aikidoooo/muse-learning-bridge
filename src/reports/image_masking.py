"""結果画面スクリーンショット内の個人情報領域マスキングモジュール。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from pydantic import BaseModel, Field


class MaskRegion(BaseModel):
    """マスキング対象の矩形領域（ピクセル座標）。"""

    x: float = Field(..., ge=0)
    y: float = Field(..., ge=0)
    width: float = Field(..., gt=0)
    height: float = Field(..., gt=0)
    label: str = Field(default="PII")


def mask_screenshot_pii(
    image_path: str | Path,
    regions: list[MaskRegion],
    *,
    output_path: str | Path | None = None,
    mode: str = "solid",
    scale_factor: float = 1.0,
) -> Path:
    """スクリーンショット内の指定領域（氏名・生徒IDなど）をマスキングして保存する。"""
    src = Path(image_path)
    if not src.exists():
        raise FileNotFoundError(f"スクリーンショットが見つかりません: {src}")

    dest = Path(output_path) if output_path else src.with_name(f"{src.stem}_masked{src.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as im:
        img = im.convert("RGB")
        img_w, img_h = img.size

        for region in regions:
            x1 = max(0, int(region.x * scale_factor))
            y1 = max(0, int(region.y * scale_factor))
            x2 = min(img_w, int((region.x + region.width) * scale_factor))
            y2 = min(img_h, int((region.y + region.height) * scale_factor))
            if x2 <= x1 or y2 <= y1:
                continue

            if mode == "blur":
                crop = img.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(radius=14))
                img.paste(crop, (x1, y1))
            else:
                draw = ImageDraw.Draw(img)
                draw.rectangle([x1, y1, x2, y2], fill=(24, 32, 46))

        img.save(dest)

    return dest
