"""Assemble the self-contained README banner from its editable SVG parts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
PARTS = ROOT / "assets/banner"
OUTPUT = ROOT / "assets/lerobot-webrtc-banner.svg"
SVG = "http://www.w3.org/2000/svg"

BACKGROUND = """<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="360"
    viewBox="0 0 1280 360" role="img" aria-labelledby="title description">
  <title id="title">LeRobot × WebRTC</title>
  <desc id="description">The official Hugging Face logo, a centered LeRobot WebRTC title,
  and a diagram of authenticated signaling, encrypted joint data, and optional
  follower camera video, directly or through optional TURN. An independent companion for
  Hugging Face LeRobot, not an official endorsement.</desc>
  <defs>
    <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
      <stop stop-color="#101f35"/>
      <stop offset="1" stop-color="#07121f"/>
    </linearGradient>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
      <path d="M32 0H0V32" fill="none" stroke="#9bc5e5" stroke-opacity="0.045"/>
    </pattern>
  </defs>
  <rect width="1280" height="360" rx="24" fill="url(#background)"/>
  <rect width="1280" height="360" rx="24" fill="url(#grid)"/>
  <path d="M0 323C285 282 440 387 719 330S1120 287 1280 325"
    fill="none" stroke="#69d4ff" stroke-opacity="0.12" stroke-width="2"/>
  <circle cx="117" cy="172" r="91" fill="#ffd35c" fill-opacity="0.045"/>
  <circle cx="117" cy="172" r="78" fill="#ffd35c" fill-opacity="0.055"/>
</svg>"""


def positioned_part(filename: str, identifier: str, x: int, y: int, width: int, height: int):
    part = ElementTree.parse(PARTS / filename).getroot()
    part.attrib.update(id=identifier, x=str(x), y=str(y), width=str(width), height=str(height))
    return part


def render() -> str:
    ElementTree.register_namespace("", SVG)
    source = json.loads((PARTS / "huggingface-logo.source.json").read_text())
    logo_bytes = (PARTS / "huggingface-logo.svg").read_bytes()
    if hashlib.sha256(logo_bytes).hexdigest() != source["sha256"]:
        raise ValueError("The official Hugging Face source asset does not match its recorded hash")

    root = ElementTree.fromstring(BACKGROUND)
    logo = positioned_part("huggingface-logo.svg", "huggingface-logo", 5, 60, 224, 224)
    logo.attrib.update({"data-source": source["url"], "aria-hidden": "true"})
    root.append(logo)
    root.append(positioned_part("title-panel.svg", "title-panel", 232, 45, 592, 270))
    root.append(positioned_part("network-diagram.svg", "network-diagram", 846, 45, 386, 263))
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="unicode") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the checked-in banner is stale"
    )
    args = parser.parse_args()
    banner = render()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != banner:
            parser.exit(1, "Banner is stale; run python scripts/build_banner.py\n")
        print("Banner matches its SVG parts")
    else:
        OUTPUT.write_text(banner)
        print(f"Built {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
