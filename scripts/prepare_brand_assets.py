#!/usr/bin/env python3
"""Prepare the generated AIive logo for web, Android and desktop consumers.

The selected ImageGen output contains a rendered transparency checkerboard.
This script removes only the border-connected neutral checkerboard, keeps the
illustration itself, and derives deterministic app-icon sizes from that source.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = PROJECT_ROOT / "assets" / "branding"
SOURCE_PATH = BRAND_DIR / "aiive-logo-source.png"
LOGO_PATH = BRAND_DIR / "aiive-logo.png"
LAUNCHER_PATH = BRAND_DIR / "aiive-app-icon.png"
WINDOWS_ICON_PATH = BRAND_DIR / "aiive-app-icon.ico"
MACOS_ICON_PATH = BRAND_DIR / "aiive-app-icon.icns"
WEB_PUBLIC_DIR = PROJECT_ROOT / "frontend" / "public"
ANDROID_PUBLIC_DIR = PROJECT_ROOT / "apps" / "android" / "public"
ANDROID_RES_DIR = PROJECT_ROOT / "apps" / "android" / "android" / "app" / "src" / "main" / "res"
LAUNCHER_BACKGROUND = (240, 234, 226, 255)  # warm ivory, #F0EAE2


def _is_checkerboard(pixel: tuple[int, int, int]) -> bool:
    """Return true for the light, near-neutral generated checkerboard pixels."""

    low = min(pixel)
    high = max(pixel)
    return low >= 225 and high - low <= 18


def extract_logo(source: Image.Image) -> Image.Image:
    """Remove only checkerboard pixels connected to the image boundary."""

    rgb = source.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    background = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if background[index] or not _is_checkerboard(pixels[x, y]):
            return
        background[index] = 1
        queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        if x > 0:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y > 0:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)

    # The hanging cord and the top frame edge enclose one checkerboard island,
    # so it cannot be reached by the border flood. Remove that large, top-only
    # neutral component as transparency as well, without touching pale painted
    # areas inside the frame.
    visited = bytearray(background)
    for start_y in range(round(height * 0.22)):
        for start_x in range(width):
            start_index = start_y * width + start_x
            if visited[start_index] or not _is_checkerboard(pixels[start_x, start_y]):
                continue
            component: list[tuple[int, int]] = []
            component_queue: deque[tuple[int, int]] = deque([(start_x, start_y)])
            visited[start_index] = 1
            min_y = start_y
            max_y = start_y
            while component_queue:
                x, y = component_queue.popleft()
                component.append((x, y))
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    index = next_y * width + next_x
                    if visited[index] or not _is_checkerboard(pixels[next_x, next_y]):
                        continue
                    visited[index] = 1
                    component_queue.append((next_x, next_y))
            if len(component) >= 5_000 and min_y < height * 0.08 and max_y < height * 0.2:
                for x, y in component:
                    background[y * width + x] = 1

    alpha = Image.new("L", (width, height), 255)
    alpha.putdata([0 if value else 255 for value in background])
    # Contract the foreground by one source pixel before feathering. This avoids
    # a pale checkerboard fringe around the Android launcher foreground.
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.65))

    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba


def contain(image: Image.Image, size: int, scale: float) -> Image.Image:
    """Center an image inside a transparent square at a deterministic scale."""

    target = max(1, round(size * scale))
    fitted = image.copy()
    fitted.thumbnail((target, target), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))
    return canvas


def save_web_assets(logo: Image.Image) -> None:
    WEB_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    logo.resize((1024, 1024), Image.Resampling.LANCZOS).save(
        WEB_PUBLIC_DIR / "aiive-logo.png",
        optimize=True,
    )
    logo.resize((64, 64), Image.Resampling.LANCZOS).save(
        WEB_PUBLIC_DIR / "favicon.png",
        optimize=True,
    )


def save_android_assets(logo: Image.Image, launcher: Image.Image) -> None:
    ANDROID_PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    logo.resize((512, 512), Image.Resampling.LANCZOS).save(
        ANDROID_PUBLIC_DIR / "aiive-logo.png",
        optimize=True,
    )

    if not ANDROID_RES_DIR.exists():
        return

    legacy_sizes = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }
    for density, size in legacy_sizes.items():
        output_dir = ANDROID_RES_DIR / density
        output_dir.mkdir(parents=True, exist_ok=True)
        icon = launcher.resize((size, size), Image.Resampling.LANCZOS)
        icon.save(output_dir / "ic_launcher.png", optimize=True)
        icon.save(output_dir / "ic_launcher_round.png", optimize=True)
        contain(logo, size, 0.68).save(
            output_dir / "ic_launcher_foreground.png",
            optimize=True,
        )

    for splash_path in ANDROID_RES_DIR.glob("drawable*/splash.png"):
        with Image.open(splash_path) as current_splash:
            splash_size = current_splash.size
        splash = Image.new("RGBA", splash_size, (25, 23, 27, 255))
        side = min(splash_size)
        splash_logo = contain(logo, side, 0.34)
        splash.alpha_composite(
            splash_logo,
            ((splash_size[0] - side) // 2, (splash_size[1] - side) // 2),
        )
        splash.convert("RGB").save(splash_path, optimize=True)


def save_desktop_assets(launcher: Image.Image) -> None:
    """生成 Electron 打包器在 Windows 和 macOS 上使用的原生图标容器。"""
    launcher.save(
        WINDOWS_ICON_PATH,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    launcher.save(MACOS_ICON_PATH, format="ICNS")


def main() -> None:
    if not SOURCE_PATH.exists():
        raise SystemExit(f"Logo source not found: {SOURCE_PATH}")

    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    logo = extract_logo(Image.open(SOURCE_PATH))
    logo = logo.resize((1024, 1024), Image.Resampling.LANCZOS)
    logo.save(LOGO_PATH, optimize=True)

    # Android adaptive icons require an opaque, full-bleed square background;
    # the launcher applies its own circle/squircle mask on top of these layers.
    launcher = Image.new("RGBA", (1024, 1024), LAUNCHER_BACKGROUND)
    launcher.alpha_composite(contain(logo, 1024, 0.86))
    launcher.convert("RGB").save(LAUNCHER_PATH, optimize=True)

    save_web_assets(logo)
    save_android_assets(logo, launcher)
    save_desktop_assets(launcher)
    print(f"Prepared {LOGO_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Prepared {LAUNCHER_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Prepared {WINDOWS_ICON_PATH.relative_to(PROJECT_ROOT)}")
    print(f"Prepared {MACOS_ICON_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
