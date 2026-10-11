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

overview（引数なしを含む）だけtextが既定で、既存subcommandはJSONのままとする。
README/SPECにこの差を明記する。argparseの共通parentで --json を定義し、
トップレベルとoverview parserの両方へ適用する。default=argparse.SUPPRESSとして
子parserの既定値が親の指定を上書きしないようにし、両位置と重複指定をfixture化する。

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
| 直近のIssue | 最新更新Issueをopen/closed両方から選び、番号・件名・state・updated_at・URL、ほか最大2件 | Issue更新をPR更新で代用せず、closedを未完了と称さない |
| Issueの最新コメント | 注目Issueの著者・created_at/updated_at・全文・元URL | Issue本体のupdated_atを最新コメント日時と混同しない |
| 直近コメント | 注目PRの最新一般コメントの著者/日時/全文/URL | review、inline、threadを一般コメントに混ぜない |
| レビュー | 最新review submission、観測した未解決thread件数と完全性 | CHANGES_REQUESTEDを重大度に変換しない |
| 同一headのCI | checksの状態/内訳、必要ならrunの状態とURL | 0 checksをgreenにせず、別SHAの成功を使わない |
| 続きを見る入口 | 実際のrepo/番号/SHAを入れた個別コマンドを最大3本 | 毎回help全文や全機能を並べない |

「注目PR」は一意に特定できた現在branchのopen PRを優先し、なければ
最新更新open PRとする。両者が異なる場合、双方の最新一般コメントを
最大1件ずつ表示する。最新作成PRは別枠で示す。
同じPRが複数枠に出る場合は表示を統合し、選択理由を複数記録する。
現在branchに複数の候補がある場合は候補を示し、一意と捏造しない。
closed/merged PRを含む履歴の全探索は追加コマンドに譲り、既定で走らせない。

IssueはPRとは独立して、updated_at降順でopen/closedを含む直近最大3件を
表示する。閉じられたIssueの直近更新も観測し、stateを明示する。
RESTのIssue一覧に含まれるPRは除外し、同じPRをIssue枠に重複表示しない。
「注目Issue」は最新更新Issueとし、その最新一般コメントを最大1件表示する。
注目Issueの本文も全文で添え、コメント無しでもIssue側の方針・残件が見えるようにする。
Issueの本文更新・close/reopen・ラベル等でupdated_atが変わっていても、
「最新コメントが付いた」「残件が完了した」とは推測しない。
最新コメントはcreated_atによる最新投稿とupdated_at（編集日時）を分ける。
現在PRとIssueの関連は明示的な参照・GitHubの関連情報が観測できた場合だけ示し、
番号や題名の類似から勝手に関連付けない。PRが無くてもIssue枠を出す。

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
latest-updated Issue: #40 Confirm rollout | open | updated_at=2026-10-11T05:58:00Z
  body: 「残件はIssueで追跡します。新しい確認結果を追加しました。」
  https://github.com/example/demo/issues/40
