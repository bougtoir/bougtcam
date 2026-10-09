# BougtCam 動作検証結果

検証日: 2026-10-09 (UTC)
環境: Windows Server 2022 (Devin VM, RDP 1024x768), Python 3.13.12, PyInstaller 6.22.3, PySide6 6.12.0

## 検証環境の制約（重要）

- **物理カメラなし**: 本 VM には dshow ビデオデバイスが存在しなかったため、仮想 DirectShow デバイス **screen-capture-recorder v0.13.2**（デスクトップキャプチャ源、実フレーム出力）をインストールして実録画検証に使用。実 USB カメラでの検証は未実施。
- winget 非搭載のため OBS Studio は未使用。
- トレイメニューは本画面解像度で下端クリップされ、項目の直接クリックは不可能だった。メニュー自体は開きステータス表示は確認済。項目が呼ぶ recorder.stop/shutdown はウィンドウの録画停止ボタンとプロセス終了(WM_QUIT)で同一経路を検証。

## ffmpeg / デバイス列挙

- 同梱 ffmpeg: `ffmpeg version 9.0.2-essentials_build-www.gyan.dev` (GPL, libx264 有効)
- `ffmpeg.exe -list_devices true -f dshow -i dummy` 結果:
  - `"screen-capture-recorder" (video)` — 仮想デバイス（インストール後に列挙）
  - `"virtual-audio-capturer" (none)` — 同梱の音声源（オーディオエンドポイントなし環境）
- インストール前はデバイス0件（`Could not enumerate video devices`）

## 単体テスト

`python -m pytest tests -q` → **9 passed**（モード解析・選択・フォールバック・コマンド生成・デバイス列挙パース新旧形式・連番）

## 仕様書セクション9 チェックリスト

| 項目 | 結果 | 証拠 |
|---|---|---|
| 起動即録画（auto start） | PASS | 起動→カメラ検出→モード列挙→自動で録画開始（ログ: status recording, tray icon 赤）|
| 最大解像度選択 | PASS | 広告モード中の最大画素数 1600x1200@30 を選択（トレイメニュー/ウィンドウに「解像度: 1600x1200 @30fps」表示）|
| 最小化・背面でも録画継続 | PASS | ウィンドウ非表示（トレイ格納）後も mkv が継続成長、Chrome 前面でも継続 |
| トレイ停止 | PASS* | トレイメニュー項目の直接クリックは解像度制約で未検証。同一の `recorder.stop` を呼ぶウィンドウ「録画停止」ボタンで検証（停止→待機状態）|
| MP4 ファイナライズ | PASS | 停止時 `remuxing ...mkv -> .mp4` → `saved`（ログ）。生成 mp4 は ffmpeg で h264 1600x1200・正常 Duration 確認 |
| 再録画で新規ファイル | PASS | 録画停止→録画開始で新 mkv `YYYY-MM-DD_HH-MM-SS.mkv` が別名で生成 |
| 終了時の最終化とクリーン終了 | PASS* | トレイ「終了」の直接クリックは未検証だが、`app.quit`→`aboutToQuit`→`recorder.shutdown` と同一経路（WM_QUIT）で停止→remux→mp4 保存→プロセス終了を確認 |
| カメラ切断検出と復帰 | PASS（代替検証） | 録画中に ffmpeg 子プロセスを強制終了 → watchdog が crashed 検出 → mkv を mp4 に最終化 → 新 mkv で録画再開。物理抜挿は UNTESTED |
| 保存先自動作成 | PASS | `C:\Users\bougt\video` が起動時に自動作成された |
| exe 起動 | PASS | `dist\BougtCam\BougtCam.exe` がコンソールなしで起動・常駐 |

※ UNTESTED 残項: 物理カメラでの実録画、トレイメニュー項目の直接クリック（解像度依存の環境制約）、音声録音、HW エンコーダ実使用（本 VM に GPU なし、nvenc/qsv/amf は即エラー→libx264 へ正しくフォールバックすることを確認）。

## 今回修正したビルド/動作不良（コミット内容）

1. **同梱 ffmpeg が見つからない**: PyInstaller onedir では `_internal\ffmpeg\ffmpeg.exe` に配置されるが `find_ffmpeg` が未対応 → 探索パス追加。
2. **ffmpeg 9.x でカメラ未検出**: `-list_devices` の出力形式が「セクションヘッダ」から行内 `(video)`/`(audio)` タグに変更済み → 両形式対応のパーサに修正。
3. **確認フェーズでキャプチャが常に失敗**: ffmpeg の stderr を確認完了まで誰も読まず、pipe バッファが warnings で満杯になり ffmpeg がブロック → spawn 時点から stderr/stdout を常時ドレイン。さらに低ビットレート源では matroska muxer のフラッシュが疎で「ファイル成長」が生存判定に使えない → `-progress pipe:1` の `out_time_ms` を生存信号として confirm/watchdog の双方で併用。
4. **`with_encoder` が `-preset veryfast -crf 20` を残したまま hw エンコーダへ差し替え**（h264_amf/nvenc が preset 値で即エラー）→ 既知エンコーダオプションを正しく除去。
5. **二重起動防止が機能していない**: ロックファイルは保持されておらず FileExistsError で常に削除→再作成されていた → ロック内 pid の生存確認（GetExitCodeProcess）を実装し、生存時は静かに終了。

## 付記

- `no_camera` 状態: 仮想デバイス登録前の初回起動で「カメラ未検出。再接続を待機中」へ遷移し待機し続けることを確認。
- 音声は既定オフのため未検証（本環境に音声エンドポイントもなし）。
