import threading
import os
from flask import Flask, jsonify, render_template_string, send_from_directory, request
from db.database import TranscriptionDB
from config import DB_PATH, ASR_PRIMARY, OPENAI_API_KEY

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["PANEL_PORT"] = 5000

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _allowed_hosts() -> set:
    port = app.config["PANEL_PORT"]
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


@app.before_request
def _guard_host_and_origin():
    """Block DNS rebinding and cross-site writes against the local panel.

    - Host must be the loopback address the server really listens on; a
      rebinding page reaches us with Host: evil.com:<port>.
    - On state-changing methods a present Origin must be one of our own
      origins, and Sec-Fetch-Site (if sent) must not be cross-site. A missing
      Origin is allowed: browsers always send it on cross-origin POSTs, so
      only non-browser clients (curl, scripts) omit it.
    """
    allowed = _allowed_hosts()
    if request.host not in allowed:
        return jsonify({"error": "forbidden host"}), 403
    if request.method in _STATE_CHANGING_METHODS:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in allowed}:
            return jsonify({"error": "forbidden origin"}), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify({"error": "forbidden origin"}), 403

# Shared dictionary/transcriber references (set by main.py)
_dictionary = None
_transcriber = None

def set_dictionary(dictionary):
    global _dictionary
    _dictionary = dictionary

