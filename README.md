# MyAudible

Chromeのサイドバーから、表示中のページや入力したテキストを日本語音声で読む拡張機能です。UIは最小限。処理は自分のPC上の **YomiToku → Irodori-TTS / speech.cpp** で行います。

- **今の画面を読む**: 表示中のページを撮影し、OCR結果を読み上げます。
- **自動読書**: ページが変わると前の音声を止め、次のページを読みます。URL・DOMに依存せず画面差分も監視するので、Canvasやiframeで描画される読書画面を対象にできます。
- **読み上げ後の操作**: 自動読み上げで最後の音声を再生した後、指定位置のクリックまたはスクロールを1回実行できます。位置は画面のプレビューから選択し、スクロールは上下左右の移動量を設定します。初期設定は「何もしない」です。
- **テキストを読む**: サイドバーに直接入力して送信します。
- **参照音声**: 一度登録すると、再起動後もプルダウンで選択できます。削除もできます。
- **パラメータ**: 読み順、推論ステップ、声の指示、seed、読み上げ速度など。速度はspeech.cppの音声生成に渡し、変更は次の読み上げから反映します。
- **音声ストリーミング**: OCR結果・入力テキストの改行や空白（全角・半角、タブなど）を除去した全文をspeech.cppへ1回で渡し、文の分割はspeech.cppに任せます。SSEによるPCM音声をWebSocketで中継し、音声チャンクが届いたら再生します。
- **サーバー起動**: Windowsではサイドバーのボタンでローカルサーバーを起動し、接続トークンも自動設定します。YomiTokuはCUDA版PyTorchと対応GPUが利用できればGPU、それ以外ではCPUを選びます。

## 導入（Windows + NVIDIA）

Python 3.11 x64、Chrome 116以上、最新のNVIDIAドライバを用意してください。Windows PowerShellで以下を実行します。

```powershell
git clone https://github.com/JiloTage/MyAudible.git
cd MyAudible
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

このプロセスだけスクリプト実行を許可します。Python 3.12を使う場合はセットアップに `-PythonVersion 3.12` を指定してください。詳細、実行ポリシー、CUDA版OCRの導入方法は [Windowsセットアップ](docs/windows-setup.md) を参照してください。初回だけモデルのダウンロードが必要です。参照音声は3〜30秒のPCM WAVを登録できます。

Chromeの `chrome://extensions` で「デベロッパーモード」を有効にし、「パッケージ化されていない拡張機能を読み込む」からこのリポジトリの `extension` フォルダを選択します。ツールバーのMyAudibleアイコンをクリックしてサイドバーを開き、**「サーバーを起動して接続」** を押します。接続先とトークンは自動設定されます。

画面取得のため、すべてのサイトへのアクセス権限を要求します。Chromeの再起動後に復元されたサイドバーからも読み取れます。設定でサイトアクセスを制限した場合は、読みたいサイトへのアクセスを許可してください。

既にセットアップ済みの場合は、更新後に一度 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-native-host.ps1` を実行し、Chromeで拡張機能を再読み込みしてください。起動用ランチャーはこのフォルダーの拡張機能だけに許可して登録します。フォルダーを移動した場合は再登録が必要です。

手動で起動する場合は `scripts\run-windows.ps1` を実行し、表示された接続トークンと `http://127.0.0.1:8765` を設定します。

## 使い方

1. Chromeで読みたいページを表示します。
2. MyAudibleアイコンをクリックしてサイドバーを開き、バックエンドに接続します。
3. 読書画面の本文範囲を設定します。ページ番号、ツールバー、広告などを除くと誤検知を減らせます。縦書きの本は読み順を「縦書き」にします。
4. 「今の画面を読む」で認識と再生を確認し、自動読み上げを開始します。
5. 本のページをめくると、画面の変化が落ち着いてから新しいページを読みます。

自動でページを進めたい場合は「読み上げ後の操作」を開き、「指定位置をクリック」または「スクロール」を選びます。「画面から操作位置を選ぶ」でプレビュー上のページ送りボタンや本文の位置を指定します。スクロール量は画面の幅・高さに対する割合で、正の値は右・下、負の値は左・上です。再生完了後も同じページが表示されている場合に1回操作し、画面の変化を検出して次を読みます。手動の読み上げでは操作しません。停止・ページ移動・タブ/ウィンドウ切り替え・設定変更で、待機中の操作を取り消します。

サイトがJavaScriptによるクリックを受け付けない場合、そのページでは自動クリックが動作しません。自動操作は表示中のウェブページ内に限ります。

画面差分を継続監視するため、同じURLのままCanvasだけが変わる場合も検知対象です。監視中は対象タブを前面に置いてください。別タブ・別ウィンドウに移ると撮影を一時停止します。別のタブを読みたい場合は「今のタブを対象にする」を押してください。

再現用のCanvas読書画面で検知をテストしますが、実サイトの描画・保護仕様や将来の変更への対応を保証するものではありません。Chromeが撮影できない保護画面を読み出す機能はありません。

## 保存するもの

登録した参照音声・その名前・接続トークンをPCに保存します。拡張側には接続設定・読書設定を保存します。**スクリーンショット、OCR本文、入力テキスト、生成音声を履歴として保存しません。** 画像と生成音声はメモリ上で処理し、停止・ページ変更・切断時に破棄します。参照登録時にspeech.cppが作る一時ファイルは処理後に削除されます。

Windowsのボタン起動用にランチャーの登録ファイル、起動処理のロック、起動ログをMyAudibleデータフォルダーへ保存します。起動ログには読み上げ本文・画像・生成音声・接続トークンを記録しません。

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
python -m pytest tests/test_native_host.py
npm test --prefix extension
pip install -r tests/requirements.txt
python -m pytest tests/browser
```

ブラウザテストにはChromiumが必要です。`python -m playwright install chromium` で導入するか、`CHROMIUM_PATH` に既存のChromium実行ファイルを指定してください。[検証記録](docs/verification.md) に自動テストと実モデルの確認結果を記載しています。

Windowsの実Native Messaging接続テストは、セットアップ済みのPCで `NATIVE_CHROMIUM_PATH` にChromium / Chrome for Testingを指定して実行します。新しい検証用プロファイルから登録済みランチャーと実サーバーへ接続し、起動ボタン、トークン保存、画面取得、長い日本語のOCRと音声生成を確認します。[実行例](docs/verification.md) を参照してください。

## ライセンス

MyAudibleのソースはMIT。YomiToku、speech.cpp、および各モデルのコード・重みにはそれぞれのライセンスが適用されます。詳細は [依存元の情報](docs/upstream-notes.md) を参照してください。
