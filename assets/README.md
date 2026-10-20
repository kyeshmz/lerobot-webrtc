# Banner assets

The README uses [`lerobot-webrtc-banner.svg`](lerobot-webrtc-banner.svg), assembled
from three separately editable parts:

| Part | Source | Purpose |
| --- | --- | --- |
| Official logo | [`banner/huggingface-logo.svg`](banner/huggingface-logo.svg) | Exact SVG download linked from [Hugging Face's brand page](https://huggingface.co/brand), not a redraw or website-header variant |
| Title and badges | [`banner/title-panel.svg`](banner/title-panel.svg) | Centered project title, subtitle, Python and robot labels |
| Connection diagram | [`banner/network-diagram.svg`](banner/network-diagram.svg) | Separate authenticated signaling and encrypted WebRTC data paths, with optional TURN |

The logo's [source record](banner/huggingface-logo.source.json) pins its official
download URL, revision and SHA-256. Keep that source SVG unchanged. The assembler
scales it uniformly and embeds its original paths and colors directly into the
banner. No external images, fonts or network requests are needed to display it.

From the repository root, rebuild or check the combined banner:

```bash
python scripts/build_banner.py
python scripts/build_banner.py --check
```

Edit the title or diagram source, then rebuild; do not edit the generated banner
directly. The SVG sources can be opened separately for inspection. Their colors
target the banner's dark background, so view the transparent title panel on a dark
canvas. Tests check source consistency, official logo geometry, accessibility and
centered labels.

The Hugging Face logo belongs to Hugging Face. Its use identifies the upstream
LeRobot project; it does not imply endorsement of this independent companion.
The remaining banner layout and illustrations are original to this repository.
