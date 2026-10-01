# Journey Talk news-podcast-bot

ニュースRSSから、多言語学習用の公開候補台本を安全な固定カタログで組み立てるWindows向け実装です。既定経路ではモデルに台本文を書かせません。

```text
RSS（3媒体）
  → 引用可能な見出し・要約文を厳格選択
  → Ollamaは列挙済みプランを選ぶ小さな呼出し1回だけ
  → 検品済み固定カタログ＋完全一致引用を110スロットへ解決
  → typed Episode JSONを厳格検査
  → JSONだけからMarkdownを描画
  → JSON・Markdown・done pointerを原子的にcommit
```

Ollamaの呼出しが失敗、timeout、または不正なenumを返した場合は、日付・source digest・contract hashのSHA-256から安全なプランを決めます。Pythonの`hash()`は使いません。見出しや要約はOllamaへ渡さず、外国語本文の生成にも使いません。

## 必要なもの

- Windows 10 / 11
- Python 3.10以上（3.11または3.12を推奨）
- RSS取得のためのインターネット接続
- 任意: [Ollama](https://ollama.com/) と`qwen2.5:7b`。停止中でも固定fallback planで安全版を構築できます。

`install.ps1`は、`uv`がPATH上にあればPython 3.12の仮想環境を作り、なければPATH上の`python`を使います。

## セットアップと実行

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
ollama pull qwen2.5:7b
.\run.ps1
```

任意の日付は次のように指定します。

```powershell
.\run.ps1 --date 2026-08-15
```

成功時にはrun-id付きの正本と固定名copyを保存し、最後にdone pointerをcommitします。

```text
output/runs/YYYY-MM-DD_<run-id>_podcast.json
output/runs/YYYY-MM-DD_<run-id>_podcast.md
output/YYYY-MM-DD_podcast.json
output/YYYY-MM-DD_podcast.md
output/YYYY-MM-DD_podcast.done.json
```

typed JSONが正本です。Markdownは必ずJSONだけから描画され、frontmatterの`episode_sha256`はJSON内容のcanonical SHA-256と一致します。done pointerはrun-id、contract/config/source hash、JSON/Markdownのファイルhashを保持します。将来のRSS処理や後工程は固定名ファイルではなくdone pointerを正本として扱ってください。

同日再実行では、resolved output directoryと日付から導出したOS lockを取得し、done・run-id付きJSON/Markdown・相互hash・typed内容・固定名copyをRSS/Ollamaより先に再検査します。異なるconfig/work directoryでも同じoutputと日付なら同じlockを使い、lockはdone commit完了まで保持します。すべて有効ならネットワーク呼出し0件で終了します。固定名copyだけが中断でずれた場合は、doneが指す検証済みrunから原子的に回復します。新しい記事で作り直す場合だけ`--force`を指定します。

完成前に中断した場合も、記事選択直後かつselector呼出し前に`.work/safe/YYYY-MM-DD/manifest.json`へ原子的に保存したsource snapshotを使います。日付、safe content/source config、contract、catalog、source digestと記事全文を上限64KiBで厳格再検査できた同日retryはRSSを呼びません。壊れたpinや契約の異なるpinは再利用せず、`--force`は常にRSSを再取得して新しいpinと暗号学的に新しいrun IDを作ります。

```powershell
.\run.ps1 --date 2026-08-15 --force
```

## 公開可能な安全契約

対応学習言語は、次の6言語だけです。日本語ナビを加えた全7言語が固定110発話に登場します。

- 英語 `en-US`
- ドイツ語 `de-DE`
- スペイン語 `es-ES`
- ロシア語 `ru-RU`
- 中国語 `zh-CN`
- 韓国語 `ko-KR`

RSSには出典言語を明示しています。

- NHK NEWS WEB: `ja-JP`
- BBC News World: `en-US`
- Reuters via Google News: `en-US`

各媒体から、見出し全体と要約の完全文を両方安全に引用できる記事を1件選びます。見出しは全体が150文字以内、要約引用は句読点で完結した20〜150文字の連続部分です。引用はpinned canonical fieldの`start:end`、本文、SHA-256、source languageを保持し、再検査時に完全一致させます。任意schemeのlocator、email、`www`、plain domain、UnicodeのCc/Cf/Cs制御文字、Markdown構造を含む引用候補は使いません。

出典scriptも候補選択、snapshot再読込、最終引用の三層で検査します。英語出典はalphabetic Latin主体で、漢字・かな・ハングル・キリル文字を混在させません。日本語出典はalphabetic/script文字のうち漢字＋かなが80%以上で、かな2文字以上または漢字2文字以上を要求し、snapshot fieldにはさらにかなの証拠を必須とし、ハングル・キリル文字を拒否します。`AI`のような少量のLatin固有語は許容しますが、英語文にかな1字を足しただけの行は拒否します。RSS文字列はHTML entity展開、NFKC、HTML/空白clean後に再度NFKCと安全検査を行い、pinのdecodeでも同じcanonical値との完全一致を要求します。

3記事はそれぞれA/Bに分かれ、各小チャンクは固定14スロットです。

1. 日本語の引用予告
2. 出典言語タグ付きの見出しまたは要約の完全一致引用
3. 日本語gloss
4. 対象外国語の検品済みP1
5. 日本語gloss
6. 対象外国語の検品済みP2
7. 以後同じ組でP6まで

したがって各小チャンクは日本語7件、source quote 1件、対象外国語6件です。日本語ナビは「最初の題材」「続く題材」「最後の題材」と引用手順だけを述べ、引用本文を埋め込みません。固定外国語カタログには記事のタイトル・要約・URLを一切渡しません。

全体は次の宣言的scheduleから機械導出します。

```text
opening 6
news 14 × 6
expressions 14
closing 6
合計 110
```

JSONの各発話は次のresolved fieldを持ちます。

```json
{
  "slot_id": "news.S01.A.04",
  "speaker": "MC_M",
  "language": "de-DE",
  "content_kind": "catalog",
  "content_ref": "catalog:language:de-DE:P1",
  "text": "Hören wir uns die Hauptaussage aufmerksam an.",
  "pause_after_ms": 1450,
  "repeat_of_slot_id": null
}
```

話者、言語、kind、参照先、本文はscheduleとcatalogから再解決して完全一致させます。`source_quote`以外のfree textは公開不能です。数字は完全一致quote内だけ許可し、それ以外ではUnicode数字、URL、Markdown構造、source ID、記事固有語を拒否します。全固定外国語は対象scriptを検査し、全文はNFKC後に重複0件でなければなりません。catalogは上限付きstrict loadでcanonical bytes・SHA-256・deep immutable valueを一体化し、その同じobjectだけをselector、resolver、validator、publisher、contract hashへ渡します。各利用境界でschema、reviewed variant、全固定text、安全性、重複とreview済みcanonical catalog hashを再検査するため、自己整合するだけの差替えcatalogも公開できません。

## 尺の決め方

推定器は言語別の文字・単語量、句読点に加えて`pause_after_ms`を計上します。本文を自由生成して水増しせず、各固定slotの許可pause候補をDPで組み合わせます。引用確定後に600〜900秒へ入る組だけを採用し、720秒に最も近い組を決定します。最短・最長の引用でも解がなければ保存せず失敗します。

## 厳格検査とI/O上限

保存前とdone再読込時に、少なくとも次を検査します。

- exact 110 slot、全体のMC交互、speaker/language/kind schedule
- catalog IDから再解決した本文のbyte/NFKC完全一致
- source field slice、範囲、hash、source languageの完全一致
- target言語のcross-script純度
- quote以外の数字・URL・Markdown・記事固有語禁止
- 全文重複禁止と`repeat_of_slot_id: null`
- 3つの記事見出しと3つの参照、全7言語
- pause込み600〜900秒
- JSONのcanonical episode hashとJSON→Markdown相互一致

JSONはduplicate keyとNaN/Infinityを拒否します。日付・日時はparse後の`isoformat()`と完全一致させます。上限はsafe source manifest/catalog 64KiB、draft checkpoint chunk 32KiB、episode JSON/Markdown各512KiB、done 16KiBです。RSSはstream読込し、`Content-Length`と展開後の累計を合わせて5MiB以内に制限します。selectorもPOST応答をstreamで読み、外側64KiB、内側4KiB、connect/read最大5/45秒に加えて総stream経過時間も制限し、不正・未完了・過深・timeoutは決定的fallbackへ移ります。記事本文はselectorへ送りません。

全JSON/Markdown/done bytesとsizeを最初に確定し、run-id付き正本、固定名copy、doneの順に保存します。run-id付き正本は同一directoryでfsync済みtempからNTFS hard-linkのno-clobber createを行い、同じbytesだけ冪等成功、異なるbytesは拒否します。hard-linkが利用できない場合も上書きへfallbackせず停止します。固定名copyとdoneは同一directoryのtempをflush/fsyncして`os.replace`し、doneを最後にcommitします。

## 旧freeformはdraft専用

以前のOllama自由文生成は公開経路から外しました。調査目的で明示的に`--draft`を指定した場合だけ実行できます。

```powershell
.\run.ps1 --date 2026-08-15 --draft
```

成果物は`.work/drafts/YYYY-MM-DD/artifacts/`のJSON envelopeだけに入り、必ず`draft: true`、`publishable: false`です。`output/`、safe publisher、done pointerには接続されません。draftを昇格するCLIはありません。`--draft`と`--force`は同時指定できません。

## 設定

`config.yaml`で番組名、固定2名のMC、Ollama selector名、RSS、出力先を指定します。安全版ではhost ID、6言語、3媒体とその`source_language`をreviewed contractへ固定しています。未対応言語を追加しても公開可能なcatalogがないため、起動時に拒否します。

Reutersは無料の公式公開RSSが現在ないため、初期設定ではGoogle NewsのReuters検索RSSを使います。利用条件に合う契約RSSがある場合も、feed名・source language・安全segment契約を保った上でURLを変更してください。各媒体に安全な見出しと完全文要約を持つ候補が1件もなければ、既存doneを変更せず終了コード1で停止します。

## テスト

実RSS/Ollamaを使わないunit/mocksは次で実行します。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile main.py safe_pipeline.py
git diff --check
```

テストには、source pinのnetwork 0 retryとforce、source script三層、catalogのbyte/hash binding、source languageと引用範囲、text/ref/hash tamper、schedule/speaker/language/kind違反、cross-script、重複、quote限定digits、DPの独立brute-force oracleと解なし、selectorの外側/内側上限とfallback、immutable runの同時writer、write/fsync/replace中断、done前crash、bounded I/O、draft publish拒否、最短/最長sourceでの完全110発話が含まれます。

## タスクスケジューラ

Windowsの「タスク スケジューラ」で、プログラムをPowerShell、引数を次の形式にします。

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\Users\ring bang\Documents\ChatGPT\ポッドキャスト\run.ps1"
```

「開始」にはプロジェクトの絶対パスを指定し、「ネットワーク接続時のみ実行」と「新しいインスタンスを開始しない」を推奨します。通常retryに`--force`を付けないことで、検証済みdoneがある日はネットワーク0件で終了します。

## 公開前の注意

安全版は、モデルの創作混入と機械的な構造事故を強く制限しますが、RSS引用の利用条件、著作権上の引用要件、発音、配信先の規約を代替判断しません。公開・収益化前に、各媒体の利用条件と完成音声を人が確認してください。

## GitHub Actionsクラウド運用

`.github/workflows/daily-radio.yml`は、毎日06:00（Asia/Tokyo）に次を実行します。

1. 既存の安全契約テストを実行
2. RSSからtyped Episode JSONとMarkdownを生成
3. 男女2音声・全7言語を音声化し、10〜15分のMP3とYouTube用MP4を生成
4. 検証済み成果物をActions artifactへ保存
5. 公開設定が揃っている場合だけ、MP3をGitHub Releaseへ配置
6. `docs/feed.xml`を更新してGitHub Pagesへ配信
7. YouTube Data APIへ動画を投稿
8. 月曜日の実行時に、直近の成功・失敗、ブロッカー、次のマイルストーンをGitHub Issueへ記録

手動実行では`publish=false`が既定で、生成・検証だけを安全に試せます。定期実行は追加シークレットなしでもMP3をGitHub Releaseへ公開します。RSS / GitHub Pages / YouTubeは独立した任意チャンネルで、設定済みのものだけ有効になります。設定不足があっても日次ビルドとRelease公開は止まりません。

### Repository variable

- `PODCAST_EMAIL`（RSSを使う場合のみ必須）: Spotify等の所有確認用。RSSに公開されるため、公開専用アドレスを推奨
- `PODCAST_AUTHOR`（任意）: 既定値は`Journey Talk`
- `PODCAST_DESCRIPTION`（任意）
- `PODCAST_BASE_URL`（任意）: 独自ドメイン利用時のみ。未設定ならGitHub Pages URLを使用
- `ENABLE_PAGES`（任意）: GitHub Pagesを設定済みの場合だけ`true`にする

### Actions secrets

- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

YouTube Data API v3を有効にしたGoogle CloudプロジェクトでOAuthクライアントを作り、ローカルで次を実行すると3つの値を取得できます。`client_secrets.json`はGitに追加しないでください。

```bash
python -m pip install -r requirements.txt google-auth-oauthlib==1.2.2
python scripts/youtube_oauth_setup.py client_secrets.json
```

### 初回公開

1. リポジトリをpublicにする
2. Settings → Pages → Sourceを`GitHub Actions`にする
3. 上記variable/secretsを登録する
4. Actionsから`Journey Talk Daily Radio`を`publish=false`で手動実行する
5. artifact内のMP3/MP4を確認する
6. 問題がなければ`publish=true`で再実行する
7. 公開された`https://<owner>.github.io/<repo>/feed.xml`をSpotify for Creatorsの「既存の番組」へ一度だけ登録する

SpotifyへのRSS登録とメール確認は初回だけ人の操作が必要です。登録後の新エピソードは、Actionsが更新する同じRSSから自動取得されます。


## 5言語 × 各10〜15分のクラウド版（主系統）

`.github/workflows/daily-multilang.yml` が日次の主系統です（毎日07:00 JST）。ドイツ語・スペイン語・ロシア語・中国語・韓国語を、それぞれ独立した10〜15分の番組として生成します。

```text
RSS 3媒体 → Geminiが共通の3記事を選択
  → 言語ごとに台本＋学習教材をGeminiで生成（検証エラーを返して最大3回再試行）
  → Gemini TTSで音声化（失敗時はEdge TTS。復習パートは🐢ゆっくり再生＋シャドーイング用の間）
  → 全デコード・無音検査・Whisper照合
  → GitHub Release（MP3・台本）＋ GitHub Pages（学習プレーヤー・RSS）
```

### 必要な設定

Repository secret に `GEMINI_API_KEY`（[Google AI Studio](https://aistudio.google.com/apikey) で発行）を登録してください。未設定の場合、ワークフローは最初の検査で停止し、Job Summaryに設定手順を表示します。任意で Repository variable `GEMINI_MODEL` によりモデルを上書きできます（既定は `cloud_languages.yaml` の `gemini-2.5-flash`）。

### 音声合成（TTS）：Gemini → GPT → Edge の3段階

`cloud_languages.yaml` の `tts.order`（既定 `[gemini, openai, edge]`）の順に試し、失敗した回は次のエンジンで丸ごと作り直します（1つの回で声が混ざることはありません）。Repository variable `TTS_ORDER`（例: `openai,edge`）で順番を変えられます。

1. **Gemini TTS**（`GEMINI_API_KEY`）: 無料枠は1モデルにつき1日約10リクエストなので、**1エピソードを1回の2話者リクエスト**で合成し、行ごとに切り分けます。各行の末尾に `[long pause]` を付けて話者交代の間を長くし、行の長さの予測と間の長さから切れ目を選びます（24行の検証で全行一致）。モデルは `gemini-3.8-flash-tts` → `gemini-3.8-flash-lite-tts` → `gemini-3.1-flash-tts-preview` → `gemini-2.5-flash-preview-tts` の順に試し、1日の上限に達したモデルは待たずに次へ進みます。声はミナ=`Aoede`、レン=`Puck`。
2. **OpenAI GPT TTS**（`OPENAI_API_KEY` を登録した場合）: 1行ずつ合成するので行のタイミングは正確です。モデルは `gpt-4o-mini-tts`（`OPENAI_TTS_MODEL` で変更可）、声は `coral` / `ash`。行ごとに話す言語を指示します。
3. **Edge TTS**: キー不要の最後の砦。単語ごとの正確なタイミングも取れます。

- Gemini と GPT の声は速いので、学習言語の行は0.85倍速に伸ばします（ピッチは保持。`tts.target_language_tempo`）。🐢の行は0.75倍速です。
- 台本の長さは、言語ごとの実測の話速（`episode.speech_rates`）で見積もります。完成音声の受け入れ範囲は9〜18分（`episode.audio_seconds`）で、話速の違うエンジンに切り替わっても公開できます。
- Gemini と GPT は単語ごとのタイミングを返さないため、カラオケ表示の単語位置は行内で推定します。
- 実測（スペイン語の回）: 65行・推定712秒の台本が、Gemini TTS の1リクエストで12.4分の音声になりました。

### 学習体験

各エピソードは音声に加えて、学習用の教材を生成します。

- **日本語訳**: 対象言語のすべての発話に訳を付与
- **単語・表現**: 5〜10個。中国語はピンイン、韓国語はローマ字、ロシア語はアクセント記号付きの読み
- **リスニングクイズ**: 3〜5問。正解位置は決定的にシャッフル
- **ゆっくり復唱**: 復習パートの重要文を-25%速度で再読み上げし、続けて真似するための間を挿入
- **固定ホスト**: ミナ（MC_F）とレン（MC_M）。名前は `cloud_languages.yaml` の `hosts` で変更可
- **学習者レベル**: `episode.learner_level`（既定 `CEFR B1`）で語彙と文の難しさを調整

教材の検証に3回とも失敗した場合でも、音声台本として有効なら教材なしで公開します。1言語の生成に失敗しても、他の言語は公開されます（失敗は `manifest.json` の `failed` とActionsの警告に記録）。

### Webプレーヤー（GitHub Pages）

`https://<owner>.github.io/<repo>/` で、スマホ向けの学習プレーヤーが使えます。

- 音声と同期するスクリプト（再生中の文をハイライト、タップでその文へ移動）
- **カラオケ表示**: Edge TTS の WordBoundary から、いま発音している単語をハイライト
- **🔁 1文リピート**（シャドーイング用）、前後の文へ移動、再生速度 0.75〜1.25×
- **🎤 発音チェック**: ブラウザの音声認識（Web Speech API）で読み上げを聞き取り、お手本と比較して点数と聞き取れなかった語を表示（Chrome / Edge / Safari。非対応ブラウザではボタン非表示）
- **ブラインドモード**: 対象言語の文をぼかし、聴き終えた文から表示
- 単語帳（間隔反復: 1→3→7→14→30日）、Anki用TSV書き出し
- クイズ、聴了記録、連続学習日数（ブラウザのlocalStorageに保存）
- **⬇ オフライン保存（PWA）**: 公開から3日以内の回を端末に保存し、電波がなくても同期スクリプト・シーク・1文リピートつきで再生。ホーム画面に追加してアプリとして使用可
- **🔄 端末間の引き継ぎ**: 学習記録を引き継ぎリンクかファイルで別端末へ移して統合（サーバー不要。データはURLの `#` 以降に入り送信されない）
- キーボード操作（Space / ← → / R）とロック画面の操作（Media Session）

### 週末まとめ回

日曜日（日本時間）は、各言語でその週に出た単語・表現だけを使った復習回（`<slug>-weekly`）も生成します。新しい場面での使い方、「〇〇って何て言う？」の練習、🐢ゆっくり復唱で構成されます。手動実行時は `weekly_review` を有効にすると任意の日に作れます。表現が少ない週や生成に失敗した言語はスキップし、通常回の公開は止めません。

### オフライン用音声とカバー画像

- render は192kbpsの本編に加えて、64kbpsモノラルの軽量版（`*.offline.mp3`）を同じタイムラインで出力します。軽量版もReleaseに置かれ、デプロイ時に直近3日分だけをPagesへ配置します（gitにはコミットしません）。Release上のMP3はCORS非対応で、キャッシュしてもシークできないため、同一オリジンの軽量版を使います。
- カバー画像（3000×3000）とアプリアイコンは `scripts/make_covers.py` で生成し、`docs/covers/`・`docs/icons/` にコミット済みです。デザインや言語を変えたときだけ再生成してください:
  `python scripts/make_covers.py --font /path/to/NotoSansCJK.ttc`

### 毎日の成果物

```text
output/languages/YYYY-MM-DD/{de,es,ru,zh,ko}.json   台本＋教材（正本）
output/languages/YYYY-MM-DD/{slug}.md               訳・単語表・クイズ付きの台本
build/languages/YYYY-MM-DD/journey-talk-YYYY-MM-DD-{slug}.mp3
build/languages/YYYY-MM-DD/media-manifest.json      各発話の開始・終了秒（timeline）を含む

docs/episodes.json                                  エピソード一覧
docs/episodes/YYYY-MM-DD/{slug}.json                プレーヤー用データ（同期スクリプト・単語・クイズ）
docs/episodes/YYYY-MM-DD/{slug}.vtt                 WebVTT字幕（podcast:transcript）
docs/feed.xml                                       全言語のPodcast RSS
docs/feeds/{slug}.xml                               言語別のPodcast RSS（カバー画像つき）
docs/offline/YYYY-MM-DD/*.offline.mp3               オフライン保存用（デプロイ時のみ配置）
```

Podcastアプリでは、学びたい言語の `feeds/{slug}.xml` だけを購読できます。各エピソードの説明欄には要約・今日の表現・学習ページへのリンクが入り、対応アプリでは字幕（transcript）も表示されます。

### テスト

```bash
python -m unittest discover -s tests -p "test_learning_pipeline.py" -v
```

台本・教材の検証、再試行と部分失敗、週末まとめ回、単語タイミング（UTF-16範囲）、オフライン用音声、WebVTT、言語別RSSとカバー画像、シャドーイング用ポーズの上限を検査します。

旧 `daily-radio.yml`（固定カタログ版・1本の多言語MP3）は別系統として残っています。
