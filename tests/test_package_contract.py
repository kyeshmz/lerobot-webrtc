import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def test_supported_lerobot_tracks_are_pinned_in_separate_manifests():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    hardware = project["project"]["optional-dependencies"]["hardware"]
    assert hardware == ["lerobot[feetech]==0.6.1"]
    assert project["project"]["optional-dependencies"]["b601"] == ["motorbridge==0.5.6"]
    example = ROOT / "examples/b601-sdk"
    environment = tomllib.loads((example / "pyproject.toml").read_text())
    requirements = environment["project"]["dependencies"]
    assert "lerobot-webrtc[b601,dev]" in requirements
    requirement = next(value for value in requirements if value.startswith("lerobot[rebot]"))
    match = re.fullmatch(
        r"lerobot\[rebot\] @ git\+https://github.com/huggingface/lerobot.git@([a-f0-9]{40})",
        requirement,
    )
    assert match
    assert environment["tool"]["uv"]["sources"]["lerobot-webrtc"]["path"] == "../.."
    assert (example / ".python-version").read_text().strip() == "3.12"
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    sdk = [package for package in lock["package"] if package["name"] == "lerobot"]
    assert len(sdk) == 1 and sdk[0]["version"] == "0.6.1" and "registry" in sdk[0]["source"]
    source_lock = tomllib.loads((example / "uv.lock").read_text())
    sdk = [package for package in source_lock["package"] if package["name"] == "lerobot"]
    assert len(sdk) == 1 and sdk[0]["version"] == "0.6.2"
    assert sdk[0]["source"]["git"].endswith(match.group(1))


def test_wheel_requirements_do_not_pretend_the_source_sdk_is_a_pypi_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    release = Version(project["version"])
    assert len(release.release) == 3
    assert str(release) == project["version"]
    requirements = project["dependencies"] + [
        value for values in project["optional-dependencies"].values() for value in values
    ]
    assert not any("git+" in value for value in requirements)
    assert not any("lerobot" in value and "0.6.2" in value for value in requirements)


def test_source_sdk_does_not_leak_its_cuda_index_into_other_tracks():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    wheels = [package for package in lock["package"] if package["name"] in {"torch", "torchvision"}]
    assert wheels
    assert all(package["source"] == {"registry": "https://pypi.org/simple"} for package in wheels)


def test_banner_is_accessible_self_contained_svg_and_python_badge_matches_floor():
    svg = ROOT / "assets/lerobot-webrtc-banner.svg"
    text = svg.read_text()
    assert "<!ENTITY" not in text and "<!DOCTYPE" not in text
    image = ElementTree.fromstring(text)
    assert image.attrib["role"] == "img"
    ids = {node.attrib["id"] for node in image.iter() if "id" in node.attrib}
    assert len(ids) == sum("id" in node.attrib for node in image.iter())
    assert set(image.attrib["aria-labelledby"].split()).issubset(ids)
    assert all(
        node.tag.rsplit("}", 1)[-1] not in {"script", "foreignObject", "image"}
        for node in image.iter()
    )
    assert all(
        not (key.endswith("href") and value.startswith(("http:", "https:")))
        for node in image.iter()
        for key, value in node.attrib.items()
    )
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    floor = metadata["requires-python"].removeprefix(">=")
    readme = (ROOT / "README.md").read_text()
    assert f"Python-{floor}%2B" in readme
    assert "assets/lerobot-webrtc-banner.svg" in readme


def test_every_banner_label_is_centered_and_pills_use_their_container_centers():
    image = ElementTree.parse(ROOT / "assets/lerobot-webrtc-banner.svg").getroot()
    elements = {node.attrib["id"]: node for node in image.iter() if "id" in node.attrib}

    def check(node, inherited="start"):
        anchor = node.attrib.get("text-anchor", inherited)
        if node.tag.rsplit("}", 1)[-1] == "text":
            assert anchor == "middle"
            if "data-container" in node.attrib:
                box = elements[node.attrib["data-container"]].attrib
                assert float(node.attrib["x"]) == float(box["x"]) + float(box["width"]) / 2
                assert float(node.attrib["y"]) == float(box["y"]) + float(box["height"]) / 2
                assert node.attrib["dominant-baseline"] == "central"
        for child in node:
            check(child, anchor)

    check(image)


