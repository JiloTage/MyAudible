# Chromium integration tests

```sh
python -m pip install -r tests/requirements.txt
python -m playwright install chromium
python -m pytest tests/browser -q
```

既存のChromiumを使う場合は、実行ファイルのパスを `CHROMIUM_PATH` に指定します。Linuxの例:

```sh
CHROMIUM_PATH=/usr/bin/chromium python -m pytest tests/browser -q
```

テストは実際のChromiumで `extension/panel.js` と本番の画像処理・ページ検出・キャプチャ保護・音声再生モジュールを実行します。Chrome APIとローカルバックエンドはテスト用に置き換えるため、GPU、モデルは不要です。WebAudioのPCM再生は実際のAudioContextでも確認します。

`fixtures/reader.html` はURLとDOMを変えずにcanvas内の一文字だけを変更する読書画面です。ページ送りのアニメーション、クリック・キー・スワイプ操作、iframeのcanvas更新、画像のアニメーションも含みます。検出後に前の処理をキャンセルすること、古い音声を無視すること、別のタブ・ウィンドウを誤ってキャプチャしないこと、参照音声の選択が再表示時に復元されることを確認します。

UI確認用の合成画面は既定で `/tmp/myaudible-browser-artifacts/panel.png` と `reader.png` に出力します。出力先は `BROWSER_ARTIFACTS` で変更できます。実際のユーザー画面・音声は使用しません。
