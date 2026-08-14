#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
GO_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PINNED_IMAGE_PATTERN = re.compile(
    r"^docker\.io/library/ros@sha256:(?P<digest>[0-9a-f]{64})$"
)
REPOSITORY = "https://github.com/lxk36/xgc2-media-edge.git"
ROS_PAIRS = ("noetic-focal", "humble-jammy", "jazzy-noble")
GO_ARCHES = ("amd64", "arm64")


def load_lock(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "go",
        "mediaEdge",
        "rosImages",
    }:
        raise ValueError("integration lock has unsupported top-level fields")
    if value["schema"] != "xgc2.integration-lock/v1":
        raise ValueError("integration lock schema is invalid")
    media_edge = value["mediaEdge"]
    if not isinstance(media_edge, dict) or set(media_edge) != {
        "repository",
        "sourceSha",
        "version",
    }:
        raise ValueError("integration lock Media Edge fields are invalid")
    if media_edge["repository"] != REPOSITORY:
        raise ValueError("integration lock Media Edge repository is invalid")
    if not isinstance(media_edge["sourceSha"], str) or not SHA_PATTERN.fullmatch(
        media_edge["sourceSha"]
    ):
        raise ValueError("integration lock Media Edge source SHA is invalid")
    if not isinstance(media_edge["version"], str) or not VERSION_PATTERN.fullmatch(
        media_edge["version"]
    ):
        raise ValueError("integration lock Media Edge version is invalid")

    go = value["go"]
    if not isinstance(go, dict) or set(go) != {"linuxSha256", "version"}:
        raise ValueError("integration lock Go fields are invalid")
    if not isinstance(go["version"], str) or not GO_VERSION_PATTERN.fullmatch(
        go["version"]
    ):
        raise ValueError("integration lock Go version is invalid")
    go_hashes = go["linuxSha256"]
    if not isinstance(go_hashes, dict) or set(go_hashes) != set(GO_ARCHES):
        raise ValueError("integration lock Go architecture hashes are invalid")
    for architecture in GO_ARCHES:
        digest = go_hashes[architecture]
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise ValueError(
                "integration lock Go hash is invalid for " + architecture
            )

    ros_images = value["rosImages"]
    if not isinstance(ros_images, dict) or set(ros_images) != set(ROS_PAIRS):
        raise ValueError("integration lock ROS image pairs are invalid")
    for pair in ROS_PAIRS:
        image = ros_images[pair]
        if not isinstance(image, str) or not PINNED_IMAGE_PATTERN.fullmatch(image):
            raise ValueError("integration lock ROS image is not digest-pinned: " + pair)
    return value


def dependency_set_digest(lock: Dict[str, Any]) -> str:
    media_edge = lock["mediaEdge"]
    dependency_set = [
        {
            "id": "xgc2-media-edge",
            "action": "verify",
            "source_sha": media_edge["sourceSha"],
            "version": media_edge["version"],
            "policy": "verify",
        }
    ]
    encoded = json.dumps(
        dependency_set,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument(
        "--field",
        choices=(
            "repository",
            "sourceSha",
            "version",
            "dependencySetDigest",
            "goVersion",
            "goSha256",
            "rosImage",
        ),
    )
    parser.add_argument("--architecture", choices=GO_ARCHES)
    parser.add_argument("--ros-distro")
    parser.add_argument("--ubuntu")
    args = parser.parse_args()
    lock = load_lock(args.lock)
    if not args.field:
        return
    if args.field in ("repository", "sourceSha", "version"):
        print(lock["mediaEdge"][args.field])
        return
    if args.field == "dependencySetDigest":
        print(dependency_set_digest(lock))
        return
    if args.field == "goVersion":
        print(lock["go"]["version"])
        return
    if args.field == "goSha256":
        if not args.architecture:
            parser.error("--architecture is required for goSha256")
        print(lock["go"]["linuxSha256"][args.architecture])
        return
    if not args.ros_distro or not args.ubuntu:
        parser.error("--ros-distro and --ubuntu are required for rosImage")
    pair = args.ros_distro + "-" + args.ubuntu
    if pair not in lock["rosImages"]:
        parser.error("unsupported ROS/Ubuntu pair: " + pair)
    print(lock["rosImages"][pair])


if __name__ == "__main__":
    main()
