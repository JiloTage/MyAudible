# Windows で起動する

Windows 10/11 x64、Chrome、Python **3.11 x64**、NVIDIA の最新 GPU ドライバーを用意する。speech.cpp はドライバーの Vulkan を使うので CUDA Toolkit のインストールは不要。YomiTokuのOCRはCUDAが利用できればGPU、それ以外ではCPUを自動選択する。基本セットアップはCPU版PyTorchを導入するため、GPUでOCRも行う場合は後述のCUDA版への切り替えを行う。

## 1. セットアップ

MyAudible のフォルダーで PowerShell を開く。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
```

このプロセスだけスクリプト実行を許可する。永続的な実行ポリシー変更は行わない。スクリプトは次を実行する。

1. リポジトリの `.venv` を作成し、CPU 版 PyTorch と `backend[ocr]` をインストールする。
2. speech.cpp **0.8.4** のタグに固定したインストーラをダウンロードし、記録した SHA-256 を確認する。上流インストーラも配布 ZIP の SHA-256 を確認する。
3. `%LOCALAPPDATA%\Programs\speech.cpp\current\speech.exe` に Windows Vulkan 版を設置する。PATH は変更しない。
4. `speech pull irodori-tts-mf` で約 1.92 GB の F16 モデルを取得する。OCR 重みは最初の画像読み取り時に取得する。
5. 現在のユーザーのChromeへNative Messagingランチャーを登録する。管理者権限は不要。このフォルダーの拡張機能IDだけを許可する。

Windowsの標準環境変数 `ComSpec` がユーザー・システムの両方に存在しない場合は、登録スクリプトがユーザー用の値をWindowsの `cmd.exe` に設定する。その旨が表示された場合はChromeをメニューの「終了」で完全に閉じ、開き直してから起動ボタンを使用する。

Python Launcher `py` がない場合は、Python 3.11 の実行ファイルを指定する。

```powershell
.\scripts\setup-windows.ps1 -PythonCommand 'C:\Python311\python.exe'
```

初回セットアップと初回 OCR はネットワークが必要。重みが揃った後はローカルで OCR と音声生成を実行する。モデルやランタイムは長期保存するが、読み取るページ画像と生成音声を保存するためのディレクトリは作成しない。

## 2. 毎回の起動

Chrome の `chrome://extensions` でデベロッパーモードを有効化し「パッケージ化されていない拡張機能を読み込む」からリポジトリ内の `extension` フォルダーを選ぶ。ツールバーの MyAudible をクリックしてサイドバーを開き、**「サーバーを起動して接続」** を押す。起動完了後に接続先とトークンを自動保存し、参照音声を更新する。既に同じトークンのバックエンドが起動済みなら、新しいプロセスを作らず接続する。

拡張機能からはセットアップ時に選んだモデルをGPUで起動し、OCRはCUDAの利用可否でGPU/CPUを自動選択する。接続先は `127.0.0.1:8765`、speech.cpp は `127.0.0.1:8081`。起動中はボタンを無効化する。ポート競合や起動失敗はサイドバーに表示する。モデル未準備などの詳細は手動起動で確認できる。

ボタン起動のサーバーはバックグラウンドで動き、サイドバーやChromeを閉じても常駐する。サイドバーの「停止」は読み上げだけを停止する。サーバーの終了はPCの再起動、またはタスクマネージャーでこのリポジトリの `scripts/run-local.py` を実行するPythonプロセスと、その子のspeech.exe / Pythonを終了する。起動に失敗した場合はランチャーが自分で起動したプロセスだけを終了する。

### 既存のインストールを更新する場合

一度だけ次を実行し、ChromeでMyAudibleを再読み込みする。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-native-host.ps1
```

フォルダーを移動した場合も再登録する。拡張機能IDを明示する場合は `-ExtensionId <chrome://extensionsに表示される32文字のID>` を指定する。モデルを変更したい場合は `-Model irodori-tts-mf:q8_0` などを指定する。ランチャーは `%LOCALAPPDATA%\MyAudible\native-host` に設置し、`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.myaudible.launcher` に登録する。

MSIXアプリ内からセットアップした場合、WindowsがAppDataの書き込み先をアプリ専用フォルダーへ転送することがある。登録スクリプトは実際の保存先を解決してChromeへ渡し、ランチャーとバックエンドのトークン保存先も揃える。

