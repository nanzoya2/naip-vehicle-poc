import numpy as np
import pytest

from detector.tiling import make_tiles


@pytest.mark.parametrize("h,w,core,overlap", [(1077, 1734, 480, 80), (100, 100, 480, 80), (960, 480, 480, 160)])
def test_cores_partition_image(h, w, core, overlap):
    """Core領域は重複なく画像全体を覆う（二重計上・取りこぼしがない）"""
    cover = np.zeros((h, w), int)
    for t in make_tiles(h, w, core, overlap):
        cover[t.core_in_image] += 1
    assert (cover == 1).all()


def test_window_contains_core_with_overlap():
    for t in make_tiles(1077, 1734, 480, 80):
        rs, cs = t.core_in_tile
        assert rs.start >= 0 and cs.start >= 0
        assert rs.stop <= t.window.height and cs.stop <= t.window.width
        # 画像の縁以外では Core の外側に overlap 分の文脈がある
        if t.core.row_off > 0:
            assert rs.start == 80
        if t.core.col_off > 0:
            assert cs.start == 80
