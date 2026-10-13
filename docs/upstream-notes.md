# 検証した上流 API

2026-10-12 に上流のソース、リリース、モデルカードを確認。依頼の「yokitoku」は日本語 OCR エンジン **Yomitoku** と解釈する。同名の非公式ラッパーや別の `speech.cpp` とは API が異なる。

| 用途 | 固定バージョン | 上流 |
| --- | --- | --- |
| OCR | Yomitoku `0.15.0`、タグ commit `afede0f4f528b30de749bc24f83fa8a62beb5036` | [kotaro-kinoshita/yomitoku](https://github.com/kotaro-kinoshita/yomitoku/tree/v0.15.0) |
| C++ TTS ランタイム | speech.cpp `0.8.4`、commit `d23c49f3233375e3ad4944c880807790b7f490be` | [nyosegawa/speech.cpp](https://github.com/nyosegawa/speech.cpp/tree/v0.8.4) |
| 既定 TTS モデル | `irodori-tts-mf` = `Irodori-TTS-866M-MF-v4.1-F16.gguf` | [sakasegawa/Irodori-TTS-v4.1-Small-MF-GGUF](https://huggingface.co/sakasegawa/Irodori-TTS-v4.1-Small-MF-GGUF) |
| 元モデル | Irodori-TTS v4.1 Small MF | [Aratako/Irodori-TTS-v4.1-Small-MF](https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small-MF) |

`speech pull` は speech.cpp に同梱されたカタログのモデル revision と SHA-256 を使う。`main` の最新モデルを任意に選ぶ処理ではない。Windows セットアップはタグに固定したインストーラの SHA-256 も検証する。

## speech.cpp の常駐サーバー

[上流 server.md](https://github.com/nyosegawa/speech.cpp/blob/v0.8.4/docs/server.md) と [モデル説明](https://github.com/nyosegawa/speech.cpp/blob/v0.8.4/docs/models/irodori-tts.md) に基づく。

```powershell
speech pull irodori-tts-mf
speech serve irodori-tts-mf --host 127.0.0.1 --port 8081 --device gpu
```

モデルは一度ロードされ、HTTP リクエストごとに再ロードしない。Windows x64 の公式配布は **Vulkan**。NVIDIA の最新ドライバーが提供する `vulkan-1.dll` を使う。`--device auto` は GPU があれば GPU、`gpu` は GPU 必須、`cpu` は CPU を選ぶ。`speech devices` で `Vulkan0` など実機名を確認できる。CUDA ビルドも可能だが上流は未検証・未配布としているため、本プロジェクトの標準手順は Vulkan。

### 音声生成

`POST http://127.0.0.1:8081/v1/audio/speech`、`Content-Type: application/json`。

```json
{
  "input": "明日の東京は晴れです。",
  "voice": "none",
  "response_format": "pcm",
  "stream_format": "sse",
  "speed": 1.0,
  "steps": 4,
  "instructions": "落ち着いた声で読み上げてください。",
  "seed": 123
}
```

`voice` は必須で、参照なしの `none` または登録名。`model` を省略するとロード中の TTS モデルを使う。seed は省略可能。送信可能な主要パラメータは次のとおり。

| キー | 範囲・挙動 |
| --- | --- |
| `input` | 必須の日本語テキスト |
| `voice` | `none` または登録済みの名前 |
| `speed` | `0.25` ～ `4`、既定 `1` |
| `steps` | `1` 以上、MF は既定 `4`、RF は既定 `40` |
| `instructions` | 声・話し方の説明、最大 512 tokens |
| `seed` | `0` ～ `2^53 - 1`。同一デバイスで再現に使用 |
| `language` | `auto` または `ja` |
| `seconds` | `0.5` ～ `30` の固定長。`duration_scale` と同時指定不可 |
| `duration_scale` | 正数、予測長に掛ける係数 |

`guidance_scale` はこの API に存在しない。RF モデル `irodori-tts` には `cfg_scale_text`、`cfg_scale_speaker`、`cfg_scale_instructions` がある。既定 MF モデルはこれらを受け付けない。未知のキーや無効な組合せを送信すると、無視されずエラーになる。

レスポンスの `X-Sample-Rate` は Irodori-TTS で **48000**。音声は **モノラル、符号付き 16-bit little-endian PCM**。SSE のイベントは `data: <JSON>` と空行で区切られる。

```text
data: {"type":"speech.audio.delta","audio":"<base64 PCM>"}

data: {"type":"speech.audio.done","seed":123,"samples":134400,"stop":"complete"}

```

クライアントは PCM を受信順に再生する。`stop` は `complete`、`max_seconds`、`model_limit`。生成前のエラーは HTTP 400/404/500 と `{"error":{"message":...,"type":...,"param":...,"code":...}}`。ストリーム開始後のエラーは `{"type":"error","error":{...}}`。切断で上流リクエストがキャンセルされる。

`response_format: "wav"` は全生成終了後にファイルを返す。`mp3`/`opus`/`aac`/`flac` は未対応。リアルタイム再生には `pcm` を使う。上流の `/v1/realtime` WebSocket は **音声認識のみ**で、TTS 会話セッションではない。MyAudible が Chrome へ提供する WebSocket は OCR、進捗、TTS PCM をつなぐ独自プロトコル。

MF は文全体の潜在音声を生成した後に、まず約 0.48 秒、続いて複数の窓で PCM をデコードする。トークン単位で即時発声するモデルではない。MyAudibleは全文を1回のHTTPリクエストで上流へ渡す。speech.cppが文単位に分割し、同じvoice・options・seedを引き継ぐ。モデルの長さ制限を超えた文は読点や空白でさらに分割する。アプリ側の文字数分割は行わない。1回のモデル推論は最大30秒・256 text tokensであり、全文のHTTPリクエストを30秒に制限する意味ではない。MFは100文字前後・18～20秒以上の長い文で発音が乱れる場合があること、`voice: none`では内部の分割ごとにも声が変わり得ることは、[上流のモデル説明](https://github.com/nyosegawa/speech.cpp/blob/v0.8.4/docs/models/irodori-tts.md#long-sentences) に記載されている。

### 参照音声の登録

動的登録は上流の組込み UI が使うエンドポイント。API を確認したソースは [tools/server/page.cpp](https://github.com/nyosegawa/speech.cpp/blob/v0.8.4/tools/server/page.cpp)。

`POST /speech/voices` の `multipart/form-data` に次を渡す。

| フィールド | 内容 |
| --- | --- |
| `name` | 任意の登録名。MyAudible は自動生成した ID を使う |
| `file` | WAVE ファイル、任意の sample rate、最大合計 120 秒 |

アップロードは 25 MB が上限。上流が作成する一時 WAV は登録終了時に削除される。声はモデルのメモリ内に追加され、レスポンスは `{"held":{...,"model":{...,"voices":[...]}}}`。モデルのアンロードや speech.cpp の再起動で登録は消える。MyAudible はユーザーが明示登録した参照 WAV とメタデータだけを保持し、必要時に再登録する。

MyAudible の登録 UI では **3～30 秒、12 MiB 以下、1～2 チャンネルの PCM WAVE** に制限し、モノラル 24 kHz / 16-bit へ正規化する。上流には声を個別削除する API がない。MyAudible で削除すると参照ファイル・メタデータ・選択肢は消えるが、上流で準備済みの声のデータは speech.cpp 終了まで揮発性メモリに残る。

再利用可能な GGUF 声ファイルも作成できる。

```powershell
speech voice irodori-tts-mf reference.wav reference.voice.gguf
speech serve irodori-tts-mf --add-voice myvoice=reference.voice.gguf --port 8081
```

`/speech/voices` はループバックで起動した場合のみ有効。MyAudible の Python バックエンドから、proxy を経由せず `127.0.0.1` に呼び出す。speech.cpp は認証を提供しないのでこのサーバーを外部へ公開しない。Chrome は認証付き MyAudible バックエンドを使う。

## Yomitoku と日本語の読み順

[v0.15.0 の Python API](https://github.com/kotaro-kinoshita/yomitoku/blob/v0.15.0/src/yomitoku/document_analyzer.py) と [結果 schema](https://github.com/kotaro-kinoshita/yomitoku/blob/v0.15.0/src/yomitoku/schemas/document_analyzer.py) を確認した。

```python
from yomitoku import DocumentAnalyzer

analyzer = DocumentAnalyzer(
    device="cpu",
    visualize=False,
    ignore_meta=True,
    ignore_ruby=True,
    reading_order="auto",
)
result, _, _ = analyzer(image_bgr)
text = "\n".join(paragraph.contents for paragraph in result.paragraphs if paragraph.contents)
```

`DocumentAnalyzer` は OCR とレイアウト・読み順解析を実行する。`result.paragraphs` は `order` に沿って整列済み。各段落のテキストは `contents`。縦書きは方向を判定し右から左へ段落を読む。`OCR` 単体の `words` を画面上の y 座標だけで整列すると、縦書きの読書ページで誤順になり得る。`ignore_ruby=True` は本文とふりがなの二重読みを減らす。

MyAudibleは画像全体と検出領域の画素値の変化を確認し、各色チャンネルの最大値と最小値の差がすべて8未満の領域を文字認識から除外する。認識後は段落・図表・表セルを組み立てる前に、不確かな `words` を除外する。YomiTokuの `rec_score` は各トークンと終端記号の確率の積なので、`rec_score ** (1 / (len(content) + 1))` により文字あたりに換算し、0.5未満を除外する。長い行の生のスコアが小さくなる性質を考慮し、本文に実際にある単語や日付の繰り返しは保持する。画像と認識結果の保存は行わない。

CPU 軽量モードは `configs` の `ocr.text_recognizer` に `model_name="parseq-tiny-dynw-v4"`、`dynamic_width=True`、`batch_bucketing=True` を指定する。`ocr.text_detector.infer_onnx=True` で CPU 検出を軽量化できる。CUDA 推論には CUDA 対応 PyTorch が必要。`DocumentAnalyzer.__call__` は内部で `asyncio.run` を呼び出し、`self.img` を書き換えるため、非同期 HTTP/WebSocket ハンドラからは専用スレッドで呼び出し、同一インスタンスへの並行実行を防ぐ。

OCR のモデル重みは初回使用時に Hugging Face からダウンロードされる。以降の画像やテキスト、生成音声はクラウドへ送信されない。上流の推奨は画像短辺 720 px 以上。読書ページの撮影領域が狭すぎたり解像度が低い場合は読み取り精度が落ちる。ルビ・見開き・漫画の吹出しの順序は必ず実際のページで確認する。

## 容量・速度・ライセンス

上流の公開値はハードウェアや文章に依存する目安。

| モデル | 配布ファイル | 上流計測の単一リクエスト peak memory |
| --- | --- | --- |
| MF F16（既定） | 約 1.92 GB | 約 2.04 GB |
| MF Q8_0 | 約 1.21 GB | 約 1.33 GB |
| RF F16 | 約 1.91 GB | 約 2.03 GB |

MF の公開ベンチマークは RTX 2080/Vulkan で first audio median 約 0.12 秒、Apple M5/Metal で約 0.25 秒。ただし短い 20 文の TTS 単体計測で、OCR・ネットワーク・モデル初回ロードを含まない。MyAudible が PC ごとのリアルタイム性を保証する値ではない。OCR を CUDA で同時に使用するとその VRAM が別途必要。

- Yomitoku は **CC BY-NC-SA 4.0**。商用利用の扱いは上流の利用条件を確認する。
- speech.cpp のコードは **MIT**。
- Irodori-TTS のコード・モデルは **MIT**。モデルカードは無断の声のなりすまし・誤情報目的の生成を禁止する追加条件を記載。参照音声には利用権限のあるものを登録する。
- 本リポジトリはモデルを同梱せず、上流ライセンスを変更しない。
