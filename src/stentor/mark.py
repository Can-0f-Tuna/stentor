"""The Stentor mark at small sizes: a helmet in profile with the voice leaving the mouth. Plain shapes on a 128 grid,
so the tray icon and the pages the app serves draw it without an image file."""

HELMET = (
    "14.6,104.9 17,103.2 19.3,101.2 21.3,98.9 23,96.4 24.2,93.5 25,90.4 25.3,86.9 24.8,84 24.4,81.3 24,78.7 23.6,76.1 23.4,73.5 23.2,70.8 23.1,67.9 24,60.2 "
    "26.5,53.2 30.4,47.1 35.5,42.2 41.8,38.4 48.9,36.1 56.7,35.3 63.8,36 70.2,38 76,41.2 80.8,45.5 84.8,50.8 87.7,57.1 89.4,64.1 91.1,91 91.1,92.1 90.9,93 "
    "90.7,93.7 90.3,94.3 89.9,94.8 89.4,95 88.9,95.1 88.5,95.1 88.2,95 87.9,94.8 87.7,94.5 87.6,94.2 87.5,93.8 87.4,93.4 85.2,71.8 82.6,70.9 79.8,70.4 "
    "76.8,70 73.8,70 70.7,70.2 67.7,70.7 64.9,71.6 64.1,71.9 63.4,72.2 62.7,72.7 62,73.1 61.4,73.7 60.7,74.2 60.1,74.8 62.4,77 64.9,78.8 67.5,80.2 "
    "70.3,81.2 73,81.8 75.8,82.2 78.6,82.1 79.3,83.7 79.7,85.6 79.8,88 79.8,90.5 79.4,93.3 78.9,96.1 78,98.9 77.5,101.3 76.9,103.5 76.3,105.7 75.6,107.7 "
    "74.8,109.6 73.9,111.4 73,113.1 70.6,113.2 68.2,113.2 65.7,113 63.2,112.5 60.7,111.9 58.2,111.1 55.8,110.1 53.1,108.5 50.5,107 47.8,105.6 45.2,104.4 "
    "42.5,103.4 39.8,102.6 36.9,101.9 33.4,101.6 30,101.5 26.7,101.7 23.5,102.1 20.4,102.8 17.4,103.8 14.6,104.9 "
)
CREST = (
    "78.7,39.2 81.8,32.7 79.6,30.5 77.5,27.4 75,23.9 72.1,20.4 68.7,17.4 65,15.5 61.4,15 57.7,14.7 54.1,14.7 50.5,15 46.9,15.5 43.3,16.3 39.8,17.3 "
    "36.4,18.6 33,20 29.8,21.7 26.7,23.7 23.7,25.8 20.9,28.1 18.3,30.6 15.9,33.3 14,36.2 12.5,39.2 11.4,42.2 10.6,45.2 10.2,48.2 10.1,51.1 10.3,53.9 "
    "10.7,56.5 11.4,59.1 12.2,61.5 13.1,63.8 14.1,65.9 15.2,67.9 16.4,69.9 17.5,71.7 18.7,73.5 19.7,75.3 20.7,77.2 21.6,79.2 21.6,79.2 20.6,75.5 20,71.6 "
    "19.8,67.8 20,63.9 20.6,60.1 21.7,56.4 23.1,52.8 24.9,49.3 27,46.1 29.5,43.1 32.3,40.5 35.3,38.1 38.6,36 42.1,34.3 45.8,33 49.5,32.1 53.3,31.6 "
    "57.2,31.5 61.1,31.8 64.9,32.5 68.6,33.6 72.1,35.1 75.5,37 78.7,39.2 "
)
VOICE = ((97.6, 70.5, 107.8, 64.1), (98.8, 86.9, 119.5, 86.9), (97.6, 103.3, 107.8, 109.6))  # three lines, x0 y0 x1 y1
VOICE_WIDTH = 9.5
INK, ACCENT = "#0c0c0d", "#2a46ff"                # on a light page
INK_DARK, ACCENT_DARK = "#ededeb", "#6b80ff"      # on a dark one


def _points(text: str) -> list[tuple[float, float]]:
    return [tuple(float(v) for v in pair.split(",")) for pair in text.split()]


def svg(size: int, ink: str = "currentColor", accent: str = ACCENT) -> str:
    lines = "".join(f"M{x0} {y0}L{x1} {y1}" for x0, y0, x1, y1 in VOICE)
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 128 128" aria-hidden="true">'
            f'<polygon points="{HELMET.strip()}" fill="{ink}"/><polygon points="{CREST.strip()}" fill="{ink}"/>'
            f'<path d="{lines}" stroke="{accent}" stroke-width="{VOICE_WIDTH}" stroke-linecap="round" fill="none"/></svg>')


def image(size: int = 64):
    """The tray icon. A taskbar or menu bar can be light or dark and the app can't see which, so the helmet is light
    with a dark edge: one of the two always shows."""
    from PIL import Image, ImageDraw
    k = 4 * size / 128  # drawn four times too large and scaled down, for smooth edges
    img = Image.new("RGBA", (4 * size, 4 * size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = lambda pts: [(x * k, y * k) for x, y in pts]
    shapes = [scale(_points(HELMET)), scale(_points(CREST))]
    edge = round(5 * k)
    for pts in shapes:
        draw.line(pts + pts[:2], fill="#0c0c0d", width=2 * edge, joint="curve")
    for x0, y0, x1, y1 in VOICE:
        _rounded_line(draw, (x0 * k, y0 * k, x1 * k, y1 * k), VOICE_WIDTH * k + 2 * edge, "#0c0c0d")
    for pts in shapes:
        draw.polygon(pts, fill=INK_DARK)
    for x0, y0, x1, y1 in VOICE:
        _rounded_line(draw, (x0 * k, y0 * k, x1 * k, y1 * k), VOICE_WIDTH * k, ACCENT_DARK)
    return img.resize((size, size), Image.LANCZOS)


def _rounded_line(draw, line, width, colour):
    x0, y0, x1, y1 = line
    r = width / 2
    draw.line(line, fill=colour, width=round(width))
    for x, y in ((x0, y0), (x1, y1)):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=colour)
