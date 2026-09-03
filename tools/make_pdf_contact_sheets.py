from pathlib import Path

from PIL import Image


root = Path("output/pdf/rendered")
pages = sorted(root.glob("page-*.png"))
for group_index, start in enumerate(range(0, len(pages), 7), 1):
    canvas = Image.new("RGB", (1040, 2940), (210, 215, 220))
    for slot, path in enumerate(pages[start : start + 7]):
        page = Image.open(path).convert("RGB")
        page.thumbnail((500, 707))
        x = (slot % 2) * 520 + 10
        y = (slot // 2) * 735 + 10
        canvas.paste(page, (x, y))
    canvas.save(root / f"contact-{group_index}.jpg", quality=90)