latest Issue comment (#40): maintainer | created_at=2026-10-11T05:58:00Z
  「追加検証が必要な項目をIssue本文に記録しました。」
  https://github.com/example/demo/issues/40#issuecomment-124
reviews (#42): COMMENTED | unresolved threads=unknown (permission denied)
checks @ aaaaaaa: pending | success=5 in_progress=1
other open PRs (shown=1, total=unknown): #41 Update docs
next: python gh_identity.py observe-pr example/demo 42
next: python gh_identity.py review-observations example/demo 42
```

標準出力は概要metadataを目安30行でまとめ、選択した最新コメントの本文は
引数なしで全文を出す。PR最大2件と注目Issue1件の最新一般コメントが対象。
注目Issueの本文も全文で出し、方針がコメントではなく本文にある場合も拾う。
ほかのIssue/PRは件名・state・日時・URLまでとし、履歴全件は展開しない。
metadataの目安をコメント本文へ適用せず、本文に240文字/8 KiB等の表示上限を
設けない。全文を読むための追加オプションや二回目の呼び出しを要求しない。
本文をAI要約・先頭抜粋・重要行抽出に置換しない。from/to等のヘッダも保持する。
stdoutの表示では本文の区切り、種類/id、著者、created_at/updated_at、元URL、
原文のchars/body_sha256を併記し、全文の範囲を明示する。
本文の改行・段落・空白を保つ。危険な端末制御文字は可逆なエスケープ表現にして
示し、元の本文を黙って削除しない。textの規則は先に「\ → \\」、
続いてLF/TAB以外のC0（CRを含む）・DEL・C1を「\xNN」（2桁小文字hex）へ
変換する。LF/TABはそのまま保持する。literalな「\x1b」と実際のESCを区別し、
逆変換fixtureで原文に戻ることを検証する。JSONではbodyに完全な原文を保持する。
秘密入りremote URL、トークン、生のAPIエラーメッセージを出力しない。
取得自体のbyte/time予算は維持するが、取得途中の本文を「全文」として出さない。
本文が予算を超える場合はその対象を取得不足と明示し、原因・id・元URLを出す。
これは正常時に抜粋へ置換する仕様ではなく、完全取得できなかった失敗状態である。
CIで実行するとstdoutの本文全文がActions等のログに保存される。private repoでも
同じ挙動であることをREADME/SPECに明記する。ユーザー指定の全文表示は維持し、
CI検証はsynthetic fixtureを使ってprivateな実本文をログへ出さない。

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

- 既存 `repository` / `resolve_ref` / `pull_requests` / `issues` / `issue` / `comments` /
  `reviews` / `review_observations` / `checks_for_sha` / `head-runs` 系を再利用。
  呼び出し間で同じPR・head・metadataを再取得しないための共有観測を持つ。
- ローカル観測は既存のbounded Git inspection経路を再利用する。
  現行 `local_identity` の独自subprocess経路をコピーして新parserを増やさない。
  足りないroot/remote/ref比較は共通の検証済みwrapperに追加する。
- 更新順候補は `issues?state=all&sort=updated&direction=desc&per_page=100`
  を最大2ページ読み、`pull_request` キーでPR/Issueを分離する。同じ応答を共有し、
  open PRとopen/closed Issueの候補を別々に選ぶ。最新作成open PRは
  `pulls?state=open&sort=created&direction=desc` の1ページで取得する。
  既存の正規化を再利用する薄い候補取得を追加し、同じAPIを別用途で重複取得しない。
- 2ページがPRばかりでIssueが見つからなくても「Issueなし」としない。
  `selection_status=not_found_within_window`、`latest=null`、
  `pages_fetched=N`、`exhausted=false`、`next_page` を保持し、
  textは「最新Issue: 未観測（Nページ内に見つからず）」とする。
  全ページの終端を確認して初めて `selection_status=none` を許容する。
  権限不足/timeoutは別の `selection_status=error`。候補の表示上限と
  最新候補の選択/一覧の完全性を別フィールドにする。
- 最新一般コメントは `issues/{n}/comments?per_page=1&page=1` の
  response Linkを読み、`rel="last"` があればその末尾ページを1回取得する。
  静止した一覧なら1対象につき最大2回（0/1件なら通常1回）で本文全文を取れる。
  全履歴の `comments(last=1)` は使わず、無効なdirection指定にも依存しない。
  注目Issue本文は更新順候補のbodyを再利用し、余分なIssue GETを避ける。
- 末尾ページ方式は同時投稿/削除がある一覧の原子的な最新保証ではない。
  最後の応答にさらにnextがあれば `latest_observed` として不完全性を示し、
  空になった末尾、矛盾したLink、同時更新を観測した場合も完了と称さない。
  自動再探索で予算を超えない。page番号を永続的な件数と扱わない。
- gh/urllib共通transportでresponse headersをboundedに取り出せる拡張が必要。
  現行JSON-only transportからLinkが読めると仮定しない。
  rel=lastはHTTPS/api.github.com・同じrepo/number/path・per_page=1・正のpageを
  検証し、任意URLを追わない。API認証を外部URLへ転送しない。
- 最新review submissionもbounded末尾取得を使い、最大2回を予約する。
  一般コメント/review/inline/threadの種類は保持し、任意本文から重要度を推測しない。
- APIで保証されたcreated順/updated順とIDを記録する。同時刻の安定化は
  観測内に限り、ページ外を含む全順序を保証しない。
- 操作全体の初期予算を20秒・HTTP要求20回・受信2 MiBへ変更する。
  HTTP要求は失敗/transport fallbackも数え、個々のAPIで予算をリセットしない。
  基本取得の設計上限は下表の19回で、残り1回を予備とする。
  rate limit/timeout/追加paginationが必要ならその項目をpartial/unknownで止める。

| 取得 | 最大HTTP要求 | 共有/上限 |
|---|---:|---|
| repository/default branch名 | 1 | 共有metadata |
| default branchの最新commit | 1 | SHA/件名を同時取得 |
| 現branchのopen PR候補 | 1 | head repo/ref照合、曖昧なら候補 |
| 最新作成open PR | 1 | 1ページ |
| 最新更新PR/Issueの混在候補 | 2 | 最大2ページを両用途で共有 |
| 注目PRの不足metadata | 1 | current PR取得/候補で揃えば0回 |
| 最新一般コメント（PR最大2＋Issue1） | 6 | 各対象最大2回、同一PRは統合 |
| 注目PRの最新review submission | 2 | 末尾取得、状態/IDを保持 |
| 注目PR同一headのchecks | 1 | 1ページ、不足はincomplete |
| 注目PRのthread概要 | 1 | 1ページ/20件、返信の追加取得なし |
| コメント対象PRのhead再確認 | 2 | 各1回、staleを検出 |
| 合計 | 19 | 操作上限20回、予備1回 |

- PR候補とIssue候補は各最大3件。thread詳細をoverviewでfan-outしない。
  `checks_for_sha` 等の現行関数が全ページを読む場合はoverview向けのbounded
  呼出し口を追加し、一覧の全体を完全取得したと誤記しない。run/log/artifact取得は
  既定overviewで追加実行せず、既存コマンドを次の入口として示す。
- GraphQLが403等で使えない場合は1回で打ち切り、再試行やRESTからの解決状態推測を
  行わない。`unknown (graphql unavailable)` と元のエラー分類を保持する。
  permission/rate limit/proxyを識別できない応答からプロキシ原因と断定しない。
- まずlocal→default branch→PR/Issue選択→双方の本文全文→head再確認→CI→review→thread
  の順で優先する。追加paginationやfallbackで余裕が減った場合はreview/threadを
  未観測にして本文を優先し、通常時の全文表示を省略オプションへ変更しない。
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
- PR無しでIssueだけある、closed Issueが最新更新、Issue本文だけの更新、
  古いコメントの編集、Issueコメント無し、PR混在ページ、Issue検索/一覧の
  上限到達、2ページ全部PR、終端確認済み0件、末尾コメントの追加/削除を検証。
  未観測と0件を区別し、PR/Issueのリンクを創作しない。
- 長文の結論が末尾、先頭がfrom/to等のヘッダ、日本語/絵文字/改行、短い本文、
  空本文をfixtureに含める。引数なしで末尾まで原文が保持され、240文字や8 KiBで
  切られないこと、chars/hash/source identityと取得上限の失敗表示を検証する。
- stdoutのmetadata件数上限と本文全文の保持、秘密入りremote/端末制御文字の除去、stderr分離、
  text/JSONの観測一致、バックスラッシュ/実ESC/CR/LF/TABの逆変換、
  --jsonの両配置、GraphQL失敗時の再試行ゼロ、終了コード、既存CLI互換性を検証。
- fake transport/Git fixtureで、help/offline時の外部APIゼロ、全実行のmutationゼロ、
  全体のrequest/byte/time上限、19回の最大構成・20回を超えないfallback、
  本文優先の打ち切り、重複取得抑制を検証する。
- README先頭の第一例を `python gh_identity.py` にする。SPECへ契約を追加し、
  pytest/JUnitとPython 3.10〜3.14＋最新stable CIで確認する。
- 実装は①boundedな概要API・引数なし分岐・renderer、②最新コメントの効率取得、
  ③fixture/docs/CIの順。既存のレビュー観測/dispatchの残作業は混ぜて完了にしない。

このPRの段階では提案の記録のみ。実装/実データでの取得件数・時間・出力サイズの
検証が済むまでは、引数なしoverviewが利用可能になったとは記載しない。

## レビュー反映記録（2026-10-11）

review 5482275860のS1/S2/N1〜N4を仕様へ反映。HTTP予算は内訳19回＋予備1回、
混在Issue窓の未観測、末尾コメントとLink対応、可逆escape、overviewだけtext、
GraphQL再試行禁止、CIへの全文記録を明文化。実装/動作検証は後続のまま。

API根拠: [Issue comments](https://docs.github.com/en/rest/issues/comments)、
[pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)。
