# なろう縦書きPDFからEPUBへの変換

小説家になろうの縦書きPDFを、縦書き・右開き・リフロー対応EPUBへ変換するスクリプトです。

## 環境のセットアップ

Python 3.12以上を事前にインストールしてください。WindowsではPowerShell、LinuxではBashを使用します。

### Windows

```powershell
./scripts/setup.ps1
```

Pythonを明示する場合：

```powershell
./scripts/setup.ps1 -Python 'C:/path/to/python.exe'
```

### Linux

```bash
bash scripts/setup.sh
```

`python3` が3.12未満の場合や、使用するPythonを指定する場合：

```bash
bash scripts/setup.sh --python /usr/bin/python3.12
```

Windowsは `.venv`、Linuxは `.venv-linux` をプロジェクト直下に作成します。同じ作業フォルダーをWindowsとLinuxで共有しても仮想環境が衝突しません。どちらも `requirements.txt` の固定バージョンを導入し、再実行時は既存の環境を再利用します。仮想環境の有効化は不要です。初回の依存取得には通信が必要ですが、PDF変換処理はオフラインです。Linuxで仮想環境の作成が失敗する場合は、使用中のPythonに対応する `venv` モジュールが導入されているか確認してください。

## 変換

```powershell
mkdir output -ErrorAction SilentlyContinue
./.venv/Scripts/python.exe -X utf8 narou_epub.py path/to/input.pdf output/converted.epub
```

Linuxの場合：

```bash
mkdir -p output
./.venv-linux/bin/python -X utf8 narou_epub.py path/to/input.pdf output/converted.epub
```

`path/to/input.pdf` は変換したいPDFのパスに置き換えてください。引数は入力PDFと出力EPUBです。表紙から書名・著者を取得できない場合は `--title` と `--author` を指定してください。既存のEPUBや診断JSONを置き換えるには `--overwrite` を付けます。出力先の親フォルダーは事前に作成してください。`.dev/` 内への出力は拒否します。

EPUBと同じ場所に `.report.json` を生成します。目次、段落数、除去したページ番号、ルビの判定、ページ境界の確認対象を確認できます。`--report` で診断JSONの出力先を変更できます。診断JSONに本文は保存しません。変換中の通信はありません。

サンプルPDFでは37項目の目次を生成し、印刷ページ番号を除去しました。ページ境界の段落結合やルビは配置から推定するため、診断JSONの確認対象と出力の抜き取り確認を推奨します。画像だけのPDFはOCR非対応です。Kindle上での縦書き、目次メニュー、文字サイズ変更、ページ送りは実機で確認してください。

```powershell
./.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -v
```

Linuxの場合：

```bash
./.venv-linux/bin/python -X utf8 -m unittest discover -s tests -v
```

`.dev/` の参照資料は変更しません。本文を含む生成物は `output/`、一時ファイルは `tmp/`、非公開の作業メモは `.work-notes/` に保存します。これらと `.venv/` はGitの対象外です。参照資料はリポジトリに同梱しません。

## 採用技術とライセンス

Python、pdfplumber、標準ライブラリのXML・ZIP処理を使用します。本リポジトリの作成コードと付属文書には [MITライセンス](LICENSE) を適用します。`.dev/` の参照資料、小説本文、生成EPUBはMITの対象ではなく、リポジトリにも同梱しません。依存ライブラリには各配布元のライセンスが適用されます。依存ライブラリのバイナリを再配布する場合は、そのライセンスと必要な表示を別途確認してください。
