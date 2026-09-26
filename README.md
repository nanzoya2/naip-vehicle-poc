# naip-vehicle-poc

NAIP航空画像から Nissan Smyrna 工場の完成車ヤードの車両台数を推定するPoC。
設計は `../NAIP車両台数分析PoC_基本設計書_v1.1.md` を参照。

Phase 0（画像認識成立性検証）は **Gate 1 通過（2026-09-26, 目視判定）**。次は Phase 1（ローカル分析処理）。

## 構成

```text
config/
  sites.yaml                     対象地点・ROI版・対象年
  roi/nissan_smyrna_v1.geojson   完成車ヤードROI（4区画 Z1〜Z4, EPSG:4326）
notebooks/phase0/
  common.py                      パス・設定・ROI読込
  01_fetch_naip.py               Planetary Computer から ROI+50m を取得 → data/raw
  02_make_annotation_template.py アノテーション用 GeoPackage を作成
  03_baseline_area_ratio.py      面積換算ベースライン → outputs/phase0/baseline
  04_evaluate.py                 GT と比較して Gate 1 判定 → outputs/phase0/evaluation
annotation/
  gt_nissan_smyrna_v1.gpkg       アノテーション（grid / cells / points_{year}）
data/raw/{site}/{year}/{source_image_id}/   image.tif（4バンド, EPSG:26916）, stac_item.json
outputs/phase0/                  推定結果・確認用画像
```

`data/` と `outputs/` は再生成可能なため Git 管理外。`annotation/` は手作業の成果なので管理対象。

## 実行

```bash
python -m venv .venv && . .venv/bin/activate   # または uv venv
pip install -r requirements.txt

python notebooks/phase0/01_fetch_naip.py            # 取得（既存はスキップ）
python notebooks/phase0/02_make_annotation_template.py   # 初回のみ（既存gpkgは上書きしない）
python notebooks/phase0/03_baseline_area_ratio.py
python notebooks/phase0/04_evaluate.py              # アノテーション後
```

## アノテーション手順（QGIS）

1. QGIS で以下を読み込む
   - `data/raw/NISSAN_SMYRNA/{year}/*/image.tif`（1年ずつ）
   - `annotation/gt_nissan_smyrna_v1.gpkg` の `cells` と `points_{year}`
   - `config/roi/nissan_smyrna_v1.geojson`（区画境界の確認用）
2. `cells` のスタイルを「塗りなし・太枠」、ラベルを `cell_id` / `split` にする
3. 画像のレンダリングは **リサンプリング＝最近傍**、縮尺は 1:300〜1:500 程度（1台 ≈ 8×3px）
4. `points_{year}` を編集モードにして、**セル内かつROI区画内の車両**に1台1点を打つ
   - 点は車体の中心付近。セル境界をまたぐ車両は中心がある側のセルに打つ
   - 車運車上・建屋の陰で判別できない車両は打たない（判断に迷ったら `note` に記入）
   - 影は車両に含めない。影だけで車体が見えない場合（2021年の暗色車）は、影の形で判断して打ってよい
5. セルを打ち終えたら `cells` の `done_{year}` を 1 にする（0台のセルも 1）
6. 保存（編集の保存）→ `04_evaluate.py`

作業量の目安（ベースライン推定から）：対象22セルで 2018年 約1,400台、2021年 約1,200台、2023年 約140台。
まず **2018年** から着手し、Gate 1 の一次判定を行うのが効率的。
アノテーション誤差の把握のため、可能なら数セルを2名で数える（§22.1）。

### セル設計

- 50m四方（設計書は100m。2018年の満車時に1セル数百台となるため縮小）
- ROI内が95%以上のセルから区画ごとに層化抽出（Z1:5, Z2:2, Z3:9, Z4:6 = 22セル）
- 150mブロック単位で train / eval に空間分割（eval 7セル）

## Gate 1 判定（2026-09-26）

**判定：通過（Phase 1 へ進む）**

- 判定方法：3年分の推定結果（`outputs/phase0/baseline/{year}/overview_occupancy.png` と crops）を原画像と目視で比較し、
  概ね正しい台数をカウントできていると評価した
- 設計書 §22.3 の定量基準（eval セルの Count Error% ±15%）による評価は実施していない。
  点アノテーションと `04_evaluate.py` は、定量評価が必要になった時点で使えるよう残している
- 採用手法：面積換算ベースライン（`baseline-area-v0`）。学習ベース手法（§13.2 優先2〜3）は現時点では不要と判断
- 既知の問題（「途中結果」参照）は Phase 1 以降の改善課題として持ち越す

## Phase 0 途中結果（2026-09-23）

### 取得できる画像

| 年 | 撮影日 | GSD | 備考 |
| --- | --- | --- | --- |
| 2012 / 2014 / 2016 | - | 1.0m | 対象外（1台 ≈ 4.5×1.8px） |
| 2018 | 2018-07-29 | 0.6m | ヤードほぼ満車。車両は1台ずつ概ね判別可能 |
| 2021 | 2021-12-03 | 0.6m | 冬撮影。影が長く隣接車両の影が融合 |
| 2023 | 2023-05-25 | 0.6m | ヤードほぼ空 |

ROIは DOQQ `3508605_nw` 1枚に収まる（工場全体は `3508604_ne` と跨る）。
3年分の比較: `outputs/phase0/yard_compare_2018_2021_2023.png`

### 面積換算ベースライン（未校正の概算）

| 年 | 推定台数 | Z1 | Z2 | Z3 | Z4 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2018 | 5,778 | 344 | 256 | 3,180 | 1,999 |
| 2021 | 4,877 | 282 | 143 | 2,465 | 1,988 |
| 2023 | 1,612 | 2 | 146 | 954 | 510 |

1台あたり占有面積は「単独車両と思われる連結成分の面積中央値」で自己校正しており、
**GTによる校正前のため精度は未評価**。確認用画像は `outputs/phase0/baseline/{year}/`。

既知の問題：

- 2023年 Z2 は空だが、舗装の黒いしみ状パターンを車両と誤検出（約150台分）
- 暗色舗装（Z2, 2021年以降のZ4）は区画線と白い車の輝度が同程度で、形状（縦方向の厚み）でのみ区別している
- 2021年は影を含めた占有面積で数えるため、密集部で影が隣の車に重なると過小になりやすい

### 設計書への反映候補

- `target_years: [2018, 2021, 2023]`
- ROI は画像が約6°傾いているため、長方形ではなく斜めの多角形で定義（v1 は画素座標で手作業定義。QGISで要確認）
- 2018年は個体の判別がかなりできるため、小物体検出（§13.2「参考」）も比較対象に上げる価値がある
- 評価セルは 100m → 50m
