# MyAudible

Chromeのサイドバーから、表示中のページや入力したテキストを日本語音声で読む拡張機能です。UIは最小限。処理は自分のPC上の **YomiToku → Irodori-TTS / speech.cpp** で行います。

- **ページを読む**: 表示中のページを撮影し、OCR結果を読み上げます。
- **自動読書**: ページが変わると前の音声を止め、次のページを読みます。URL・DOMに依存せず画面差分も監視するので、Canvasやiframeで描画される読書画面を対象にできます。
- **テキストを読む**: サイドバーに直接入力して送信します。
- **参照音声**: 一度登録すると、再起動後もプルダウンで選択できます。削除もできます。
- **パラメータ**: 読み順、推論ステップ、声の指示、seed、音声の再生速度など。
- **音声ストリーミング**: speech.cppのSSEによるPCM音声をWebSocketで中継し、音声チャンクが届いたら再生します。

## 導入（Windows + NVIDIA）

Python 3.11 x64、Chrome 116以上、最新のNVIDIAドライバを用意してください。Windows PowerShellで以下を実行します。

```powershell
git clone https://github.com/JiloTage/MyAudible.git
cd MyAudible
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1
```

このプロセスだけスクリプト実行を許可します。Python 3.12を使う場合はセットアップに `-PythonVersion 3.12` を指定してください。詳細、実行ポリシー、CUDA版OCRの導入方法は [Windowsセットアップ](docs/windows-setup.md) を参照してください。初回だけモデルのダウンロードが必要です。参照音声は3〜30秒のPCM WAVを登録できます。

起動時に表示される **接続トークン** を控えます。Chromeの `chrome://extensions` で「デベロッパーモード」を有効にし、「パッケージ化されていない拡張機能を読み込む」からこのリポジトリの `extension` フォルダを選択します。ツールバーのMyAudibleアイコンをクリックするとサイドバーが開きます。

サイドバーの接続先は `http://127.0.0.1:8765`、トークンは起動時の値に設定してください。

## U-NEXTでの使い方

1. U-NEXTの本をChromeで開き、読みたいページを表示します。
2. MyAudibleアイコンをクリックしてサイドバーを開き、バックエンドに接続します。
3. 読書画面の本文範囲を設定します。ページ番号、ツールバー、広告などを除くと誤検知を減らせます。縦書きの本は読み順を「縦書き」にします。
4. 「ページを読む」で認識と再生を確認し、自動読書を開始します。
5. 本のページをめくると、画面の変化が落ち着いてから新しいページを読みます。

画面差分を継続監視するため、同じURLのままCanvasだけが変わる場合も検知対象です。監視中は対象タブを前面に置いてください。別タブ・別ウィンドウに移ると撮影を一時停止します。ページ全体を別サイトへ移動した場合は、そのタブで拡張アイコンをクリックし直してください。

**U-NEXTのログイン済み実画面での検証は未実施です。** 再現用のCanvas読書画面で検知をテストしますが、実サイトの描画・保護仕様や将来の変更への対応を保証するものではありません。Chromeが撮影できない保護画面を読み出す機能はありません。実機での確認項目は [U-NEXT検証手順](docs/unext-validation.md) にまとめています。

## 保存するもの

登録した参照音声・その名前・接続トークンをPCに保存します。拡張側には接続設定・読書設定を保存します。**スクリーンショット、OCR本文、入力テキスト、生成音声を履歴として保存しません。** 画像と生成音声はメモリ上で処理し、停止・ページ変更・切断時に破棄します。参照登録時にspeech.cppが作る一時ファイルは処理後に削除されます。

モデルのダウンロード後、推論に外部APIは使いません。Chrome → `127.0.0.1:8765` → `127.0.0.1:8081` のローカル通信です。参照音声はプルダウンから選ぶ用途のため明示的に保存します。削除すると保存ファイルと選択肢が消えます。speech.cpp内の声の特徴量は、そのプロセスを終了するまでメモリに残ります。

## 構成

```text
extension/           Manifest V3、サイドバー、画面監視、PCM再生
backend/             FastAPI、YomiToku、参照音声管理、speech.cpp中継
scripts/             Windowsセットアップとローカル起動
tests/browser/       描画とページ変更のブラウザ統合テスト
docs/                セットアップ、API、検証手順
```

バックエンドとspeech.cppを別々に起動する場合:

```powershell
& "$env:LOCALAPPDATA\Programs\speech.cpp\current\speech.exe" serve irodori-tts-mf --host 127.0.0.1 --port 8081 --device gpu
# 別ターミナル（リポジトリ直下）
.\.venv\Scripts\python.exe -m myaudible
```

Windows以外でもPythonバックエンドと拡張機能を利用できます。speech.cppの導入方法・検証したAPIは [依存元の情報](docs/upstream-notes.md)、接続の仕様は [API](docs/protocol.md) を参照してください。

## 開発・検証

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e 'backend[test]'
python -m pytest backend/tests
npm test --prefix extension
pip install -r tests/requirements.txt
python -m pytest tests/browser
```

ブラウザテストにはChromiumが必要です。`python -m playwright install chromium` で導入するか、`CHROMIUM_PATH` に既存のChromium実行ファイルを指定してください。[検証記録](docs/verification.md) に自動テストと実モデルの確認結果を記載しています。Windows GPUとU-NEXT実画面の確認は別途必要です。

## ライセンス

MyAudibleのソースはMIT。YomiToku、speech.cpp、および各モデルのコード・重みにはそれぞれのライセンスが適用されます。詳細は [依存元の情報](docs/upstream-notes.md) を参照してください。
