# 引数なし実行で現在地点を出す — 実装案

状態: 提案。今回のPRは仕様の整理のみで、CLIの動作変更はまだ含まない。
基準main: `16df861e8f44ea8c9da69416af1272e6bc1afb5a`（2026-10-11）。

## 目的と入口

最初の一回を `python gh_identity.py` にするだけで、作業者とAIが
「どのrepoのどの位置にいて、GitHub側はどこまで進み、直近で何が言われたか」
を短い標準出力で把握できるようにする。毎回help、Git、ghを何度も呼んで
情報をつなぎ直す手間と、全文ログ・差分を読むトークンを減らす。

現行 `_main()` はsubcommand必須のため、引数なしではusageとexit 2になる。
既存の個別コマンドは保持し、subcommand省略時だけoverviewへ分岐する。
`--help` は従来どおり機能一覧を表示して終了し、APIを呼ばない。
予算/transport指定だけの場合もoverviewとする。内部worker分岐は維持する。

- `python gh_identity.py`: cwdから読み取り、短いテキストをstdoutへ。
- `python gh_identity.py overview`: 同じ処理を明示して実行する入口。
- `python gh_identity.py --json`: 同じ観測の構造化JSON。
- `python gh_identity.py overview --offline`: ローカル観測だけ。
- `python gh_identity.py overview --repo OWNER/REPO`: Git外/remote曖昧時の明示指定。

テキストとJSONは同じPython API `overview(...)` の結果を描画する。
既存subcommandのJSON出力やゲート用終了コードを勝手に変更しない。

## 既定で出す情報

| 表示 | 内容 | 避ける誤認 |
|---|---|---|
| 観測 | UTCの観測時刻、online/offline、complete/partial | 古いキャッシュを今のGitHubと混同しない |
| 作業場所 | cwd、repo、選択根拠、branchまたはdetached、local HEAD | スクリプトの配置先を作業repoにしない |
| ローカル変更 | staged/unstaged/untrackedの件数、取得上限 | dirtyだけで中身を読ませず、差分全文を出さない |
| ローカル最新commit | HEADの短いSHA、件名、日時 | commit日時をfetch日時にしない |
| upstream | upstream ref、手元の追跡refに対するahead/behind | GitHubの最新状態と称さない |
| GitHub基準 | repositoryのdefault branchとその最新SHA/commit件名 | default branchがmainとは限らない |
| 基準との比較 | local SHAとGitHub基準SHAの一致、比較可能ならahead/behind | SHAが違うだけで「rebase必要」と断定しない |
| 現在のPR | 現branchに対応するopen PRの番号/件名/URL/head/base/draft | ブランチ名だけでforkの別PRを選ばない |
| 直近のPR | 最新作成open PR 1件、最新更新open PR 1件、ほか最大3件 | created順とupdated順を混ぜた「最新」にしない |
| 直近コメント | 注目PRの最新一般コメントの著者/日時/短文/URL | review、inline、threadを一般コメントに混ぜない |
| レビュー | 最新review submission、観測した未解決thread件数と完全性 | CHANGES_REQUESTEDを重大度に変換しない |
| 同一headのCI | checksの状態/内訳、必要ならrunの状態とURL | 0 checksをgreenにせず、別SHAの成功を使わない |
| 続きを見る入口 | 実際のrepo/番号/SHAを入れた個別コマンドを最大3本 | 毎回help全文や全機能を並べない |

「注目PR」は一意に特定できた現在branchのopen PRを優先し、なければ
最新更新open PRとする。両者が異なる場合、双方の最新一般コメントを
最大1件ずつ表示する。最新作成PRは別枠で示す。
同じPRが複数枠に出る場合は表示を統合し、選択理由を複数記録する。
現在branchに複数の候補がある場合は候補を示し、一意と捏造しない。
closed/merged PRを含む履歴の全探索は追加コマンドに譲り、既定で走らせない。

## 既定出力のイメージ

以下は架空のrepo・SHA・件数を使った表示例で、実際の観測結果ではない。

```text
GHI overview | observed_at=2026-10-11T06:00:00Z | partial
repo: example/demo  (source: branch upstream remote)
cwd: /work/demo
local: work/fix-a @ aaaaaaa | staged=1 unstaged=0 untracked=2
local HEAD: 2026-10-11T05:00:00Z Add boundary check
upstream (local tracking ref): origin/work/fix-a | ahead=1 behind=0
GitHub default: main @ bbbbbbb | Update README
vs GitHub default: different | ahead/behind=unknown (remote SHA not local)
current PR: #42 Add boundary check | open, draft=false | head=aaaaaaa
  https://github.com/example/demo/pull/42
latest-created open PR: #43 Add example | created_at=2026-10-11T05:40:00Z
latest-updated open PR: #42 | updated_at=2026-10-11T05:55:00Z
latest conversation (#42): reviewer, 2026-10-11T05:55:00Z
  「境界ケースは確認済みです。READMEの例も更新してください。」
  https://github.com/example/demo/pull/42#issuecomment-123
reviews (#42): COMMENTED | unresolved threads=unknown (permission denied)
checks @ aaaaaaa: pending | success=5 in_progress=1
other open PRs (shown=1, total=unknown): #41 Update docs
next: python gh_identity.py observe-pr example/demo 42
next: python gh_identity.py review-observations example/demo 42
```

