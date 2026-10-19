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
台本: incoming/YYYY-MM-DD.json（レビュー済み台本）があればそれを使用
      なければ RSS 3媒体 → 台本アリーナ（Gemini・Mistral・OpenRouter無料モデルが同時に書き、検証を通った最良の台本を採用）
  → 学習教材（日本語訳・単語・クイズ）
  → 音声: Fish Audio → Gemini TTS → Kaggle(CosyVoice) → Modal(CosyVoice) → Google Cloud → Actions CPU(CosyVoice) → Edge
  → 1本ずつQA（全デコード・無音・音量・Whisper照合）→ 合格した回だけ公開
  → GitHub Release（MP3・台本）＋ GitHub Pages（学習プレーヤー・RSS）
```

### かんたん設定ガイド（すべて無料）

必要なのは「各サービスで登録してキーをコピー → GitHub に貼る」だけです。貼る場所はすべて同じです：
**GitHub のこのリポジトリ → Settings → Secrets and variables → Actions → New repository secret**（Name と Secret を入れて Add secret）。
どれも任意で、登録しなかったものは自動で飛ばされます（台本は `incoming/` か下の①〜③のどれか1つ、音声は何も無くても Edge で必ず作れます）。

**台本を書くAI（台本アリーナ）**

| | どこで | やること | GitHub に貼る Secret |
|---|---|---|---|
| ① Gemini | https://aistudio.google.com/apikey | 「Create API key」→ キーをコピー | `GEMINI_API_KEY` |
| ② Mistral | https://console.mistral.ai | 登録（メール＋電話番号の確認。カード不要）→ **Experiment（無料）プラン**を選ぶ → API Keys → Create new key | `MISTRAL_API_KEY` |
| ③ OpenRouter | https://openrouter.ai | 登録 → Keys → Create Key（`:free` の無料モデルだけを使うので課金なし。1日50回まで） | `OPENROUTER_API_KEY` |

**声（音声合成）**

| | どこで | やること | GitHub に貼る |
|---|---|---|---|
| ④ Fish Audio（11/30まで無料） | https://fish.audio | 登録 → 右上のアカウント → **API Keys** → Create → キーをコピー | Secret `FISH_API_KEY` |
| ⑤ Kaggle（GPU 週約30時間） | https://www.kaggle.com | 登録 → **Settings → Phone verification**（電話番号の確認。カーネルからのインターネット利用に必須）→ 同じ Settings の **API → Generate New Token** → 表示されたトークンをコピー | Secret `KAGGLE_API_TOKEN` と、Secret か Variable に `KAGGLE_USERNAME`（Kaggle のユーザー名） |
| ⑥ Modal（月30ドル分） | https://modal.com | 「Sign up」→ GitHub でログイン → **Settings → API Tokens → New Token** → 表示される2つの値をコピー | `MODAL_TOKEN_ID`（`ak-`）と `MODAL_TOKEN_SECRET`（`as-`） |
| ⑦ Google Cloud TTS | ①のキーのプロジェクトで「Cloud Text-to-Speech API」を有効化 | 課金アカウントの紐付けを求められたら紐付け（無料枠内に自動で制御） | 追加不要（①のキー） |
| ⑧ Actions の CPU | 登録不要 | このリポジトリは公開なので GitHub Actions の標準ランナーは無料・無制限。何もしなくても使われます | なし |

### 台本アリーナ（Gemini・Mistral・OpenRouter）

`cloud_languages.yaml` の `provider.models` に並んだモデルが、毎ラウンド**提供元の違う3者で同時に**台本を書き、検証（尺・行数・言語比率・教材）を通った中から目標の長さに最も近いものを採用します。提供元ごとに無料枠が別なので、Gemini の「1日約20回」を使い切っても Mistral と OpenRouter で続けられます。

- `mistral:mistral-medium-latest` など: Mistral の無料 Experiment プラン（送った内容は Mistral の学習に使われることがあります）
- `openrouter:free`: その日 OpenRouter に並んでいる `:free` モデルから、`provider.openrouter.preferred` の順（deepseek → qwen3 → …）で自動選択。無料モデルは入れ替わるので固定しません
- キーのない提供元、その日の上限に達したモデルは自動で飛ばします

### レビュー済み台本（incoming/）

`incoming/YYYY-MM-DD.json`（形式は `docs/EPISODE_BUNDLE.md`）があれば、その日の台本はAIに書かせずにそれを使います。話者・言語・URLなどの構造だけを確認し、日本語訳・単語・クイズは台本アリーナのモデルが後付けします（キーが無ければ教材なしで公開）。台本が固定なので、長さは音声で判定し、5〜18分（`episode.bundle_audio_seconds`）を受け入れます。

**過去の日をまとめて作る**: Actions → Journey Talk Cloud Daily → Run workflow → `dates` に `2026-10-07,2026-10-08` のように入力。日付ごとに順番に作って公開します（過去の日は `incoming/` の台本がある日だけ）。main 以外のブランチで実行した場合は公開せず、MP3 を実行結果の Artifacts に残すだけです（試聴用）。

### 音声合成（TTS）

`tts.order`（既定 `[fish, gemini, kaggle, cosyvoice, google_cloud, cosyvoice_local, edge]`）の順に、**まだ音声が無い回をまとめて**次のエンジンに渡します。1つの回で声が混ざることはなく、仕上がりの長さが範囲外だった回も次のエンジンでやり直します。Repository variable `TTS_ORDER` で順番を変えられます。既定の順番は無料のものだけです（有料の GPT は `TTS_ORDER` に自分で書いた場合だけ）。

1. **Fish Audio S2.1 Pro**（`FISH_API_KEY`）: 無料API用モデル `s2.1-pro-free`（2026-06-23 の発表で 11/30 まで無料・公正利用の範囲で無制限・品質保証なし・送信内容は改善に使われることあり）。1行ずつ3並列で合成し、ミナとレンの声は `assets/voices/` のお手本から複製、学習用の話速もAPIで指定します。`tts.fish.available_until` を過ぎると自動で使わなくなり、402（支払いが必要）が返ればその日は止めるので課金されません。延長されたら日付を書き換えるだけで続けられます。
2. **Gemini TTS**（`GEMINI_API_KEY`）: 1エピソードを1回の2話者リクエストで合成し、`[long pause]` の間で行ごとに切り分けます（無料枠は1モデル1日約10回）。
3. **Kaggle の無料GPU で CosyVoice 3**（`KAGGLE_USERNAME` + `KAGGLE_API_TOKEN`）: その日の未完成の回を**1回の非公開カーネル実行**（T4 GPU）でまとめて合成します。`kaggle kernels push` で起動して完了を待ち、結果を取り込みます。カーネルは kaggle.com/code/<ユーザー名>/journey-talk-tts に見えます。週約30時間の枠を使い切ると失敗して次へ回ります。
4. **Modal の CosyVoice 3**（`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`）: 月30ドルの無料クレジット内で1回1呼び出し。
5. **Google Cloud TTS — Chirp 3: HD**（月100万文字の無料枠、使用量を `state/` に記録して95万文字で止める）。
6. **Actions の CPU で CosyVoice 3**: アカウント不要・回数無制限。初回だけ Python 3.10 の仮想環境にライブラリとモデルを入れ（モデルは Actions のキャッシュに保存）、200分（`tts.cosyvoice_local.max_minutes`）で打ち切って、間に合わなかった回は次へ回します。CPU の速度は未実測なので、ログの `RTF`（1秒の音声に何秒かかったか）を見て調整してください。
7. **Edge TTS**: キー不要の最後の砦。

- Kaggle・Modal・Actions CPU は同じ `scripts/cosyvoice_core.py` / `cosyvoice_worker.py` を使い、CosyVoice のソースはコミット固定です。声は Fish と同じお手本から複製するので、どの段で作っても同じ2人に聞こえます。
- 学習言語の行は0.85倍速、🐢の行は0.75倍速。Fish・CosyVoice・Cloud TTS は話速を直接指定し、Gemini と GPT は合成後に伸ばします。
- クラウドの声は単語ごとのタイミングを返さないため、カラオケ表示の単語位置は行内で推定します。

### 品質チェックと公開

QA は1本ずつ合否を出します（全デコード、3秒超の無音、平均音量・ピーク、Whisper 照合）。**合格した回だけ**を Release・RSS・プレーヤーに載せ、不合格の回は警告とレポート（`qa-report.json`）に残します。`docs/health.json` に最終公開日と各回のエンジンを書き出します。Podcast 登録用に Repository variable `PODCAST_AUTHOR` / `PODCAST_EMAIL` を入れると RSS に所有者情報が入ります。

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
python -m unittest discover -s tests -p "test_*.py" -v
```

台本・教材の検証、台本アリーナ（Gemini・Mistral・OpenRouter）、レビュー済み台本の取り込み、Fish Audio・Kaggle・Actions CPU の各エンジン（通信はモック）、1本ずつのQAと合格分だけの公開、週末まとめ回、単語タイミング、オフライン用音声、WebVTT、言語別RSSを検査します。

旧 `daily-radio.yml`（固定カタログ版・1本の多言語MP3）は別系統として残っています。
