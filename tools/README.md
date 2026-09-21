# PDF抽出検証ツール

`probe_pdf.py` は、PDF冒頭の読み順・段落・空行を参照EPUBと照合するための開発用試作です。変換本体ではありません。コマンドはプロジェクトのルートから実行します。

Windows（PowerShell）：

```powershell
./.venv/Scripts/python.exe -X utf8 tools/probe_pdf.py path/to/input.pdf --reference-epub path/to/reference.epub --start-page 4 --end-page 5 --paragraphs 20 --output output/pdf-probe
```

Linux（Bash）：

```bash
./.venv-linux/bin/python -X utf8 tools/probe_pdf.py path/to/input.pdf --reference-epub path/to/reference.epub --start-page 4 --end-page 5 --paragraphs 20 --output output/pdf-probe
```

検証用EPUBは抽出結果の照合にのみ使用します。この試作はサンプルの冒頭用であり、作品全体・ルビなどには未対応です。出力には本文が含まれるため、公開・コミットしないでください。詳細は[仕様書](../SPECIFICATION.md)を参照してください。
