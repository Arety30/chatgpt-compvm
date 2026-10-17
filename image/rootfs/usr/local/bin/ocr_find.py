#!/usr/bin/env python3
"""Screenshot the desktop, OCR it with tesseract, print JSON matches for a query.
Each match: {"text":..., "x":centerX, "y":centerY}. Empty query -> all words."""
import sys, os, json, subprocess
from itertools import groupby

query = (sys.argv[1] if len(sys.argv) > 1 else "").lower().strip()
img = "/tmp/_ocr.png"
env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
subprocess.run(["scrot", "-o", img], env=env, check=False)
tsv = subprocess.run(["tesseract", img, "stdout", "--psm", "11", "tsv"],
                     capture_output=True, text=True).stdout

words = []
for line in tsv.splitlines()[1:]:
    c = line.split("\t")
    if len(c) < 12:
        continue
    try:
        conf = float(c[10])
    except ValueError:
        conf = -1
    text = c[11].strip()
    if not text or conf < 40:
        continue
    left, top, w, h = map(int, c[6:10])
    words.append({"text": text, "cx": left + w // 2, "cy": top + h // 2,
                  "left": left, "top": top, "w": w, "h": h,
                  "key": (c[2], c[3], c[4])})

matches = []
if not query:
    matches = [{"text": wd["text"], "x": wd["cx"], "y": wd["cy"]} for wd in words]
else:
    for wd in words:
        if query in wd["text"].lower():
            matches.append({"text": wd["text"], "x": wd["cx"], "y": wd["cy"]})
    if not matches and " " in query:      # phrase spanning a line
        for _, grp in groupby(words, key=lambda x: x["key"]):
            g = list(grp)
            if query in " ".join(x["text"] for x in g).lower():
                xs = [x["left"] for x in g] + [x["left"] + x["w"] for x in g]
                ys = [x["top"] for x in g] + [x["top"] + x["h"] for x in g]
                matches.append({"text": " ".join(x["text"] for x in g),
                                "x": (min(xs) + max(xs)) // 2,
                                "y": (min(ys) + max(ys)) // 2})
print(json.dumps(matches))