### 手動で起動する場合

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1
```

speech.cpp が `127.0.0.1:8081`、MyAudible バックエンドが `127.0.0.1:8765` で起動する。モデルは常駐し、リクエストごとにロードしない。最初の GPU 実行では shader のコンパイルでしばらく待つ場合がある。コンソールに表示された接続トークンを拡張の設定欄に入力する。コンソールを開いたまま使用し、終了時は **Ctrl+C**。ランチャーは自分が開始した 2 つのサーバーを停止する。

参照音声はサイドバーで **3～30 秒、12 MiB 以下、1～2 チャンネルの PCM WAVE** ファイルを登録する。一度登録すれば PC の MyAudible データフォルダーに保持され、プルダウンから選べる。生成音声やページ画像と異なり、参照音声はユーザーが削除するまで残る。

## GPU で OCR も実行する場合

対応するNVIDIAドライバーと十分なVRAMのあるPCでは、起動済みのMyAudibleサーバーを終了してから、リポジトリの仮想環境をCUDA版PyTorchへ切り替える。speech.cppはVulkan、YomiTokuはCUDAを使用する。以下は[PyTorch公式の2.6.0対応表](https://pytorch.org/get-started/previous-versions/#v260)にあるCUDA 12.4の組合せで、CPU版がインストール済みでもCUDA版へ置き換える。

```powershell
.\.venv\Scripts\python.exe -m pip install 'torch==2.6.0+cu124' 'torchvision==0.21.0+cu124' --index-url https://download.pytorch.org/whl/cu124
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c 'import torch; print(torch.__version__, torch.cuda.is_available())'
```

CUDAの確認結果が `True` なら、拡張機能の「サーバーを起動して接続」でGPU OCRが選ばれる。手動起動も既定は `-OcrDevice auto`。GPUを明示する場合は `scripts\run-windows.ps1 -OcrDevice cuda` とし、CUDAが利用できなければ起動時にエラーを表示する。TTSとOCRは同じGPUのVRAMを使うため、不足する場合はサーバーを終了し、`scripts\run-windows.ps1 -OcrDevice cpu` で起動する。CUDA Toolkitを別途インストールする必要はない。

## 選択肢とトラブル対応

| 状況 | 対処 |
| --- | --- |
| 起動用ランチャーに接続できない | `install-native-host.ps1` を実行し、拡張機能を再読み込み。IDが一致しない場合は `-ExtensionId` で再登録 |
| `Failed to start native messaging host.` | `install-native-host.ps1` を実行。`ComSpec` の修復が表示された場合は、Chromeをメニューの「終了」で完全に閉じて開き直す。起動済みChromeの環境変数は更新されない |
| `Either the '<all_urls>' or 'activeTab' permission is required.` | 拡張機能を更新し、`chrome://extensions` でMyAudibleを再読み込みする。サイトアクセスも許可する。サイドバーのボタンでは `activeTab` の権限を取得できないため、画面取得用のサイトアクセス権限を使用する |
| 長い画面で `Local speech.cpp disconnected or timed out` | 最新のバックエンドに更新し、MyAudibleのサーバーを再起動する。再生待ちによってspeech.cppからの受信が止まる問題を修正済み |
| Python 3.11 が見つからない | x64 版をインストールし `py -3.11 --version` を確認 |
| `vulkan-1.dll` がない | NVIDIA のドライバーを更新して再セットアップ |
| GPU が選べない | `& "$env:LOCALAPPDATA\Programs\speech.cpp\current\speech.exe" devices` で確認。CPU TTS は `-SpeechDevice cpu` |
| メモリを減らしたい | setup と run の両方に `-Model irodori-tts-mf:q8_0` を指定（約 1.21 GB）。音声品質は F16 より下がる場合がある |
| より高い精度を優先 | setup/run に `-Model irodori-tts` を指定し、サイドバーの steps を `16` ～ `40` に変更。MF より時間がかかる |
| ポートが使用中 | 既存のプロセスを確認。run の `-SpeechPort`/`-BackendPort` で別ポートを指定し、拡張の接続 URL も合わせる |
| 初回 OCR に時間がかかる | モデルダウンロードを完了させる。認識は以降も PC 性能と画像サイズに依存する |
| CUDA が利用不可と表示される | 対応した CUDA wheel とドライバーを確認、または `-OcrDevice cpu` |
| トークンが変わった・接続できない | 起動コンソールの値をサイドバーへコピーし、バックエンド URL を確認 |

### 手動で 2 つのコンソールから起動

PowerShell 1:

```powershell
& "$env:LOCALAPPDATA\Programs\speech.cpp\current\speech.exe" serve irodori-tts-mf --host 127.0.0.1 --port 8081 --device gpu
```

PowerShell 2（MyAudible フォルダー）:

```powershell
$env:MYAUDIBLE_SPEECH_URL = 'http://127.0.0.1:8081'
$env:MYAUDIBLE_OCR_DEVICE = 'cpu'
$env:MYAUDIBLE_OCR_LITE = '1'
.\.venv\Scripts\python.exe -m myaudible
```

手動起動では終了時に両方のコンソールで Ctrl+C を押す。API、モデル上の制限、ライセンスは [upstream-notes.md](upstream-notes.md) を参照。
