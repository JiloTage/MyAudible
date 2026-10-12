# 検証記録

2026-10-12、Linux x64 / Python 3.12 / Chromiumで実施。

## 自動テスト

| 対象 | 結果 | 主な確認内容 |
| --- | --- | --- |
| バックエンド | 28テスト＋7サブテスト成功 | 認証・画像制限・読み順・参照保存削除・SSE・切断・生成キャンセル・OCRの排他・音声ACK・再起動後の音声登録 |
| 拡張ロジック | 9テスト成功 | 一文字だけ違うページ・安定待ち・タブ/ウィンドウの切替競合・PCM・古い要求ID・音声キュー破棄 |
| Chromium統合 | 11テスト成功 | 同じURLのCanvas・iframe・画像更新・クリック/キー/タッチ・実際のpanel.js・前の要求の置換・参照選択復元・メモリと保存範囲 |

```bash
python -m pytest backend/tests -q
npm test --prefix extension
CHROMIUM_PATH=/usr/bin/chromium python -m pytest tests/browser -q
```

Chromium統合テストでは描画・画像処理・WebAudioを実行し、Chrome APIとバックエンド接続境界はテスト用に置き換える。実際のブラウザ権限付与操作やU-NEXTの読書画面を再現しきるものではない。

## 実モデル

偽の音声応答を使わず、公式の重みと実行ファイルで別途確認した。

- **YomiToku 0.15.0 / CPU PyTorch 2.6.0**: 合成した日本語の横書き、縦書き、縦書きの自動判定で、本文と順序が一致した。初回ダウンロードと初期化を含むOCRは約42秒、モデル読み込み済みの縦書きOCRは約5秒。処理後の `DocumentAnalyzer.img` が解放された。
- **speech.cpp 0.8.4 / Irodori-TTS v4.1 Small MF F16 / CPU**: 配布バイナリのSHA-256を確認し、本実装の `SpeechService` から日本語SSE音声を受信した。48 kHz・16-bit PCM。短文の最初の音声チャンクは約2.2〜2.4秒後に届いた。
- **参照音声**: 一時的なテスト用WAVを本実装のVoiceStoreで正規化し、実際のspeech.cppに登録・選択して生成できた。テスト用参照は終了時に削除した。声の似方や聞きやすさは評価していない。
- **WebSocket中継**: 認証 → 本文 → 実TTS → `audio.chunk` → `audio.ack` → 完了を確認した。短文で3チャンク、261,120 PCM bytes、最初の音声は約2.42秒。切断後のジョブと接続数は0、生成WAVの保存は0。

計測値はこのCPU環境での短文テスト。OCR込みの実際のページやWindows GPUで同じ速度になるという保証ではない。

## 起動手順と未検証範囲

PowerShellの両スクリプトは実際のPowerShell 7.5.3で構文解析した。Pythonランチャーは、空白のある実行パス、起動待ち、ポートとOCR設定、Ctrl+Cによる自分の子プロセス終了、既存ポートの使用中判定を確認した。

Windows上でのセットアップ実行、NVIDIA/Vulkan・CUDA推論、実際のU-NEXTのログイン済み読書画面は未検証。[U-NEXT実機検証](unext-validation.md) を利用PCで行う必要がある。スクリーンショットを制限する画面の回避は実装していない。
