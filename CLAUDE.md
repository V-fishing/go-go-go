# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Windows-only toolset that plays and analyzes Go (围棋) on the **腾讯围棋** (Tencent Go) mini-program running inside WeChat. It captures the game window with `PIL.ImageGrab`, locates the board with computer vision, reads stone colors, queries the local **KataGo** engine, and places stones by posting Win32 mouse messages directly to the render window — no cursor movement required, so the user keeps mouse control.

There is no build system, no test suite, and no package manager. Every script is run directly with `python <script>.py`. Python dependencies: `numpy`, `Pillow` (PIL). It also requires a local KataGo engine + weights (see paths below).

## How to run

| Command | Purpose |
|---|---|
| `python katago_ui.py` | GUI launcher (main entry) — spawns the tools below and tails their log |
| `python katago_play.py white --turn black` | Auto-play as white; `--turn` sets whose move it is now |
| `python katago_suggest.py [--watch]` | Read-only position analysis (recommendations, no moves) |
| `python board_reader.py [--png P] [--no-calib]` | Dump the current board state to stdout (debug the CV pipeline) |
| `python select_board.py` | GUI: drag a rectangle over the board, save `grid_calib.json` |
| `python align_overlay.py` | Transparent grid overlay for manual grid calibration |

Shared flags for `katago_play.py` / `katago_suggest.py`: `--size 9|13|19` (force board size, 0=auto), `--visits N` (KataGo search effort). `katago_play.py` additionally accepts `--wait-new` (auto-rematch after game end), `--beep`, `--force-any` (skip page guard), `--detect-first` (empty-board color detection).

## Architecture

Pipeline: capture window → locate grid → align → classify stones → (query KataGo → click).

- `board_reader.py` — the CV core. `read_current()` returns `{n, board, xs, ys, src, step, stone_r, img, rect, drift}` or `None`. Grid source priority: `grid_calib.json` calibration (screen coords) → full auto-detection (`locate_board`, which tries line detection then star-point detection). `board` is a list of strings: `'X'`=black, `'O'`=white, `'.'`=empty, `'?'`=out of bounds.
- `katago_client.py` — `KataClient`, a persistent subprocess client for `katago.exe analysis` (JSON-lines protocol). `query(req)` returns a response dict or `None` (auto-restarts the process once). Model selected by board size via `model_for_size`.
- `katago_play.py` — the auto-player state machine. Turn flips only on two events: a confirmed board change (opponent moved) or our own successful placement. Handles rematch/endgame detection, a stall watchdog, and board-size changes.
- `katago_suggest.py` — read-only analysis (winrate + top moves), single-shot or `--watch` polling.
- `katago_ui.py` — Tkinter launcher; spawns scripts as subprocesses and tails `katago_ui.log`.
- `select_board.py` / `align_overlay.py` — two calibration tools, both write `grid_calib.json` (different schemas: screen rectangle vs. origin+step).
- `ocr.ps1` — PowerShell wrapper over Windows built-in OCR; used by `katago_play.py` to locate UI buttons (e.g. "重新匹配" rematch).

## Critical hard-coded values (edit before reuse)

- `board_reader.py:26` — `PID = 26768`, the process ID of the `WeChatAppEx.exe` (腾讯围棋) window. This is session-specific and MUST match the running game or every read fails. `align_overlay.py:31` duplicates it.
- Engine/weights paths are hard-coded in `katago_client.py` and `katago_play.py`: `KATAGO` (engine exe), `MODEL19` / `MODEL9` (weight `.bin.gz`), `CFG` (analysis config).
- `LETTERS = 'ABCDEFGHJKLMNOPQRST'` — Go coordinates (skips `I`). Board cell `(i, j)` maps to `LETTERS[j] + str(n - i)`.
- Rules are Chinese with komi 7.5, set in the analysis request payloads.

## Go-specific conventions

- Supported board sizes: 9, 13, 19 (star-point tables in `board_reader.py:STAR_POINTS`).
- Board strings use black=`'X'`, white=`'O'`; the KataGo `initialStones` payload uses `['b', 'A1']` / `['w', 'B2']`.
- `grid_calib.json` holds screen-space calibration consumed by `read_current`; `grid.json` is an older window-space snapshot; `lum_grid.npy` is a cached luminance grid.
