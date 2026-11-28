#!/usr/bin/env python3
"""Render a crisp 20-second animated terminal GIF + static WebP poster from real `cairnmind try` output.

Reads the verbatim lines captured in `website/src/data/try-output.json` so the animation
stays 100% faithful to the real CLI (verified by `tests/test_website_sync.py`).
"""
from __future__ import annotations

import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "src" / "data" / "try-output.json"
OUT_GIF = ROOT / "public" / "media" / "try-demo.gif"
OUT_POSTER = ROOT / "public" / "media" / "try-demo-poster.webp"

W, H = 960, 540
BG = (11, 14, 20)
CHROME = (18, 22, 31)
BORDER = (42, 51, 66)
TEXT = (224, 232, 242)
STRONG = (244, 248, 252)
MUTED = (139, 151, 166)
DIM = (96, 108, 124)
ACCENT = (135, 175, 215)
OK = (143, 208, 138)
OK_BG = (18, 38, 28)
DANGER = (244, 112, 103)
DANGER_BG = (45, 20, 24)
WARN = (240, 207, 135)


def load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


FONT_MONO = load_font(
    [
        r"C:\Windows\Fonts\CascadiaMono.ttf",
        r"C:\Windows\Fonts\consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ],
    14,
)
FONT_MONO_B = load_font(
    [
        r"C:\Windows\Fonts\consolab.ttf",
        r"C:\Windows\Fonts\CascadiaMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    ],
    14,
)
FONT_UI = load_font(
    [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    12,
)
FONT_UI_B = load_font(
    [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    12,
)


def wrap_line(text: str, max_chars: int = 96) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    indent = " " * (len(text) - len(text.lstrip(" ")))
    words = text.strip().split(" ")
    out: list[str] = []
    cur = indent
    for w in words:
        candidate = f"{cur}{w}" if cur == indent else f"{cur} {w}"
        if len(candidate) > max_chars and cur.strip():
            out.append(cur)
            cur = indent + w
        else:
            cur = candidate
    if cur.strip():
        out.append(cur)
    return out


def classify_line(line: str) -> tuple[tuple[int, int, int], bool, tuple[int, int, int] | None, tuple[int, int, int] | None]:
    """Return (fg_color, is_bold, row_bg, left_bar_color)."""
    s = line.strip()
    if s.startswith("Cairnmind Try Sandbox"):
        return STRONG, True, None, None
    if s.startswith("===="):
        return DIM, False, None, None
    if s.startswith("Indexed "):
        return MUTED, False, None, None
    if s.startswith("[1/3]") or s.startswith("[2/3]") or s.startswith("[3/3]"):
        return ACCENT, True, None, None
    if "-> BLOCKED" in line or "Task completion requires fresh" in line:
        return DANGER, "BLOCKED" in line, DANGER_BG, DANGER
    if "-> SUCCESS" in line:
        return OK, True, OK_BG, OK
    if "-> PASS" in line:
        return OK, False, None, None
    if "Verified verbatim quote" in line or "A: unknown" in line:
        return OK, False, None, None
    if s.startswith("Q: "):
        return STRONG, False, None, None
    if "Edited trail_notes/store.py" in line:
        return WARN, False, None, None
    return TEXT, False, None, None


def render_frame(
    typed_cmd: str,
    cursor_on: bool,
    visible_lines: list[str],
    elapsed_ms: int,
    total_ms: int,
    active_step: int,
) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Outer terminal frame
    draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=14, fill=BG, outline=BORDER, width=1)
    # Header chrome
    draw.rounded_rectangle([1, 1, W - 2, 42], radius=14, fill=CHROME)
    draw.rectangle([1, 28, W - 2, 42], fill=CHROME)
    draw.line([1, 42, W - 2, 42], fill=BORDER, width=1)

    # Window traffic lights
    draw.ellipse([18, 15, 29, 26], fill=(244, 112, 103))
    draw.ellipse([36, 15, 47, 26], fill=(240, 207, 135))
    draw.ellipse([54, 15, 65, 26], fill=(143, 208, 138))

    # Title + right status
    draw.text((80, 13), "cairnmind try · ephemeral in-memory SQLite (0 files written)", fill=MUTED, font=FONT_UI)

    # Step badges in top-right chrome
    badges = [("1. Unknown", 1), ("2. Blocked", 2), ("3. Verified", 3)]
    bx = W - 295
    for label, idx in badges:
        bw = 88
        if active_step == idx:
            bg_c = DANGER_BG if idx == 2 else OK_BG if idx == 3 else (24, 42, 66)
            fg_c = DANGER if idx == 2 else OK if idx == 3 else ACCENT
            bd_c = fg_c
        else:
            bg_c, fg_c, bd_c = (24, 29, 40), DIM, BORDER
        draw.rounded_rectangle([bx, 10, bx + bw, 32], radius=6, fill=bg_c, outline=bd_c, width=1)
        draw.text((bx + 10, 14), label, fill=fg_c, font=FONT_UI_B)
        bx += bw + 8

    # Prompt line
    y = 56
    prompt_prefix = "$ "
    draw.text((24, y), prompt_prefix, fill=ACCENT, font=FONT_MONO_B)
    px = 24 + int(draw.textlength(prompt_prefix, font=FONT_MONO_B))
    draw.text((px, y), typed_cmd, fill=STRONG, font=FONT_MONO_B)
    if cursor_on:
        cx = px + int(draw.textlength(typed_cmd, font=FONT_MONO_B)) + 2
        draw.rectangle([cx, y + 2, cx + 8, y + 17], fill=ACCENT)

    y += 26

    # Render real captured CLI lines
    for raw in visible_lines:
        if not raw.strip():
            y += 9
            continue
        wrapped = wrap_line(raw, 96)
        fg, bold, row_bg, bar = classify_line(raw)
        font = FONT_MONO_B if bold else FONT_MONO
        line_h = 20
        block_h = len(wrapped) * line_h + (6 if row_bg else 0)
        if y + block_h > H - 34:
            break
        if row_bg:
            draw.rounded_rectangle([20, y - 3, W - 20, y + len(wrapped) * line_h + 2], radius=6, fill=row_bg)
        if bar:
            draw.rectangle([20, y - 3, 24, y + len(wrapped) * line_h + 2], fill=bar)
        for sub in wrapped:
            draw.text((30 if row_bg else 24, y), sub, fill=fg, font=font)
            y += line_h
        if row_bg:
            y += 4

    # Footer timeline bar (20.0s loop)
    draw.line([1, H - 28, W - 2, H - 28], fill=BORDER, width=1)
    secs = min(total_ms, elapsed_ms) / 1000.0
    total_s = total_ms / 1000.0
    draw.text(
        (24, H - 22),
        "Real CLI output captured from src/cairnmind/try_demo.py",
        fill=DIM,
        font=FONT_UI,
    )
    timer_text = f"0:{round(secs):02d} / 0:{round(total_s):02d}"
    tw = int(draw.textlength(timer_text, font=FONT_UI_B))
    draw.text((W - 24 - tw, H - 22), timer_text, fill=MUTED, font=FONT_UI_B)

    # Progress bar track & fill
    bar_w = W - 48
    frac = max(0.0, min(1.0, elapsed_ms / total_ms))
    draw.rectangle([24, H - 6, 24 + bar_w, H - 3], fill=(28, 34, 46))
    fill_color = DANGER if active_step == 2 else OK if active_step == 3 else ACCENT
    if frac > 0:
        draw.rectangle([24, H - 6, 24 + int(bar_w * frac), H - 3], fill=fill_color)

    return img


def main() -> None:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    # Show the core 3-step proof lines (exclude trailing "Next steps" so all 3 acts fit cleanly without clipping)
    all_lines: list[str] = []
    for ln in payload["lines"]:
        if ln.startswith("Next steps"):
            break
        all_lines.append(ln)
    while all_lines and not all_lines[-1].strip():
        all_lines.pop()

    cmd = "uvx cairnmind try"
    # Find indices of major milestones in all_lines
    idx_banner = next(i for i, l in enumerate(all_lines) if l.startswith("Indexed ")) + 1
    idx_s1_q1 = next(i for i, l in enumerate(all_lines) if "--turbo-cache-flag" in l) + 1
    idx_s1_a1 = idx_s1_q1 + 1
    idx_s1_end = next(i for i, l in enumerate(all_lines) if "Verified verbatim quote" in l) + 1
    idx_s2_hdr = next(i for i, l in enumerate(all_lines) if l.startswith("[2/3]")) + 1
    idx_s2_pass = idx_s2_hdr + 1
    idx_s2_edit = idx_s2_pass + 1
    idx_s2_blocked = next(i for i, l in enumerate(all_lines) if "Task completion requires" in l) + 1
    idx_s3_hdr = next(i for i, l in enumerate(all_lines) if l.startswith("[3/3]")) + 1
    idx_s3_pass = idx_s3_hdr + 1
    idx_s3_done = len(all_lines)

    # Timeline specification: frame 0 starts immediately with command + sandbox banner (never a blank screen!)
    spec: list[tuple[str, bool, int, int, int]] = [
        # 1. Command already run + sandbox banner (~1.4s)
        (cmd, False, idx_banner, 0, 1400),
        # 2. Step 1: No guessing — unknown vs verbatim quote (~5.0s)
        (cmd, False, idx_s1_q1, 1, 1100),
        (cmd, False, idx_s1_a1, 1, 1400),
        (cmd, False, idx_s1_end, 1, 2500),
        # 3. Step 2: Dramatic BLOCKED hold (~6.8s)
        (cmd, False, idx_s2_hdr, 2, 700),
        (cmd, False, idx_s2_pass, 2, 1000),
        (cmd, False, idx_s2_edit, 2, 1100),
        (cmd, False, idx_s2_blocked, 2, 4000),
        # 4. Step 3: Fresh proof unlocks verified done (~6.8s)
        (cmd, False, idx_s3_hdr, 3, 700),
        (cmd, False, idx_s3_pass, 3, 1300),
        (cmd, False, idx_s3_done, 3, 4800),
    ]

    total_ms = sum(item[4] for item in spec)
    frames: list[Image.Image] = []
    durations: list[int] = []
    elapsed = 0
    for idx_frame, (typed, cursor_on, count, step, dur) in enumerate(spec):
        shown_ms = total_ms if idx_frame == len(spec) - 1 else elapsed + dur
        frame = render_frame(typed, cursor_on, all_lines[:count], shown_ms, total_ms, step)
        frames.append(frame)
        durations.append(dur)
        elapsed += dur

    # Save crisp static climax poster (final frame with all 3 proofs visible)
    OUT_POSTER.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(OUT_POSTER, format="WEBP", quality=92, method=6)

    # Quantize frames with a shared adaptive palette for clean, tiny GIF output
    palette_src = frames[-1].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    q_frames = [f.quantize(palette=palette_src, dither=Image.Dither.NONE) for f in frames]
    q_frames[0].save(
        OUT_GIF,
        save_all=True,
        append_images=q_frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(
        f"[generate-try-demo] wrote {OUT_GIF} ({OUT_GIF.stat().st_size / 1024:.1f} KB, "
        f"{len(frames)} frames, {total_ms / 1000:.1f}s) and {OUT_POSTER} ({OUT_POSTER.stat().st_size / 1024:.1f} KB)"
    )


if __name__ == "__main__":
    main()
