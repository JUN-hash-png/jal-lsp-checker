# JAL LSP Checker

JAL Mileage Parkで「Life Status ポイント積算対象」かつキャンペーン中として絞り込んだ検索結果を、毎日自動取得してスマホ向け一覧にします。

## 最初にやること

1. このZIPを展開する
2. `jal-lsp-checker` フォルダの**中身を全部**、GitHubの `JUN-hash-png/jal-lsp-checker` にアップロードする  
   GitHub画面では `Add file` → `Upload files`
3. GitHubの `Actions` タブを開き、必要ならワークフローを有効化する
4. `Update JAL LSP data` → `Run workflow` を押して初回実行する
5. 実行成功後、`Settings` → `Pages`
6. `Build and deployment` を次のようにする
   - Source: `Deploy from a branch`
   - Branch: `main`
   - Folder: `/docs`
7. 数分後、次のURLで表示される  
   `https://jun-hash-png.github.io/jal-lsp-checker/`

## 自動更新

GitHub Actionsが毎日06:17ごろ（日本時間）に実行します。手動更新は `Actions` → `Update JAL LSP data` → `Run workflow`。

## 表示項目

- サービス名
- 「何円ごとに何マイル」
- 100マイル以上になる最小利用額
- その時点で獲得するマイル数
- 最低利用額
- キャンペーン終了日
- 初回系の条件
- 個別LSPルールや自動判定できない案件への警告

## 大事な仕様

通常のMileage Park案件は `100マイル = 1LSP` として計算します。

ただし、詳細ページに次のような除外文言がある案件は誤計算を避けるため「要確認」にします。

> JALマイレージパーク経由によるマイル・Life Statusポイントは積算対象外

固定ボーナス、サービス固有LSP、複雑な最低利用条件も自動計算を止めます。利用前には必ず詳細ページを確認してください。

## 検索条件を変える

`config.json` の `search_url` を、JAL Mileage Parkで絞り込んだ検索結果の1ページ目URLへ差し替えます。ページ数が変わったら `pages` も変更します。