def test_banner_embeds_the_official_huggingface_logo_without_redrawing_it():
    image = ElementTree.parse(ROOT / "assets/lerobot-webrtc-banner.svg").getroot()
    logo = image.find(".//{http://www.w3.org/2000/svg}svg[@id='huggingface-logo']")
    assert logo is not None
    source = json.loads((ROOT / "assets/banner/huggingface-logo.source.json").read_text())
    asset = ROOT / "assets/banner/huggingface-logo.svg"
    assert source["brand_page"] == "https://huggingface.co/brand"
    assert source["url"] == (
        "https://huggingface.co/datasets/huggingface/brand-assets/resolve/"
        f"{source['revision']}/hf-logo.svg"
    )
    assert re.fullmatch(r"[a-f0-9]{40}", source["revision"])
    assert source["sha256"] == "942cad1ccda905ac5a659dfd2d78b344fccfb84a8a3ac3721e08f488205638a0"
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == source["sha256"]
    official = ElementTree.parse(asset).getroot()
    assert logo.attrib["data-source"] == source["url"]
    assert logo.attrib["viewBox"] == official.attrib["viewBox"] == "0 0 256 256"
    assert float(logo.attrib["width"]) / 256 == float(logo.attrib["height"]) / 256
    shapes = [(node.tag.rsplit("}", 1)[-1], dict(node.attrib)) for node in logo]
    original_shapes = [(node.tag.rsplit("}", 1)[-1], dict(node.attrib)) for node in official]
    assert shapes == original_shapes


def test_title_badges_have_no_off_center_decorations():
    panel = ElementTree.parse(ROOT / "assets/banner/title-panel.svg").getroot()
    badges = panel.find(".//{http://www.w3.org/2000/svg}g[@id='title-badges']")
    assert badges is not None
    assert [node.tag.rsplit("}", 1)[-1] for node in badges] == ["rect", "text"] * 3


def test_banner_is_reproducible_from_its_separate_parts():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_banner.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_network_diagram_separates_signaling_and_optional_turn_from_joint_data():
    diagram = ElementTree.parse(ROOT / "assets/banner/network-diagram.svg").getroot()
    labels = " ".join(diagram.itertext()).upper()
    for label in ("LEADER", "FOLLOWER", "WEBRTC", "SIGNALING", "TURN", "OPTIONAL"):
        assert label in labels
    ids = [node.attrib["id"] for node in diagram.iter() if "id" in node.attrib]
    assert all(identifier.startswith("diagram-") for identifier in ids)
    cards = {node.attrib["id"]: node.attrib for node in diagram.iter() if "id" in node.attrib}
    for node in diagram.iter():
        if "data-card" in node.attrib:
            box = cards[node.attrib["data-card"]]
            assert float(node.attrib["x"]) == float(box["x"]) + float(box["width"]) / 2


def test_coturn_template_requires_auth_and_enables_no_placeholder_account():
    path = ROOT / "examples/coturn/turnserver.conf.example"
    settings = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "lt-cred-mech" in settings and "no-auth" not in settings
    assert not any(line.startswith("user=") for line in settings)
    assert "cli=false" in settings and "dtls=false" in settings
    assert "no-tls" in settings
    assert "no-cli" not in settings and "no-dtls" not in settings
    assert "coturn/coturn:4.18.0-r0" in (ROOT / "examples/coturn/compose.yml").read_text()
    compose = (ROOT / "examples/coturn/compose.yml").read_text()
    assert '"--lt-cred-mech"' in compose
    assert "create_host_path: false" in compose


def test_optional_livekit_script_has_its_own_pinned_sdk_lock():
    script = ROOT / "examples/livekit/probe.py"
    block = script.read_text().split("# /// script\n", 1)[1].split("# ///", 1)[0]
    metadata = tomllib.loads("\n".join(line.removeprefix("# ") for line in block.splitlines()))
    requirement = metadata["dependencies"][0]
    assert requirement.startswith("livekit==")
    expected = requirement.removeprefix("livekit==")
    lock = tomllib.loads(script.with_suffix(".py.lock").read_text())
    assert any(
        package["name"] == "livekit" and package["version"] == expected
        for package in lock["package"]
    )
