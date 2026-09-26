"""Tile分割とCore領域（§14, §15）

Core領域は隣接Tile間で重複せず画像全体を隙間なく覆う。推論はOverlapを含むTile全体で行い、
集計にはCore領域の値のみを使うことで二重計上を防ぐ。
"""
from dataclasses import dataclass

from rasterio.windows import Window


@dataclass(frozen=True)
class Tile:
    index: int
    window: Window  # 推論に使う範囲（Core＋Overlap, 画像内にクリップ）
    core: Window    # 集計に使う範囲（画像座標）

    @property
    def core_in_tile(self) -> tuple[slice, slice]:
        """Tile配列内でのCore領域のスライス (rows, cols)"""
        r0 = self.core.row_off - self.window.row_off
        c0 = self.core.col_off - self.window.col_off
        return slice(r0, r0 + self.core.height), slice(c0, c0 + self.core.width)

    @property
    def core_in_image(self) -> tuple[slice, slice]:
        return (slice(self.core.row_off, self.core.row_off + self.core.height),
                slice(self.core.col_off, self.core.col_off + self.core.width))


def make_tiles(height: int, width: int, core_px: int, overlap_px: int) -> list[Tile]:
    tiles = []
    for r in range(0, height, core_px):
        for c in range(0, width, core_px):
            core = Window(c, r, min(core_px, width - c), min(core_px, height - r))
            r0, c0 = max(0, r - overlap_px), max(0, c - overlap_px)
            r1 = min(height, r + core.height + overlap_px)
            c1 = min(width, c + core.width + overlap_px)
            tiles.append(Tile(len(tiles), Window(c0, r0, c1 - c0, r1 - r0), core))
    return tiles
