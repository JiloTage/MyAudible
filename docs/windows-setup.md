# Windows で起動する

Windows 10/11 x64、Chrome、Python **3.11 x64**、NVIDIA の最新 GPU ドライバーを用意する。speech.cpp はドライバーの Vulkan を使うので CUDA Toolkit のインストールは不要。OCR は最初は CPU で実行する。

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

Python Launcher `py` がない場合は、Python 3.11 の実行ファイルを指定する。

```powershell
.\scripts\setup-windows.ps1 -PythonCommand 'C:\Python311\python.exe'
```

初回セットアップと初回 OCR はネットワークが必要。重みが揃った後はローカルで OCR と音声生成を実行する。モデルやランタイムは長期保存するが、読み取るページ画像と生成音声を保存するためのディレクトリは作成しない。

## 2. 毎回の起動

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1
```

speech.cpp が `127.0.0.1:8081`、MyAudible バックエンドが `127.0.0.1:8765` で起動する。モデルは常駐し、リクエストごとにロードしない。最初の GPU 実行では shader のコンパイルでしばらく待つ場合がある。コンソールに表示された接続トークンを拡張の設定欄に入力する。コンソールを開いたまま使用し、終了時は **Ctrl+C**。ランチャーは自分が開始した 2 つのサーバーを停止する。

Chrome の `chrome://extensions` でデベロッパーモードを有効化し「パッケージ化されていない拡張機能を読み込む」からリポジトリ内の `extension` フォルダーを選ぶ。ツールバーの MyAudible をクリックしてサイドバーを開く。

参照音声はサイドバーで **3～30 秒、12 MiB 以下、1～2 チャンネルの PCM WAVE** ファイルを登録する。一度登録すれば PC の MyAudible データフォルダーに保持され、プルダウンから選べる。生成音声やページ画像と異なり、参照音声はユーザーが削除するまで残る。

## GPU で OCR も実行する場合

CPU OCR が遅い場合は、CUDA 12.8 対応ドライバーと十分な VRAM のある PC で次を実行する。speech.cpp は引き続き Vulkan、Yomitoku の PyTorch は CUDA を使用する。以下の PyTorch/torchvision Windows Python 3.11 wheel の組合せを確認済み。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 -TorchIndexUrl https://download.pytorch.org/whl/cu128 -TorchVersion 2.7.1 -TorchVisionVersion 0.22.1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1 -OcrDevice cuda
```

GPU の世代、ドライバー、Windows 環境に合わせて PyTorch の公式対応表を確認する。インデックスとバージョンは setup のパラメータで変更可能。CUDA の利用可否はランチャーでも検査する。TTS と OCR は同じ GPU の VRAM を使うため、不足する場合は `-OcrDevice cpu` に戻す。

## 選択肢とトラブル対応

| 状況 | 対処 |
| --- | --- |
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

手動起動では終了時に両方のコンソールで Ctrl+C を押す。API、モデル上の制限、ライセンスは [upstream-notes.md](upstream-notes.md)、U-NEXT の検証手順はリポジトリの README を参照。