標準出力はこの程度の密度（目安25行・最大8 KiB）を上限とし、全コメント、
全diff、ジョブログ、artifact本文、全repo一覧、help全文を展開しない。
コメントpreviewは各240文字まで。改行/制御文字は表示用に正規化する。
秘密入りremote URL、トークン、生のAPIエラーメッセージを出力しない。
表示の短縮とAPI取得の不完全性は別々に記録する。

## repoとrefの決定

1. 明示 `--repo` があれば利用する。cwd由来repoとの相違も示す。
2. cwdのGit root・branch/upstreamから対象remoteを選択する。
3. upstreamがなければ、GitHub repoとして一意に特定できるremoteを利用する。
   複数候補が残るなら候補のみを出し、`--repo` による選択を案内する。
4. Git外では検証可能なCIの `GITHUB_REPOSITORY` を候補とする。
   local HEADとCIの `GITHUB_SHA` は別の観測として記録し、黙って置換しない。
5. repoが判定できなくてもローカル情報・判定できない理由はstdoutへ出す。
   非Gitディレクトリから全アカウントrepo探索を始めない。

URLから取得するのは検証したOWNER/REPOのみ。remoteの資格情報を表示しない。
ローカルrefとGitHubのdefault branchは別フィールドで保持する。
GitHubから得たSHAのobjectが手元になければahead/behindはunknown。
自動fetchで埋めず、必要なら利用者が次の操作を選べるようにする。

## 取得、上限と状態

- 既存 `repository` / `resolve_ref` / `pull_requests` / `comments` /
  `reviews` / `review_observations` / `checks_for_sha` / `head-runs` 系を再利用。
  呼び出し間で同じPR・head・metadataを再取得しないための共有観測を持つ。
- ローカル観測は既存のbounded Git inspection経路を再利用する。
  現行 `local_identity` の独自subprocess経路をコピーして新parserを増やさない。
  足りないroot/remote/ref比較は共通の検証済みwrapperに追加する。
- 最新一般コメントの取得は、API側の順序/末尾取得を保証できる経路にする。
  `comments(last=1)` が全ページを読む既存実装を既定入口でそのまま使わない。
  上限付き昇順の先頭ページを「最新コメント」と呼ばない。
- APIで保証されたcreated順/updated順とIDを記録する。同時刻ならIDで安定化。
  取得範囲内の最大日時しか保証できないなら `latest_observed` と明示する。
- 初期予算案: 操作全体20秒・HTTPページ/リクエスト12回・受信2 MiB。
  PR候補の表示は最大3件、一般コメント最大2件、threadは1ページ/20件まで。
  個々のAPI呼び出しで全体予算をリセットしない。
- 12回に収まらない詳細は追加コマンドへ譲る。完全性を捏造して予算を隠さない。
  まずlocal→default branch→PR選択→最新コメント→CI→threadの順で優先する。
- ネットワーク/APIが失敗しても取得済みlocal/remoteの観測を保持して出力する。
  401、403、404、timeout、missing、truncated、未実行を区別し、0件に置換しない。
- PR選択/CI/コメントを読む間のhead変更は、注目PRの最後のhead再確認で
  `stale=true` とする。詳細観測が予算不足なら未確認と記録する。
  overviewを自動merge/rebaseのゲートにせず、個別の厳密確認は既存CLIで行う。
- 終了コード案: 0=選んだonline/offlineモードの主要観測完了、
  1=概要は出したが一部unknown/partial、2=入力不正または概要自体を作れない。
  CI failed/pendingやdirtyは「観測された状態」であり、overview自体の失敗とは別。

既定入口は読み取り専用。fetch/pull/rebase/reset/checkout/dispatch/comment/merge
を実行しない。利用者はoverview後に既存Git/ghや書き込みAPIを自由に選べる。
`--help`や「必ずこれを使う」という強制ではなく、最初に読むと便利な入口にする。

## 受入条件と実装の切り分け

- 引数なし/cwdのrepo、subdirectory、非Git、detached HEAD、unborn HEAD、
  dirty、Git無し、upstream無し、複数remote、fork、CI環境不一致をfixture化。
- default branchがmain以外、remote SHAのlocal object無し、ローカルtracking refの
  古さが不明な場合を区別。SHA相違だけからbehind数やrebase必要性を創作しない。
- PR無し、branch一致が複数、最新作成と最新更新が別PR、同日時、コメント無し、
  100件超のコメント、ページ上限、権限不足、部分結果、途中head変更を検証。
- stdoutの予算とpreview短縮、秘密入りremote/端末制御文字の除去、stderr分離、
  text/JSONの観測一致、終了コード、既存CLI互換性を検証。
- fake transport/Git fixtureで、help/offline時の外部APIゼロ、全実行のmutationゼロ、
  全体のrequest/byte/time上限、重複取得抑制を検証する。
- README先頭の第一例を `python gh_identity.py` にする。SPECへ契約を追加し、
  pytest/JUnitとPython 3.10〜3.14＋最新stable CIで確認する。
- 実装は①boundedな概要API・引数なし分岐・renderer、②最新コメントの効率取得、
  ③fixture/docs/CIの順。既存のレビュー観測/dispatchの残作業は混ぜて完了にしない。

このPRの段階では提案の記録のみ。実装/実データでの取得件数・時間・出力サイズの
検証が済むまでは、引数なしoverviewが利用可能になったとは記載しない。