def set_transcriber(transcriber):
    """Needed for the dashboard's re-transcribe action (Feature 1)."""
    global _transcriber
    _transcriber = transcriber

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>fluentpill — Historial</title>
    <link rel="icon" type="image/svg+xml" href="/brand/favicon.svg">
    <link rel="icon" type="image/png" sizes="32x32" href="/brand/favicon-32.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        /* ── Reset ──────────────────────────────────────────────────────────── */
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        /* ── Design tokens ──────────────────────────────────────────────────── */
        :root {
            /* Palette */
            --bg-base:         #050505;
            --surface-1:       rgba(255,255,255,0.025);
            --surface-2:       rgba(255,255,255,0.04);
            --surface-3:       rgba(255,255,255,0.07);
            --border-hairline: rgba(255,255,255,0.07);
            --border-accent:   rgba(255,255,255,0.14);
            --border-dirty:    rgba(251,191,36,0.35);
            --text-primary:    #f4f4f5;
            --text-muted:      #71717a;
            --text-dim:        #3f3f46;
            --accent:          #22D3EE;
            --accent-dim:      rgba(34,211,238,0.10);
            --success:         #34d399;
            --success-dim:     rgba(52,211,153,0.10);
            --danger:          #f87171;
            --danger-dim:      rgba(248,113,113,0.10);
            --warning:         #fbbf24;
            --warning-dim:     rgba(251,191,36,0.10);

            /* Typography */
            --font-ui:   'Geist', system-ui, -apple-system, sans-serif;
            --font-mono: 'Geist Mono', 'JetBrains Mono', monospace;

            /* Shadows */
            --shadow-card:       0 1px 3px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.05);
            --shadow-card-hover: 0 4px 16px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.10);
            --shadow-inset-top:  inset 0 1px 0 rgba(255,255,255,0.08);
            --shadow-button:     0 1px 2px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.06);
            --shadow-focus-ring: 0 0 0 2px rgba(34,211,238,0.4);

            /* Easing */
            --ease-spring: cubic-bezier(0.32, 0.72, 0, 1);
        }

        /* ── Base ───────────────────────────────────────────────────────────── */
        html, body { height: 100%; }

        body {
            background: var(--bg-base);
            color: var(--text-primary);
            font-family: var(--font-ui);
            font-size: 14px;
            line-height: 1.55;
        }

        /* Mesh gradient — fixed, pointer-events none */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            pointer-events: none;
            z-index: -1;
            background:
                radial-gradient(ellipse 60% 40% at 5% 10%, rgba(99,102,241,0.05) 0%, transparent 70%),
                radial-gradient(ellipse 50% 35% at 95% 90%, rgba(16,185,129,0.03) 0%, transparent 70%);
        }

        /* Grain overlay — fixed, pointer-events none */
        body::after {
            content: '';
            position: fixed;
            inset: 0;
            pointer-events: none;
            z-index: 100;
            opacity: 0.03;
            mix-blend-mode: overlay;
            background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='120'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='1' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='0.6'/></svg>");
            background-repeat: repeat;
        }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border-hairline); border-radius: 2px; }

        /* ── Layout ─────────────────────────────────────────────────────────── */
        .container {
            max-width: 1180px;
            margin: 0 auto;
            padding: 0 24px 96px;
        }
        @media (min-width: 680px) {
            .container { padding-left: 48px; padding-right: 48px; }
        }

        /* ── Header ─────────────────────────────────────────────────────────── */
        .header {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 24px 0 20px;
        }

        /* Lockup (symbol + wordmark). The SVG carries ~25% empty margin
           around the artwork, so the negative margin gives it back. */
        .logo {
            height: 44px;
            width: auto;
            margin: -6px -2px -6px -8px;
            flex-shrink: 0;
        }

        .header-sub {
            font-size: 11px;
            color: var(--text-muted);
            letter-spacing: 0.01em;
            line-height: 1.4;
        }

        .count-badge {
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
        }

        /* Search — pushes right */
        .search-wrap {
            margin-left: auto;
            position: relative;
            display: flex;
            align-items: center;
        }

        .search-icon {
            position: absolute;
            left: 10px;
            top: 50%;
            transform: translateY(-50%);
            pointer-events: none;
            color: var(--text-dim);
        }

        .search-input {
            background: var(--surface-1);
            border: 1px solid var(--border-hairline);
            border-radius: 10px;
            padding: 7px 12px 7px 32px;
            font-size: 13px;
            font-family: var(--font-ui);
            color: var(--text-primary);
            outline: none;
            width: 180px;
            transition: width 300ms var(--ease-spring), border-color 200ms ease, box-shadow 200ms ease;
        }
        .search-input::placeholder { color: var(--text-muted); }
        .search-input:focus {
            width: 280px;
            border-color: var(--accent);
            box-shadow: var(--shadow-focus-ring);
        }

        /* The lockup is wider than the old icon + word, so on phones the
           search drops to its own row instead of running off the screen. */
        @media (max-width: 479px) {
            .header { flex-wrap: wrap; row-gap: 8px; }
            .search-wrap { margin-left: 0; width: 100%; }
            .search-input, .search-input:focus { width: 100%; }
        }

        /* ── Error banner ───────────────────────────────────────────────────── */
        .error-banner {
            background: var(--danger-dim);
            border: 1px solid rgba(248,113,113,0.20);
            border-radius: 10px;
            padding: 10px 14px;
            margin-bottom: 16px;
            font-size: 11px;
            color: var(--danger);
            opacity: 0;
            transition: opacity 300ms ease;
            display: none;
        }
        .error-banner.visible {
            display: block;
            opacity: 1;
        }

        /* ── Cards list ─────────────────────────────────────────────────────── */
        #cards-list {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        /* ── Card — double bezel ────────────────────────────────────────────── */
        .card-outer {
            background: var(--surface-1);
            border: 1px solid var(--border-hairline);
            border-radius: 20px;
            padding: 6px;
            box-shadow: var(--shadow-card);
            transition:
                box-shadow 250ms var(--ease-spring),
                border-color 250ms var(--ease-spring);
        }
        .card-outer:hover {
            box-shadow: var(--shadow-card-hover);
            border-color: var(--border-accent);
        }

        /* New card entry animation */
        .card-outer.card-new {
            animation: cardEntry 500ms var(--ease-spring) forwards;
        }
        @keyframes cardEntry {
            from { opacity: 0; transform: translateY(-12px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        /* Ephemeral accent border on newly refreshed card */
        .card-outer.card-fresh {
            border-color: rgba(34,211,238,0.4);
        }

        .card-inner {
            background: var(--surface-2);
            border-radius: 14px;
            padding: 14px 16px 16px;
            box-shadow: var(--shadow-inset-top);
        }

        /* ── Card meta row ──────────────────────────────────────────────────── */
        .card-meta {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
        }

        .meta-time {
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
        }

        .meta-sep {
            font-size: 12px;
            color: var(--text-dim);
        }

        .meta-dur {
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
        }

        .badge-ai {
            font-size: 11px;
            font-weight: 500;
            background: var(--success-dim);
            color: var(--success);
            border-radius: 5px;
            padding: 1px 5px;
            cursor: default;
        }

        /* ── Two-column textarea grid ───────────────────────────────────────── */
        .card-cols {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        @media (max-width: 639px) {
            .card-cols { grid-template-columns: 1fr; }
        }

        .col-label {
            font-size: 11px;
            font-weight: 400;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            line-height: 1.4;
            margin-bottom: 6px;
        }

        .ta {
            width: 100%;
            min-height: 72px;
            max-height: 300px;
            overflow-y: auto;
            resize: vertical;
            background: rgba(0,0,0,0.20);
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 10px;
            padding: 10px 12px;
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-primary);
            outline: none;
            line-height: 1.5;
            transition: border-color 200ms ease, box-shadow 200ms ease;
        }
        .ta:focus {
            border-color: var(--border-accent);
            box-shadow: var(--shadow-focus-ring);
        }
        .ta.dirty {
            border-color: var(--border-dirty);
            box-shadow: 0 0 0 2px rgba(251,191,36,0.12);
        }
        /* dirty wins over focus */
        .ta.dirty:focus {
            border-color: var(--border-dirty);
            box-shadow: 0 0 0 2px rgba(251,191,36,0.18);
        }

        .col-actions {
            display: flex;
            justify-content: flex-end;
            margin-top: 6px;
        }

        /* ── Copy button ────────────────────────────────────────────────────── */
        .btn-copy {
            font-size: 11px;
            font-weight: 500;
            font-family: var(--font-ui);
            padding: 4px 10px;
            border-radius: 999px;
            cursor: pointer;
            background: transparent;
            border: 1px solid var(--border-hairline);
            color: var(--text-muted);
            transition:
                background 150ms ease-out,
                color 150ms ease-out,
                border-color 150ms ease-out,
                transform 150ms ease-out;
        }
        .btn-copy:hover {
            background: var(--surface-3);
            color: var(--text-primary);
            border-color: var(--border-accent);
            transform: scale(1.02);
        }
        .btn-copy:active {
            transform: scale(0.97);
            transition-duration: 80ms;
        }
        .btn-copy.copied {
            color: var(--success);
            border-color: var(--success);
            transition:
                color 150ms ease,
                border-color 150ms ease;
        }
        .btn-copy.copied-reset {
            transition:
                color 200ms ease,
                border-color 200ms ease;
        }

        /* ── Save button ────────────────────────────────────────────────────── */
        .card-save-wrap {
            margin-top: 12px;
        }

        .btn-save {
            width: 100%;
            font-size: 13px;
            font-weight: 500;
            font-family: var(--font-ui);
            padding: 10px 16px;
            border-radius: 8px;
            cursor: pointer;
            background: var(--warning-dim);
            border: 1px solid rgba(251,191,36,0.25);
            color: var(--warning);
            opacity: 0;
            transform: translateY(4px);
            transition:
                opacity 300ms var(--ease-spring),
                transform 300ms var(--ease-spring),
                background 150ms ease-out,
                transform 150ms ease-out;
            /* Hidden by default — no display:none to allow animation */
            pointer-events: none;
        }
        .btn-save.visible {
            opacity: 1;
            transform: translateY(0);
            pointer-events: auto;
        }
        .btn-save:hover {
            filter: brightness(1.1);
            transform: scale(0.99);
        }
        .btn-save:active {
            transform: scale(0.97);
            transition-duration: 80ms;
        }
        .btn-save.saving {
            cursor: default;
            pointer-events: none;
            opacity: 0.75;
        }
        .btn-save.saved {
            background: var(--success-dim);
            border-color: rgba(52,211,153,0.25);
            color: var(--success);
        }
        .btn-save.fading {
            opacity: 0;
            transition: opacity 250ms ease-in;
        }

        /* Save error */
        .save-error {
            font-size: 11px;
            color: var(--danger);
            margin-top: 6px;
            opacity: 0;
            transition: opacity 200ms ease;
            display: none;
        }
        .save-error.visible {
            display: block;
            opacity: 1;
        }

        /* ── Skeleton loading ───────────────────────────────────────────────── */
        @keyframes shimmer {
            0%   { background-position: -200% 0; }
            100% { background-position:  200% 0; }
        }
        .skeleton-block {
            background: linear-gradient(
                90deg,
                rgba(255,255,255,0.03) 0%,
                rgba(255,255,255,0.07) 50%,
                rgba(255,255,255,0.03) 100%
            );
            background-size: 200% 100%;
            animation: shimmer 1.5s infinite ease-in-out;
            border-radius: 6px;
        }
        .skeleton-meta { height: 12px; width: 140px; margin-bottom: 12px; }
        .skeleton-body { height: 64px; margin-bottom: 8px; }
        .skeleton-body-short { height: 64px; }

        /* ── Empty state ────────────────────────────────────────────────────── */
        .empty-state {
            padding: 80px 0;
            text-align: center;
            display: none;
        }
        .empty-state.visible { display: block; }

        .empty-icon {
            display: flex;
            justify-content: center;
            margin-bottom: 16px;
        }

        /* Bar animation */
        .empty-bar {
            transform-origin: bottom center;
            transform: scaleY(0);
        }
        .empty-state.visible .empty-bar:nth-child(1) {
            animation: barUp 400ms var(--ease-spring) 0ms forwards;
        }
        .empty-state.visible .empty-bar:nth-child(2) {
            animation: barUp 400ms var(--ease-spring) 80ms forwards;
        }
        .empty-state.visible .empty-bar:nth-child(3) {
            animation: barUp 400ms var(--ease-spring) 160ms forwards;
        }
        @keyframes barUp {
            from { transform: scaleY(0); }
            to   { transform: scaleY(1); }
        }

        .empty-title {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-muted);
            line-height: 1.3;
            margin-bottom: 8px;
        }

        .empty-hint {
            font-size: 12px;
            color: var(--text-dim);
            line-height: 1.4;
        }

        /* No results inline */
        .no-results {
            font-size: 11px;
            color: var(--text-dim);
            padding: 20px 0;
            display: none;
        }
        .no-results.visible { display: block; }

        /* Cards hidden during filter */
        .card-outer.filtered-out {
            opacity: 0.25;
            pointer-events: none;
            transition: opacity 200ms ease;
        }

        /* ── Footer ─────────────────────────────────────────────────────────── */
        .footer {
            padding: 48px 0 32px;
            text-align: center;
            font-size: 11px;
            color: var(--text-dim);
            letter-spacing: 0.01em;
            line-height: 1.4;
        }
        .footer-sep { margin: 0 6px; }

        /* ── Tools bar: dictionary + cleanup ─────────────────────────────────── */
        .tools-bar {
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 20px;
            align-items: start;
            margin: 4px 0 20px 0;
            padding: 14px 16px;
            background: var(--surface-1);
            border: 1px solid var(--border-hairline);
            border-radius: 16px;
        }
        @media (max-width: 720px) {
            .tools-bar { grid-template-columns: 1fr; }
        }
        .tools-label {
            font-size: 11px;
            font-weight: 500;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .tools-label .count {
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-dim);
            font-variant-numeric: tabular-nums;
            font-weight: 400;
            text-transform: none;
            letter-spacing: 0;
        }

        /* Dictionary chips */
        .dict-chips {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 8px;
            min-height: 22px;
        }
        .dict-chip {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-family: var(--font-mono);
            font-size: 12px;
            font-weight: 400;
            color: var(--text-primary);
            background: var(--surface-2);
            border: 1px solid var(--border-hairline);
            border-radius: 5px;
            padding: 3px 6px 3px 9px;
            transition: border-color 150ms ease-out, background 150ms ease-out;
        }
        .dict-chip:hover {
            border-color: var(--border-accent);
            background: var(--surface-3);
        }
        .dict-chip-x {
            cursor: pointer;
            background: transparent;
            border: none;
            color: var(--text-dim);
            font-size: 13px;
            line-height: 1;
            padding: 0 2px;
            font-family: var(--font-ui);
            transition: color 150ms ease-out;
        }
        .dict-chip-x:hover { color: var(--danger); }
        .dict-empty {
            font-size: 12px;
            color: var(--text-dim);
            padding: 2px 0;
        }
        .dict-add-row {
            display: flex;
            gap: 6px;
        }
        .dict-add-input {
            flex: 1;
            min-width: 0;
            background: rgba(0, 0, 0, 0.20);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 8px;
            padding: 6px 10px;
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-primary);
            outline: none;
            transition: border-color 200ms ease, box-shadow 200ms ease;
        }
        .dict-add-input::placeholder { color: var(--text-dim); }
        .dict-add-input:focus {
            border-color: var(--border-accent);
            box-shadow: var(--shadow-focus-ring);
        }
        .dict-add-btn {
            background: var(--accent-dim);
            border: 1px solid rgba(34,211,238, 0.25);
            border-radius: 8px;
            padding: 6px 12px;
            font-family: var(--font-ui);
            font-size: 12px;
            font-weight: 500;
            color: var(--accent);
            cursor: pointer;
            transition: background 150ms ease-out, transform 80ms ease;
        }
        .dict-add-btn:hover { background: rgba(34,211,238, 0.18); }
        .dict-add-btn:active { transform: scale(0.97); }
        .dict-add-btn:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }

        /* ── Vocabulary suggestions (passive auto-learn candidates) ──────────── */
        .vocab-bar {
            margin: 4px 0 20px 0;
            padding: 14px 16px;
            background: var(--surface-1);
            border: 1px solid var(--border-hairline);
            border-radius: 16px;
        }
        .vocab-sugg-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .vocab-sugg-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 10px;
            background: var(--surface-2);
            border: 1px solid var(--border-hairline);
            border-radius: 10px;
        }
        .vocab-sugg-word {
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 500;
            color: var(--text-primary);
            white-space: nowrap;
        }
        .vocab-sugg-seen {
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--text-dim);
            white-space: nowrap;
        }
        .vocab-sugg-sample {
            flex: 1;
            min-width: 0;
            font-size: 12px;
            color: var(--text-muted);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .vocab-sugg-actions {
            display: flex;
            gap: 6px;
            flex-shrink: 0;
        }
        .vocab-sugg-btn {
            font-family: var(--font-ui);
            font-size: 11px;
            font-weight: 500;
            padding: 5px 12px;
            border-radius: 999px;
            cursor: pointer;
            transition: background 150ms ease-out, color 150ms ease-out,
                        border-color 150ms ease-out, transform 80ms ease;
        }
        .vocab-sugg-btn:active { transform: scale(0.97); }
        .vocab-sugg-approve {
            background: var(--accent-dim);
            border: 1px solid rgba(34,211,238, 0.25);
            color: var(--accent);
        }
        .vocab-sugg-approve:hover { background: rgba(34,211,238, 0.18); }
        .vocab-sugg-reject {
            background: transparent;
            border: 1px solid var(--border-hairline);
            color: var(--text-muted);
        }
        .vocab-sugg-reject:hover {
            color: var(--danger);
            border-color: rgba(248, 113, 113, 0.30);
            background: var(--danger-dim);
        }

        /* Cleanup */
        .cleanup-form {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .cleanup-select {
            background: rgba(0, 0, 0, 0.20);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 8px;
            padding: 6px 8px;
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-primary);
            outline: none;
            cursor: pointer;
        }
        .cleanup-select:focus {
            border-color: var(--border-accent);
            box-shadow: var(--shadow-focus-ring);
        }
        .cleanup-btn {
            background: transparent;
            border: 1px solid var(--border-hairline);
            border-radius: 999px;
            padding: 5px 12px;
            font-family: var(--font-ui);
            font-size: 11px;
            font-weight: 500;
            color: var(--text-muted);
            cursor: pointer;
            transition: background 150ms ease-out, color 150ms ease-out,
                        border-color 150ms ease-out, transform 80ms ease;
        }
        .cleanup-btn:hover {
            background: var(--surface-3);
            color: var(--text-primary);
            border-color: var(--border-accent);
        }
        .cleanup-btn:active { transform: scale(0.97); }
        .cleanup-btn.danger:hover {
            color: var(--danger);
            border-color: rgba(248, 113, 113, 0.30);
            background: var(--danger-dim);
        }
        .cleanup-result {
            font-size: 11px;
            color: var(--text-dim);
            margin-top: 6px;
            font-variant-numeric: tabular-nums;
            min-height: 14px;
        }
        .cleanup-result.success { color: var(--success); }
        .cleanup-result.error { color: var(--danger); }

        /* Slightly bigger card textarea font (user feedback: text looked small) */
        .ta { font-size: 13px; line-height: 1.55; }

        /* ── Stats bar ──────────────────────────────────────────────────────── */
        .stats-bar {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
            gap: 10px;
            margin: 0 0 16px 0;
        }
        .stat {
            background: var(--surface-1);
            border: 1px solid var(--border-hairline);
            border-radius: 14px;
            padding: 12px 14px;
            box-shadow: var(--shadow-inset-top);
        }
        .stat-value {
            font-family: var(--font-mono);
            font-size: 18px;
            font-weight: 500;
            color: var(--text-primary);
            font-variant-numeric: tabular-nums;
            line-height: 1.2;
            white-space: nowrap;
        }
        .stat-label {
            font-size: 10px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-top: 3px;
        }

        /* Audio row: play + re-transcribe (Feature 1) */
        .audio-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
        }
        .audio-row audio {
            height: 28px;
            flex: 1;
            min-width: 0;
        }
        .btn-retranscribe {
            font-size: 11px;
            font-weight: 500;
            font-family: var(--font-ui);
            padding: 5px 10px;
            border-radius: 999px;
            cursor: pointer;
            background: var(--accent-dim);
            border: 1px solid rgba(34,211,238,0.25);
            color: var(--accent);
            white-space: nowrap;
            transition: background 150ms ease-out, transform 80ms ease;
        }
        .btn-retranscribe:hover { background: rgba(34,211,238,0.18); }
        .btn-retranscribe:active { transform: scale(0.97); }
        .btn-retranscribe:disabled { opacity: 0.5; cursor: default; }

        /* App chip on cards (where the dictation was pasted) */
        .badge-app {
            font-size: 11px;
            font-weight: 500;
            background: var(--accent-dim);
            color: var(--accent);
            border-radius: 5px;
            padding: 1px 6px;
            cursor: default;
            max-width: 160px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        /* ── Tabs + English view ────────────────────────────────────────────── */
        .tabs { display: flex; gap: 6px; margin: 0 0 20px; border-bottom: 1px solid var(--border-hairline); }
        .tab {
            appearance: none; background: none; border: 0; cursor: pointer;
            min-height: 44px; padding: 0 14px; margin-bottom: -1px;
            font: 500 13px var(--font-ui); color: #a1a1aa;
            border-bottom: 2px solid transparent;
            transition: color 200ms ease, border-color 200ms ease;
        }
        .tab:hover { color: var(--text-primary); }
        .tab[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
        .tab:focus-visible { outline: none; box-shadow: var(--shadow-focus-ring); border-radius: 8px; }
        body.view-english .search-wrap { visibility: hidden; }
        .view[hidden] { display: none; }

        .en-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; margin-bottom: 20px; }
        .en-title { font-size: clamp(18px, 2.4vw, 24px); font-weight: 600; letter-spacing: -0.01em; margin-right: auto; }
        .seg { display: inline-flex; background: var(--surface-1); border: 1px solid var(--border-hairline); border-radius: 12px; padding: 3px; }
        .seg button {
            appearance: none; background: none; border: 0; cursor: pointer;
            min-height: 44px; min-width: 44px; padding: 0 14px; border-radius: 9px;
            font: 500 13px var(--font-ui); color: #a1a1aa;
            transition: background 200ms ease, color 200ms ease;
        }
        .seg button:hover { color: var(--text-primary); }
        .seg button[aria-pressed="true"] { background: var(--accent-dim); color: var(--accent); }
        .seg button:focus-visible { outline: none; box-shadow: var(--shadow-focus-ring); }
        .en-sum { font-size: 13px; color: #a1a1aa; margin: 0 0 24px; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; }
        .en-sum b { color: var(--text-primary); font-variant-numeric: tabular-nums; }
        .chip { font-size: 11px; padding: 2px 8px; border-radius: 6px; background: var(--surface-3); color: #a1a1aa; white-space: nowrap; }

        .en-h { font-size: 12px; font-weight: 500; letter-spacing: 0.06em; text-transform: uppercase; color: #a1a1aa; margin: 0 0 12px; }
        .rep-grid { display: grid; grid-template-columns: 1fr; gap: 12px; margin-bottom: 12px; }
        @media (min-width: 900px) { .rep-grid { grid-template-columns: repeat(3, 1fr); gap: 16px; } }
        .rep-card .card-inner { padding: clamp(16px, 2vw, 24px); height: 100%; display: flex; flex-direction: column; gap: 10px; }
        .rep-count { align-self: flex-start; font: 500 12px var(--font-mono); color: var(--accent); background: var(--accent-dim); border-radius: 6px; padding: 2px 8px; }
        .rep-fix { font-size: clamp(20px, 2.2vw, 26px); font-weight: 600; line-height: 1.25; overflow-wrap: anywhere; }
        .rep-fix .was { color: #a1a1aa; font-weight: 400; }
        .rep-fix .arrow { color: var(--text-dim); margin: 0 6px; }
        .rep-fix .now { color: var(--accent); }
        .rep-why { font-size: 14px; color: #d4d4d8; overflow-wrap: anywhere; }
        .rep-also { font-size: 12px; color: #a1a1aa; margin-top: auto; overflow-wrap: anywhere; }
        .rep-more { list-style: none; display: grid; gap: 8px; margin-bottom: 8px; }
        .rep-more li { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; font-size: 13px; padding: 10px 14px; background: var(--surface-1); border: 1px solid var(--border-hairline); border-radius: 12px; }
        .rep-more .now { color: var(--accent); font-weight: 600; }
        .rep-more .n { font-family: var(--font-mono); font-size: 12px; color: #a1a1aa; margin-left: auto; }

        .en-list { list-style: none; display: grid; gap: 8px; margin-top: 8px; }
        .en-row { display: grid; gap: 4px; padding: 12px 16px; background: var(--surface-1); border: 1px solid var(--border-hairline); border-radius: 14px; }
        .en-row .line { font-size: 15px; overflow-wrap: anywhere; }
        .en-row .was { color: #a1a1aa; text-decoration: line-through; text-decoration-color: rgba(161,161,170,0.6); }
        .en-row .arrow { color: var(--text-dim); margin: 0 6px; }
        .en-row .now { color: var(--accent); font-weight: 600; }
        .en-row .why { font-size: 13px; color: #d4d4d8; overflow-wrap: anywhere; }
        .en-row .meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font: 12px var(--font-mono); color: #a1a1aa; }
        .en-note { font-size: 14px; color: #a1a1aa; padding: 16px; border: 1px dashed var(--border-accent); border-radius: 14px; margin-bottom: 24px; }
        .en-empty { text-align: center; padding: clamp(48px, 10vw, 96px) 16px; }
        .en-empty p { font-size: clamp(15px, 2vw, 18px); color: #d4d4d8; max-width: 34ch; margin: 0 auto; }
        .en-empty kbd { font: 500 0.9em var(--font-mono); color: var(--accent); background: var(--accent-dim); padding: 2px 8px; border-radius: 6px; white-space: nowrap; }
        .en-empty .en-empty-title { color: var(--text-primary); font-weight: 600; margin-bottom: 8px; }
        .en-err { color: var(--danger); font-size: 13px; margin-bottom: 16px; }
    </style>
</head>
<body>
<div class="container">

    <!-- Header -->
    <header class="header">
        <img src="/brand/fluentpill-lockup-dark.svg" alt="fluentpill" class="logo">
        <span class="header-sub" id="header-sub">historial</span>
        <span class="count-badge" id="count-badge"></span>
        <div class="search-wrap">
            <!-- Magnifying glass icon -->
            <svg class="search-icon" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <circle cx="6" cy="6" r="4.5" stroke="currentColor" stroke-width="1.4"/>
                <line x1="9.5" y1="9.5" x2="12.5" y2="12.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
            </svg>
            <input
                type="text"
                id="search"
                class="search-input"
                placeholder="buscar…"
                autocomplete="off"
                spellcheck="false"
                aria-label="Buscar transcripciones"
            >
        </div>
    </header>

    <!-- Tabs -->
    <nav class="tabs" role="tablist" aria-label="secciones">
        <button class="tab" id="tab-history" role="tab" aria-selected="true" aria-controls="view-history" type="button">historial</button>
        <button class="tab" id="tab-english" role="tab" aria-selected="false" aria-controls="view-english" type="button">english</button>
    </nav>

    <!-- English: repeated mistakes -->
    <section class="view" id="view-english" role="tabpanel" aria-labelledby="tab-english" hidden>
        <div class="en-bar">
            <h2 class="en-title">Tus errores repetidos</h2>
            <div class="seg" role="group" aria-label="Periodo">
                <button type="button" data-days="7" aria-pressed="true">7 días</button>
                <button type="button" data-days="30" aria-pressed="false">30 días</button>
                <button type="button" data-days="all" aria-pressed="false">todo</button>
            </div>
        </div>
        <div id="en-body" aria-live="polite"></div>
    </section>

    <div class="view" id="view-history" role="tabpanel" aria-labelledby="tab-history">

    <!-- Stats bar -->
    <section class="stats-bar" id="stats-bar" aria-label="estadísticas">
        <div class="stat"><div class="stat-value" id="stat-count">–</div><div class="stat-label">dictados</div></div>
        <div class="stat"><div class="stat-value" id="stat-words">–</div><div class="stat-label">palabras</div></div>
        <div class="stat"><div class="stat-value" id="stat-audio">–</div><div class="stat-label">audio total</div></div>
        <div class="stat"><div class="stat-value" id="stat-wpm">–</div><div class="stat-label">wpm medio</div></div>
        <div class="stat"><div class="stat-value" id="stat-saved">–</div><div class="stat-label">ahorrado vs teclear</div></div>
        <div class="stat"><div class="stat-value" id="stat-today">–</div><div class="stat-label">hoy</div></div>
    </section>

    <!-- Tools bar: dictionary + cleanup -->
    <section class="tools-bar" aria-label="herramientas">
        <div class="tools-section">
            <div class="tools-label">
                diccionario aprendido
                <span class="count" id="dict-count"></span>
            </div>
            <div id="dict-chips" class="dict-chips" aria-live="polite"></div>
            <div class="dict-add-row">
                <input
                    type="text"
                    id="dict-input"
                    class="dict-add-input"
                    placeholder="añadir palabra (ej: supabase, example.com)"
                    maxlength="80"
                    autocomplete="off"
                    spellcheck="false"
                    aria-label="Añadir palabra al diccionario"
                >
                <button id="dict-add-btn" class="dict-add-btn" type="button" disabled>
                    añadir
                </button>
            </div>
        </div>
        <div class="tools-section">
            <div class="tools-label">limpieza historial</div>
            <div class="cleanup-form">
                <span style="font-size:12px;color:var(--text-muted);">borrar &gt;</span>
                <select id="cleanup-select" class="cleanup-select" aria-label="Días de antigüedad">
                    <option value="1">1 día</option>
                    <option value="3" selected>3 días</option>
                    <option value="7">7 días</option>
                    <option value="14">14 días</option>
                    <option value="30">30 días</option>
                </select>
                <button id="cleanup-btn" class="cleanup-btn danger" type="button">
                    borrar
                </button>
            </div>
            <div id="cleanup-result" class="cleanup-result"></div>
        </div>
    </section>

    <!-- Vocabulary suggestions: passive candidates from the raw-vs-cleaned
         diff (Dictionary.observe()). Hidden entirely while empty. -->
    <section class="vocab-bar" id="vocab-suggestions" aria-label="sugerencias de vocabulario" style="display:none;">
        <div class="tools-label">
            sugerencias de vocabulario
            <span class="count" id="vocab-sugg-count"></span>
        </div>
        <div id="vocab-sugg-list" class="vocab-sugg-list" aria-live="polite"></div>
    </section>

    <!-- Connection error banner -->
    <div id="error-banner" class="error-banner" role="alert">
        sin conexión con el servidor. los cambios no se guardarán.
    </div>

    <!-- No results -->
    <div id="no-results" class="no-results" aria-live="polite"></div>

    <!-- Cards -->
    <div id="cards-list" role="list"></div>

    <!-- Empty state -->
    <div id="empty-state" class="empty-state" aria-live="polite">
        <div class="empty-icon">
            <svg width="24" height="20" viewBox="0 0 24 20" fill="none" aria-hidden="true">
                <!-- 3 bars: heights 8/14/10, vertically aligned to bottom -->
                <rect class="empty-bar" x="1"  y="12" width="5" height="8"  rx="1.5" fill="#3f3f46"/>
                <rect class="empty-bar" x="9.5" y="6"  width="5" height="14" rx="1.5" fill="#3f3f46"/>
                <rect class="empty-bar" x="18" y="10" width="5" height="10" rx="1.5" fill="#3f3f46"/>
            </svg>
        </div>
        <p class="empty-title">nada grabado aún</p>
        <p class="empty-hint">mantén Ctrl+Shift para grabar</p>
    </div>

    </div><!-- /view-history -->

    <!-- Footer -->
    <footer class="footer">
        fluentpill<span class="footer-sep">·</span>grabado localmente<span class="footer-sep">·</span>{{ asr_label }}
    </footer>

</div>

<script>
(function () {
    'use strict';

    let allData = [];
    let lastMaxId = 0;
    let searchQuery = '';
    let debounceTimer = null;
    let isInitialLoad = true;

    // ── Relative time ──────────────────────────────────────────────────────────
    function relTime(iso) {
        const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
        const diff = Math.floor((Date.now() - d) / 1000);
        if (diff < 60) return 'hace ' + diff + 's';
        if (diff < 3600) return 'hace ' + Math.floor(diff / 60) + ' min';
        if (diff < 86400) return 'hoy ' + d.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' });
        if (diff < 172800) return 'ayer ' + d.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' });
        return d.toLocaleDateString('es-MX', { day: 'numeric', month: 'short' })
             + ' ' + d.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' });
    }

    // ── Auto-resize textarea ───────────────────────────────────────────────────
    function autoResize(ta) {
        ta.style.height = 'auto';
        ta.style.height = Math.min(ta.scrollHeight, 300) + 'px';
    }

    // ── Spinner inline (12px) ──────────────────────────────────────────────────
    function makeSpinner() {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', '12');
        svg.setAttribute('height', '12');
        svg.setAttribute('viewBox', '0 0 12 12');
        svg.style.cssText = 'display:inline-block;vertical-align:middle;margin-right:6px;animation:spin 0.7s linear infinite';
        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', '6');
        circle.setAttribute('cy', '6');
        circle.setAttribute('r', '4.5');
        circle.setAttribute('stroke', 'currentColor');
        circle.setAttribute('stroke-width', '1.5');
        circle.setAttribute('fill', 'none');
        circle.setAttribute('stroke-dasharray', '14 8');
        svg.appendChild(circle);
        // keyframes via style tag (inserted once)
        if (!document.getElementById('spin-keyframes')) {
            const st = document.createElement('style');
            st.id = 'spin-keyframes';
            st.textContent = '@keyframes spin { to { transform: rotate(360deg); } }';
            document.head.appendChild(st);
        }
        return svg;
    }

    // ── Build one skeleton card ────────────────────────────────────────────────
    function buildSkeleton() {
        const outer = document.createElement('div');
        outer.className = 'card-outer';
        const inner = document.createElement('div');
        inner.className = 'card-inner';
        inner.innerHTML =
            '<div class="skeleton-block skeleton-meta"></div>' +
            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">' +
                '<div class="skeleton-block skeleton-body"></div>' +
                '<div class="skeleton-block skeleton-body-short"></div>' +
            '</div>';
        outer.appendChild(inner);
        return outer;
    }

    // ── Build one card DOM node ────────────────────────────────────────────────
    function buildCard(t, isNew) {
        const rawVal   = (t.raw_text && t.raw_text.trim()) ? t.raw_text : t.text;
        const finalVal = t.text || '';
        const cleaned  = t.was_cleaned;

        // Outer bezel
        const outer = document.createElement('div');
        outer.className = 'card-outer' + (isNew ? ' card-new' : '');
        outer.setAttribute('role', 'listitem');
        outer.dataset.id = t.id;

        // Inner core
        const inner = document.createElement('div');
        inner.className = 'card-inner';

        // Meta row
        const meta = document.createElement('div');
        meta.className = 'card-meta';

        const timeEl = document.createElement('span');
        timeEl.className = 'meta-time';
        timeEl.textContent = relTime(t.created_at);
        meta.appendChild(timeEl);

        if (t.duration_seconds) {
            const sep = document.createElement('span');
            sep.className = 'meta-sep';
            sep.textContent = '·';
            meta.appendChild(sep);

            const dur = document.createElement('span');
            dur.className = 'meta-dur';
            dur.textContent = t.duration_seconds.toFixed(1) + 's';
            meta.appendChild(dur);
        }

        if (cleaned) {
            const badge = document.createElement('span');
            badge.className = 'badge-ai';
            badge.textContent = 'IA';
            badge.title = 'limpiado por IA';
            meta.appendChild(badge);
        }

        if (t.app_name) {
            const appChip = document.createElement('span');
            appChip.className = 'badge-app';
            appChip.textContent = t.app_name;
            appChip.title = 'dictado hacia ' + t.app_name +
                (t.app_category ? ' (' + t.app_category + ')' : '');
            meta.appendChild(appChip);
        }

        inner.appendChild(meta);

        // Two-column grid
        const cols = document.createElement('div');
        cols.className = 'card-cols';

        function makeCol(label, value, fieldName) {
            const col = document.createElement('div');

            const lbl = document.createElement('div');
            lbl.className = 'col-label';
            lbl.textContent = label;

            const ta = document.createElement('textarea');
            ta.className = 'ta';
            ta.value = value;
            ta.dataset.field = fieldName;
            ta.dataset.original = value;
            ta.spellcheck = false;
            ta.setAttribute('aria-label', label);

            ta.addEventListener('input', function () {
                autoResize(this);
                const dirty = this.value !== this.dataset.original;
                this.classList.toggle('dirty', dirty);
                updateSaveBtn(inner);
            });

            const actions = document.createElement('div');
            actions.className = 'col-actions';

            const btnCopy = document.createElement('button');
            btnCopy.className = 'btn-copy';
            btnCopy.textContent = 'copiar';
            btnCopy.setAttribute('aria-label', 'Copiar ' + label);

            btnCopy.addEventListener('click', function () {
                navigator.clipboard.writeText(ta.value).then(() => {
                    btnCopy.classList.remove('copied-reset');
                    btnCopy.textContent = 'copiado ✓';
                    btnCopy.classList.add('copied');
                    setTimeout(() => {
                        btnCopy.classList.add('copied-reset');
                        btnCopy.classList.remove('copied');
                        btnCopy.textContent = 'copiar';
                        setTimeout(() => btnCopy.classList.remove('copied-reset'), 200);
                    }, 1600);
                });
            });

            actions.appendChild(btnCopy);
            col.appendChild(lbl);
            col.appendChild(ta);
            col.appendChild(actions);
            return col;
        }

        const rawTa = makeCol('WHISPER RAW', rawVal, 'raw_text');
        const finalTa = makeCol('FINAL', finalVal, 'text');
        cols.appendChild(rawTa);
        cols.appendChild(finalTa);
        inner.appendChild(cols);

        // Audio row: play + re-transcribe (only when the WAV is still on disk —
        // retention purges files after RECORDINGS_RETENTION_HOURS)
        if (t.has_audio) {
            const audioRow = document.createElement('div');
            audioRow.className = 'audio-row';

            const audioEl = document.createElement('audio');
            audioEl.controls = true;
            audioEl.preload = 'none';
            audioEl.src = '/api/audio/' + t.id;
            audioEl.addEventListener('error', () => { audioRow.style.display = 'none'; });

            const btnRe = document.createElement('button');
            btnRe.type = 'button';
            btnRe.className = 'btn-retranscribe';
            btnRe.textContent = 're-transcribir';
            btnRe.addEventListener('click', () => retranscribeCard(t.id, btnRe, rawTa.querySelector('.ta'), finalTa.querySelector('.ta')));

            audioRow.appendChild(audioEl);
            audioRow.appendChild(btnRe);
            inner.appendChild(audioRow);
        }

        // Save button wrap
        const saveWrap = document.createElement('div');
        saveWrap.className = 'card-save-wrap';

        const btnSave = document.createElement('button');
        btnSave.className = 'btn-save';
        btnSave.textContent = 'guardar cambios';
        btnSave.addEventListener('click', () => saveCard(inner, t.id, btnSave));
        saveWrap.appendChild(btnSave);

        // Save error message
        const saveErr = document.createElement('div');
        saveErr.className = 'save-error';
        saveErr.textContent = 'error al guardar, intenta de nuevo';
        saveWrap.appendChild(saveErr);

        inner.appendChild(saveWrap);
        outer.appendChild(inner);

        // Initial height
        setTimeout(() => {
            outer.querySelectorAll('.ta').forEach(ta => autoResize(ta));
        }, 0);

        return outer;
    }

    function updateSaveBtn(inner) {
        const anyDirty = Array.from(inner.querySelectorAll('.ta')).some(ta => ta.value !== ta.dataset.original);
        const btn = inner.querySelector('.btn-save');
        if (anyDirty) {
            btn.classList.add('visible');
            btn.classList.remove('saving', 'saved', 'fading');
        } else {
            btn.classList.remove('visible');
        }
    }

    async function saveCard(inner, id, btn) {
        const payload = {};
        inner.querySelectorAll('.ta').forEach(ta => {
            if (ta.value !== ta.dataset.original) {
                payload[ta.dataset.field] = ta.value;
            }
        });
        if (!Object.keys(payload).length) return;

        // Saving state
        btn.classList.add('saving');
        btn.innerHTML = '';
        btn.appendChild(makeSpinner());
        btn.appendChild(document.createTextNode('guardando…'));

        const saveErr = inner.querySelector('.save-error');
        saveErr.classList.remove('visible');

        try {
            const res = await fetch('/api/transcriptions/' + id, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });

            if (res.ok) {
                // Update originals
                inner.querySelectorAll('.ta').forEach(ta => {
                    ta.dataset.original = ta.value;
                    ta.classList.remove('dirty');
                });
                btn.classList.remove('saving');
                btn.classList.add('saved');
                btn.innerHTML = 'guardado';

                // Fade out after 1800ms
                setTimeout(() => {
                    btn.classList.add('fading');
                    setTimeout(() => {
                        btn.classList.remove('saved', 'fading', 'visible');
                        btn.innerHTML = 'guardar cambios';
                    }, 250);
                }, 1800);
            } else {
                throw new Error('HTTP ' + res.status);
            }
        } catch (e) {
            btn.classList.remove('saving');
            btn.classList.add('visible');
            btn.innerHTML = 'guardar cambios';
            // Show inline error
            saveErr.classList.add('visible');
            setTimeout(() => saveErr.classList.remove('visible'), 3000);
        }
    }

    // ── Re-transcribe from the saved WAV ───────────────────────────────────────
    async function retranscribeCard(id, btn, rawTa, finalTa) {
        const original = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'transcribiendo…';
        try {
            const res = await fetch('/api/transcriptions/' + id + '/retranscribe', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
            rawTa.value = data.raw_text;
            rawTa.dataset.original = data.raw_text;
            rawTa.classList.remove('dirty');
            finalTa.value = data.text;
            finalTa.dataset.original = data.text;
            finalTa.classList.remove('dirty');
            autoResize(rawTa);
            autoResize(finalTa);
            btn.textContent = 'hecho ✓';
            setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
        } catch (e) {
            btn.textContent = 'error';
            setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2000);
        }
    }

    // ── Render skeletons ───────────────────────────────────────────────────────
    function showSkeletons() {
        const list = document.getElementById('cards-list');
        list.innerHTML = '';
        for (let i = 0; i < 3; i++) {
            list.appendChild(buildSkeleton());
        }
    }

    // ── Update count badge ─────────────────────────────────────────────────────
    function updateBadge(n) {
        const badge = document.getElementById('count-badge');
        badge.textContent = n > 0 ? '[' + n + ']' : '';
    }

    // ── Connection error banner ────────────────────────────────────────────────
    function showErrorBanner() {
        const banner = document.getElementById('error-banner');
        banner.style.display = 'block';
        requestAnimationFrame(() => banner.classList.add('visible'));
    }

    // ── Render list ────────────────────────────────────────────────────────────
    function renderAll(data) {
        const list  = document.getElementById('cards-list');
        const empty = document.getElementById('empty-state');
        const noRes = document.getElementById('no-results');

        updateBadge(allData.length);
        noRes.classList.remove('visible');

        if (data.length === 0) {
            list.innerHTML = '';
            if (searchQuery) {
                // No results for query
                noRes.textContent = 'sin resultados para «' + searchQuery + '»';
                noRes.classList.add('visible');
                empty.classList.remove('visible');
            } else {
                empty.classList.add('visible');
            }
            return;
        }

        empty.classList.remove('visible');
        list.innerHTML = '';
        data.forEach(t => list.appendChild(buildCard(t, false)));
    }

    // ── Filter client-side (opacity dim for non-matching) ─────────────────────
    function applyFilter(q) {
        if (!q) {
            // Show all — remove dim
            document.querySelectorAll('.card-outer').forEach(c => c.classList.remove('filtered-out'));
            document.getElementById('no-results').classList.remove('visible');
            document.getElementById('empty-state').classList.toggle('visible', allData.length === 0);
            return;
        }
        const lc = q.toLowerCase();
        const list = document.getElementById('cards-list');
        const noRes = document.getElementById('no-results');

        if (!list.children.length || document.querySelector('.skeleton-block')) {
            // Skeleton still showing — re-render with filter
            const filtered = allData.filter(t =>
                (t.text || '').toLowerCase().includes(lc) ||
                (t.raw_text || '').toLowerCase().includes(lc)
            );
            renderAll(filtered);
            return;
        }

        let visibleCount = 0;
        Array.from(list.children).forEach(card => {
            const id = parseInt(card.dataset.id, 10);
            const t = allData.find(x => x.id === id);
            if (!t) { card.classList.add('filtered-out'); return; }
            const matches =
                (t.text || '').toLowerCase().includes(lc) ||
                (t.raw_text || '').toLowerCase().includes(lc);
            card.classList.toggle('filtered-out', !matches);
            if (matches) visibleCount++;
        });

        noRes.classList.toggle('visible', visibleCount === 0);
        if (visibleCount === 0) {
            noRes.textContent = 'sin resultados para «' + q + '»';
        }
    }

    // ── Load / auto-refresh ────────────────────────────────────────────────────
    async function loadData(initial) {
        const q = searchQuery;
        const url = '/api/transcriptions?limit=50' + (q ? '&q=' + encodeURIComponent(q) : '');
        try {
            const res = await fetch(url);
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();

            if (initial) {
                allData = data;
                lastMaxId = data.length ? data[0].id : 0;
                renderAll(data);
                isInitialLoad = false;
                return;
            }

            // Detect new entries
            const newItems = data.filter(t => t.id > lastMaxId);
            if (newItems.length) {
                lastMaxId = newItems[0].id;
                allData = data;
                updateBadge(allData.length);

                if (!q) {
                    const list = document.getElementById('cards-list');
                    newItems.reverse().forEach(t => {
                        const card = buildCard(t, true);
                        list.insertBefore(card, list.firstChild);
                        // Trim excess
                        while (list.children.length > 50) list.removeChild(list.lastChild);

                        // Ephemeral accent border — fade out at 2000ms
                        setTimeout(() => {
                            card.classList.add('card-fresh');
                            setTimeout(() => {
                                card.style.transition = 'border-color 600ms ease';
                                card.classList.remove('card-fresh');
                                setTimeout(() => card.style.transition = '', 600);
                            }, 2000);
                        }, 10);
                    });
                } else {
                    applyFilter(q);
                }
            }
        } catch (e) {
            console.warn('Fetch error:', e);
            if (initial) {
                // Clear skeletons on error
                document.getElementById('cards-list').innerHTML = '';
                showErrorBanner();
                isInitialLoad = false;
            }
        }
    }

    // ── Search with debounce ───────────────────────────────────────────────────
    document.getElementById('search').addEventListener('input', function () {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            searchQuery = this.value.trim();
            applyFilter(searchQuery);
        }, 200);
    });

    // ── Dictionary panel ──────────────────────────────────────────────────────
    const dictChips = document.getElementById('dict-chips');
    const dictCount = document.getElementById('dict-count');
    const dictInput = document.getElementById('dict-input');
    const dictAddBtn = document.getElementById('dict-add-btn');

    function renderDictionary(words) {
        dictCount.textContent = words.length === 0
            ? 'vacío'
            : '· ' + words.length + (words.length === 1 ? ' palabra' : ' palabras');
        dictChips.innerHTML = '';
        if (words.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'dict-empty';
            empty.textContent = 'aún no has corregido nada. cuando edites una transcripción, las palabras nuevas aparecen aquí.';
            dictChips.appendChild(empty);
            return;
        }
        words.forEach(word => {
            const chip = document.createElement('span');
            chip.className = 'dict-chip';
            const label = document.createElement('span');
            label.textContent = word;
            const x = document.createElement('button');
            x.type = 'button';
            x.className = 'dict-chip-x';
            x.textContent = '×';
            x.setAttribute('aria-label', 'borrar ' + word);
            x.title = 'borrar';
            x.addEventListener('click', () => deleteDictionaryWord(word, chip));
            chip.appendChild(label);
            chip.appendChild(x);
            dictChips.appendChild(chip);
        });
    }

    async function loadDictionary() {
        try {
            const res = await fetch('/api/vocabulary');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const words = await res.json();
            renderDictionary(Array.isArray(words) ? words : []);
        } catch (e) {
            console.warn('loadDictionary failed:', e);
        }
    }

    async function deleteDictionaryWord(word, chipEl) {
        try {
            const res = await fetch('/api/vocabulary/' + encodeURIComponent(word), {
                method: 'DELETE',
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            if (chipEl) {
                chipEl.style.opacity = '0';
                chipEl.style.transform = 'scale(0.92)';
                chipEl.style.transition = 'opacity 200ms ease, transform 200ms ease';
                setTimeout(loadDictionary, 220);
            } else {
                loadDictionary();
            }
        } catch (e) {
            console.warn('deleteDictionaryWord failed:', e);
        }
    }

    async function addDictionaryWord(word) {
        const trimmed = (word || '').trim();
        if (trimmed.length < 2) return;
        try {
            dictAddBtn.disabled = true;
            const res = await fetch('/api/vocabulary', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ word: trimmed }),
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            dictInput.value = '';
            await loadDictionary();
        } catch (e) {
            console.warn('addDictionaryWord failed:', e);
        } finally {
            dictAddBtn.disabled = dictInput.value.trim().length < 2;
        }
    }

    dictInput.addEventListener('input', function () {
        dictAddBtn.disabled = this.value.trim().length < 2;
    });
    dictInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !dictAddBtn.disabled) {
            e.preventDefault();
            addDictionaryWord(this.value);
        }
    });
    dictAddBtn.addEventListener('click', () => addDictionaryWord(dictInput.value));

    // ── Vocabulary suggestions (passive auto-learn candidates) ─────────────────
    const vocabSuggSection = document.getElementById('vocab-suggestions');
    const vocabSuggList = document.getElementById('vocab-sugg-list');
    const vocabSuggCount = document.getElementById('vocab-sugg-count');

    function renderVocabCandidates(candidates) {
        if (!candidates || candidates.length === 0) {
            vocabSuggSection.style.display = 'none';
            vocabSuggList.innerHTML = '';
            return;
        }
        vocabSuggSection.style.display = '';
        vocabSuggCount.textContent = '· ' + candidates.length +
            (candidates.length === 1 ? ' candidata' : ' candidatas');
        vocabSuggList.innerHTML = '';
        candidates.forEach(c => {
            const item = document.createElement('div');
            item.className = 'vocab-sugg-item';

            const word = document.createElement('span');
            word.className = 'vocab-sugg-word';
            word.textContent = c.word;

            const seen = document.createElement('span');
            seen.className = 'vocab-sugg-seen';
            seen.textContent = 'visto ' + c.times_seen + 'x';

            const sample = document.createElement('span');
            sample.className = 'vocab-sugg-sample';
            sample.textContent = c.sample_clean || c.sample_raw || '';
            sample.title = c.sample_clean || c.sample_raw || '';

            const actions = document.createElement('div');
            actions.className = 'vocab-sugg-actions';

            const approveBtn = document.createElement('button');
            approveBtn.type = 'button';
            approveBtn.className = 'vocab-sugg-btn vocab-sugg-approve';
            approveBtn.textContent = 'aprobar';
            approveBtn.addEventListener('click', () => resolveVocabCandidate(c.word, 'approve', item));

            const rejectBtn = document.createElement('button');
            rejectBtn.type = 'button';
            rejectBtn.className = 'vocab-sugg-btn vocab-sugg-reject';
            rejectBtn.textContent = 'descartar';
            rejectBtn.addEventListener('click', () => resolveVocabCandidate(c.word, 'reject', item));

            actions.appendChild(approveBtn);
            actions.appendChild(rejectBtn);

            item.appendChild(word);
            item.appendChild(seen);
            item.appendChild(sample);
            item.appendChild(actions);
            vocabSuggList.appendChild(item);
        });
    }

    async function loadVocabCandidates() {
        try {
            const res = await fetch('/api/vocabulary/candidates');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const candidates = await res.json();
            renderVocabCandidates(Array.isArray(candidates) ? candidates : []);
        } catch (e) {
            console.warn('loadVocabCandidates failed:', e);
        }
    }

    async function resolveVocabCandidate(word, action, itemEl) {
        try {
            const res = await fetch('/api/vocabulary/candidates/' + encodeURIComponent(word) + '/' + action, {
                method: 'POST',
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            if (itemEl) {
                itemEl.style.opacity = '0';
                itemEl.style.transition = 'opacity 200ms ease';
                setTimeout(() => { loadVocabCandidates(); if (action === 'approve') loadDictionary(); }, 220);
            } else {
                loadVocabCandidates();
                if (action === 'approve') loadDictionary();
            }
        } catch (e) {
            console.warn('resolveVocabCandidate failed:', e);
        }
    }

    // ── History cleanup ───────────────────────────────────────────────────────
    const cleanupBtn = document.getElementById('cleanup-btn');
    const cleanupSelect = document.getElementById('cleanup-select');
    const cleanupResult = document.getElementById('cleanup-result');

    cleanupBtn.addEventListener('click', async function () {
        const days = parseInt(cleanupSelect.value, 10);
        if (!days || days < 1) return;
        const confirmMsg = 'borrar todas las transcripciones de más de ' + days + ' días? esto no se puede deshacer.';
        if (!confirm(confirmMsg)) return;

        cleanupResult.className = 'cleanup-result';
        cleanupResult.textContent = 'borrando…';

        try {
            const res = await fetch('/api/transcriptions/older-than/' + days, {
                method: 'DELETE',
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            const n = data.removed || 0;
            cleanupResult.className = 'cleanup-result success';
            cleanupResult.textContent = n === 0
                ? 'nada que borrar más antiguo de ' + days + ' días'
                : 'borrados ' + n + (n === 1 ? ' registro' : ' registros');
            // Reload the cards list so deleted entries disappear
            loadData(true);
            setTimeout(() => {
                cleanupResult.className = 'cleanup-result';
                cleanupResult.textContent = '';
            }, 4000);
        } catch (e) {
            cleanupResult.className = 'cleanup-result error';
            cleanupResult.textContent = 'error al borrar';
            setTimeout(() => {
                cleanupResult.className = 'cleanup-result';
                cleanupResult.textContent = '';
            }, 4000);
        }
    });

    // ── Stats bar ──────────────────────────────────────────────────────────────
    function fmtCount(n) {
        return n >= 10000 ? (n / 1000).toFixed(1).replace('.0', '') + 'k' : String(n);
    }
    function fmtMinutes(m) {
        if (m >= 60) return Math.floor(m / 60) + 'h ' + Math.round(m % 60) + 'm';
        return Math.round(m) + ' min';
    }
    async function loadStats() {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) return;
            const s = await res.json();
            document.getElementById('stat-count').textContent = fmtCount(s.total_dictations);
            document.getElementById('stat-words').textContent = fmtCount(s.total_words);
            document.getElementById('stat-audio').textContent = fmtMinutes(s.total_audio_minutes);
            document.getElementById('stat-wpm').textContent = Math.round(s.avg_wpm);
            document.getElementById('stat-saved').textContent = fmtMinutes(s.saved_minutes);
            document.getElementById('stat-today').textContent =
                s.today_dictations + ' · ' + fmtCount(s.today_words) + ' pal.';
        } catch (e) {
            console.warn('loadStats failed:', e);
        }
    }


    // ── English tab: repeated mistakes ─────────────────────────────────────────
    const EN_PERIOD = { '7': 'esta semana', '30': 'este mes', 'all': 'desde el principio' };
    let enDays = '7';
    let enReq = 0;

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined) n.textContent = text;
        return n;
    }
    function times(n) { return n + (n === 1 ? ' vez' : ' veces'); }

    function renderEnglish(s) {
        const body = document.getElementById('en-body');
        body.replaceChildren();

        if (!s.dictations && !s.corrections) {
            const box = el('div', 'en-empty');
            box.appendChild(el('p', 'en-empty-title', 'Aún no hay dictados en inglés.'));
            const p = el('p');
            p.append('Mantén ', el('kbd', '', 'Ctrl+Alt+Espacio'), ' y habla en inglés.');
            box.appendChild(p);
            body.appendChild(box);
            return;
        }

        const sum = el('div', 'en-sum');
        const a = el('span'); a.append(el('b', '', String(s.dictations)), ' dictados en inglés');
        const b = el('span'); b.append(el('b', '', String(s.corrections)), ' correcciones');
        sum.append(a, b);
        Object.entries(s.by_type).sort((x, y) => y[1] - x[1])
            .forEach(([t, n]) => sum.appendChild(el('span', 'chip', t + ' · ' + n)));
        body.appendChild(sum);

        body.appendChild(el('h3', 'en-h', 'Tus 3 errores más repetidos ' + EN_PERIOD[enDays]));
        if (!s.repeated.length) {
            body.appendChild(el('p', 'en-note', 'Todavía no se repite ningún error en este periodo. Cuando uno salga dos veces, aparecerá aquí.'));
        } else {
            const grid = el('div', 'rep-grid');
            s.repeated.slice(0, 3).forEach(r => {
                const outer = el('div', 'card-outer rep-card');
                const inner = el('div', 'card-inner');
                inner.appendChild(el('span', 'rep-count', times(r.count)));
                const fix = el('div', 'rep-fix');
                fix.append(el('span', 'was', r.examples[0] || ''), el('span', 'arrow', '→'), el('span', 'now', r.better));
                inner.appendChild(fix);
                if (r.why) inner.appendChild(el('div', 'rep-why', r.why));
                if (r.examples[1]) inner.appendChild(el('div', 'rep-also', 'También dijiste: ' + r.examples[1]));
                outer.appendChild(inner);
                grid.appendChild(outer);
            });
            body.appendChild(grid);
            if (s.repeated.length > 3) {
                const ul = el('ul', 'rep-more');
                s.repeated.slice(3).forEach(r => {
                    const li = el('li');
                    li.append(el('span', '', r.examples[0] || ''), el('span', '', '→'), el('span', 'now', r.better), el('span', 'n', times(r.count)));
                    ul.appendChild(li);
                });
                body.appendChild(ul);
            }
        }

        const h = el('h3', 'en-h', 'Últimas correcciones');
        h.style.marginTop = '28px';
        body.appendChild(h);
        if (!s.recent.length) {
            body.appendChild(el('p', 'en-note', 'No hay correcciones en este periodo.'));
            return;
        }
        const list = el('ul', 'en-list');
        s.recent.forEach(c => {
            const li = el('li', 'en-row');
            const line = el('div', 'line');
            line.append(el('span', 'was', c.original), el('span', 'arrow', '→'), el('span', 'now', c.better));
            li.appendChild(line);
            if (c.why) li.appendChild(el('div', 'why', c.why));
            const meta = el('div', 'meta');
            if (c.type) meta.appendChild(el('span', 'chip', c.type));
            meta.appendChild(el('span', '', c.created_at ? relTime(c.created_at) : ''));
            li.appendChild(meta);
            list.appendChild(li);
        });
        body.appendChild(list);
    }

    async function loadEnglish() {
        const my = ++enReq;
        try {
            const res = await fetch('/api/english/stats?days=' + enDays);
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const s = await res.json();
            if (my !== enReq) return;  // a newer period was picked meanwhile
            renderEnglish(s);
        } catch (e) {
            if (my !== enReq) return;
            document.getElementById('en-body').replaceChildren(
                el('p', 'en-err', 'sin conexión con el servidor. no se pudieron cargar los errores.'));
        }
    }

    function showView(name) {
        const en = name === 'english';
        document.getElementById('view-english').hidden = !en;
        document.getElementById('view-history').hidden = en;
        document.body.classList.toggle('view-english', en);
        document.getElementById('header-sub').textContent = en ? 'english' : 'historial';
        document.getElementById('tab-english').setAttribute('aria-selected', String(en));
        document.getElementById('tab-history').setAttribute('aria-selected', String(!en));
        if (en) loadEnglish();
    }
    document.getElementById('tab-history').addEventListener('click', () => { history.replaceState(null, '', '#'); showView('history'); });
    document.getElementById('tab-english').addEventListener('click', () => { history.replaceState(null, '', '#english'); showView('english'); });
    document.querySelectorAll('.seg button').forEach(btn => btn.addEventListener('click', () => {
        enDays = btn.dataset.days;
        document.querySelectorAll('.seg button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
        loadEnglish();
    }));
    setInterval(() => { if (!document.getElementById('view-english').hidden) loadEnglish(); }, 30000);
    if (location.hash === '#english') showView('english');

    // ── Boot ───────────────────────────────────────────────────────────────────
    showSkeletons();
    loadData(true);
    loadDictionary();
    loadVocabCandidates();
    loadStats();
    setInterval(() => loadData(false), 5000);
    setInterval(loadDictionary, 30000);
    setInterval(loadVocabCandidates, 30000);
    setInterval(loadStats, 30000);
})();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    # Same rule as core/transcriber.py: OpenAI leads only when configured and keyed.
    provider = "openai" if ASR_PRIMARY == "openai" and OPENAI_API_KEY else "groq"
    return render_template_string(HTML_TEMPLATE, asr_label=f"{provider} whisper")


@app.route("/api/stats")
def get_stats():
    """Productivity stats for the dashboard header."""
    db = TranscriptionDB()
    return jsonify(db.stats())


@app.route("/api/english/stats")
def get_english_stats():
    """'Your repeated mistakes' data for the English tab. days=7|30|all."""
    raw = request.args.get("days", "7")
    if raw == "all":
        days = None
    elif raw in ("7", "30"):
        days = int(raw)
    else:
        return jsonify({"error": "days must be 7, 30 or all"}), 400
    db = TranscriptionDB(app.config.get("DB_PATH") or DB_PATH)
    return jsonify(db.english_stats(days))


def _audio_file_for(row: dict):
    """Return the Path to a row's WAV if audio_path is set and the file still
    exists on disk (retention cleanup purges files >24h — see recordings.py),
    else None."""
    audio_path = row.get("audio_path")
    if not audio_path:
        return None
    from core.recordings import recordings_dir
    base = recordings_dir().resolve()
    candidate = (base / audio_path).resolve()
    # Defense in depth: audio_path is written by us (basename only), but if
    # the DB were ever tampered with, refuse anything that escapes the
    # recordings dir or smuggles separators.
    if candidate.parent != base or candidate.name != audio_path:
        return None
    return candidate if candidate.is_file() else None


def _with_has_audio(rows: list) -> list:
    for row in rows:
        row["has_audio"] = _audio_file_for(row) is not None
    return rows


@app.route("/api/transcriptions")
def get_transcriptions():
    db = TranscriptionDB()
    limit = request.args.get("limit", 50, type=int)
    q = request.args.get("q", "").strip()
    rows = db.search(q, limit=limit) if q else db.get_recent(limit=limit)
    return jsonify(_with_has_audio(rows))


@app.route("/api/audio/<int:transcription_id>")
def get_audio(transcription_id: int):
    """Serve the debug WAV for a transcription, if it still exists (retention
    purges files after RECORDINGS_RETENTION_HOURS)."""
    db = TranscriptionDB()
    row = db.get_by_id(transcription_id)
    if not row:
        return jsonify({"error": "Transcription not found"}), 404
    audio_file = _audio_file_for(row)
    if not audio_file:
        return jsonify({"error": "Audio not available (never saved or expired)"}), 404
    # send_from_directory sanitizes the filename against path traversal —
    # never build the path from client-supplied input.
    return send_from_directory(audio_file.parent, audio_file.name, mimetype="audio/wav")


@app.route("/api/transcriptions/<int:transcription_id>/retranscribe", methods=["POST"])
def retranscribe(transcription_id: int):
    """Re-run transcription + dictionary + cleaner on the saved WAV.

    Does NOT paste anywhere and does NOT use app-context tone adaptation
    (there's no "current" foreground app for a dashboard action).
    """
    if _transcriber is None:
        return jsonify({"error": "Transcriber not available"}), 503
    db = TranscriptionDB()
    row = db.get_by_id(transcription_id)
    if not row:
        return jsonify({"error": "Transcription not found"}), 404
    audio_file = _audio_file_for(row)
    if not audio_file:
        return jsonify({"error": "Audio not available (never saved or expired)"}), 404

    try:
        import io
        wav_bytes = audio_file.read_bytes()
        raw_text = _transcriber.transcribe(io.BytesIO(wav_bytes))
        if not raw_text:
            return jsonify({"error": "No speech detected"}), 422
        if _dictionary:
            raw_text = _dictionary.apply(raw_text)
        from core.cleaner import clean_dictation
        user_vocab = _dictionary.user_vocabulary_words() if _dictionary else []
        cleaned, was_cleaned = clean_dictation(raw_text, user_vocab=user_vocab, app_context=None)
        db.update_model_and_texts(
            transcription_id, text=cleaned, raw_text=raw_text,
            model="re-transcribed", was_cleaned=was_cleaned,
        )
        return jsonify({"ok": True, "text": cleaned, "raw_text": raw_text})
    except Exception as e:
        return jsonify({"error": f"Re-transcription failed: {e}"}), 500


@app.route("/api/transcriptions/<int:transcription_id>", methods=["PATCH"])
def patch_transcription(transcription_id: int):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing body"}), 400
    text = data.get("text")
    raw_text = data.get("raw_text")
    if text is None and raw_text is None:
        return jsonify({"error": "Nothing to update"}), 400
    db = TranscriptionDB()
    updated = db.update_texts(transcription_id, text=text, raw_text=raw_text)
    return jsonify({"updated": updated})

@app.route("/api/correct", methods=["POST"])
def correct_transcription():
    """Feed a correction to the dictionary so it can learn new vocabulary words.

    Payload: {original: '...', corrected: '...'}
    The dictionary only extracts new *words* from the corrected text to bias
    Whisper's prompt next time — it never stores wrong→right substitutions.
    """
    data = request.get_json()
    if not data or "original" not in data or "corrected" not in data:
        return jsonify({"error": "Missing original/corrected"}), 400
    if _dictionary:
        _dictionary.learn(data["original"], data["corrected"])
        return jsonify({"ok": True, "vocabulary": _dictionary._db.get_vocabulary_words()})
    return jsonify({"error": "Dictionary not initialized"}), 500


@app.route("/api/vocabulary")
def get_vocabulary():
    """Return all vocabulary words learned from user corrections."""
    db = TranscriptionDB()
    return jsonify(db.get_vocabulary_words())


@app.route("/api/vocabulary", methods=["POST"])
def add_vocabulary():
    """Manually add a word to the dictionary (no correction needed)."""
    body = request.get_json(silent=True) or {}
    word = (body.get("word") or "").strip()
    if not word:
        return jsonify({"error": "Missing 'word'"}), 400
    if len(word) < 2 or len(word) > 80:
        return jsonify({"error": "Word must be 2-80 chars"}), 400
    db = TranscriptionDB()
    db.add_vocabulary_word(word)
    if _dictionary:
        _dictionary.reload()
    return jsonify({"ok": True, "vocabulary": db.get_vocabulary_words()})


@app.route("/api/vocabulary/<word>", methods=["DELETE"])
def delete_vocabulary(word: str):
    db = TranscriptionDB()
    removed = db.delete_vocabulary_word(word)
    if _dictionary:
        _dictionary.reload()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/vocabulary/candidates")
def get_vocabulary_candidates():
    """Pending suggestions from passive learning (Dictionary.observe()).

    Words that showed up in a cleaned transcript but not in the raw one,
    waiting to be seen enough times (or approved by hand) before joining
    the real vocabulary.
    """
    db = TranscriptionDB()
    return jsonify(db.get_vocab_candidates(status="pending"))


@app.route("/api/vocabulary/candidates/<word>/approve", methods=["POST"])
def approve_vocabulary_candidate(word: str):
    """Promote a pending candidate into the real vocabulary immediately."""
    db = TranscriptionDB()
    db.add_vocabulary_word(word)
    db.set_vocab_candidate_status(word, "approved")
    if _dictionary:
        _dictionary.reload()
    return jsonify({"ok": True, "vocabulary": db.get_vocabulary_words()})


@app.route("/api/vocabulary/candidates/<word>/reject", methods=["POST"])
def reject_vocabulary_candidate(word: str):
    """Mark a candidate rejected — it will never be bumped or re-proposed."""
    db = TranscriptionDB()
    db.set_vocab_candidate_status(word, "rejected")
    return jsonify({"ok": True})


@app.route("/api/transcriptions/older-than/<int:days>", methods=["DELETE"])
def delete_older_than(days: int):
    """Delete transcriptions older than N days. Returns count deleted."""
    if days < 0 or days > 3650:
        return jsonify({"error": "days must be 0-3650"}), 400
    db = TranscriptionDB()
    removed = db.delete_older_than_days(days)
    return jsonify({"ok": True, "removed": removed})


# Back-compat: older frontends may still hit /api/corrections. Return the
# vocabulary list so they don't break, but the legacy mapping concept is gone.
@app.route("/api/corrections")
def get_corrections():
    db = TranscriptionDB()
    return jsonify({"vocabulary": db.get_vocabulary_words()})


_BRAND_FILES = {
    "favicon.svg": "image/svg+xml",
    "favicon-32.png": "image/png",
    "fluentpill-lockup-dark.svg": "image/svg+xml",
}


@app.route("/brand/<name>")
def brand_asset(name):
    # Whitelist, not a directory listing: only the files the panel uses.
    mimetype = _BRAND_FILES.get(name)
    if mimetype is None:
        return ("", 404)
    brand_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "brand")
    return send_from_directory(brand_dir, name, mimetype=mimetype)


@app.route("/logo_small.png")
def logo():
    root_dir = os.path.dirname(os.path.dirname(__file__))
    return send_from_directory(root_dir, "logo_small.png")


def start_web_server(port: int = 5000):
    """Start Flask in a daemon thread so it doesn't block the Qt event loop."""
    app.config["PANEL_PORT"] = port
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return port
