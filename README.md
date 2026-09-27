# マーケット比較ボード

国内・海外の株価指数とドル円を、起点=100の騰落率で1本のチャートに重ねて表示するページです。

- `index.html` … ページ本体（`data.json` を読み込んで描画）
- `data.json` … 株価データ（GitHub Actions が自動更新）
- `fetch_data.py` … Stooq / Yahoo Finance からデータを取得するスクリプト
- `.github/workflows/update.yml` … 1日4回データを更新し GitHub Pages に公開

手動で更新したいときは、GitHub の「Actions」タブ →「株価データ更新と公開」→「Run workflow」。
