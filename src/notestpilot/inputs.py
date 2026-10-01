"""Explicit, fingerprinted server-only inputs for disposable mod tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


def load_server_inputs(manifest: Path | None):
    if manifest is None:
        return []
    manifest = manifest.resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), list) or not data["files"]:
        raise ValueError("Server input manifest needs version 1 and nonempty files")
    inputs, destinations = [], set()
    for item in data["files"]:
        destination = item.get("destination", "")
        if not isinstance(destination, str):
            raise ValueError("Unsafe server input destination")
        pure = PurePosixPath(destination)
        parts = pure.parts
        # Keep these additions out of the bridge, loader, game and private data.
        if "\\" in destination or ":" in destination or ".." in parts or pure.as_posix() != destination:
            raise ValueError("Unsafe server input destination")
        plugin = len(parts) >= 3 and parts[:2] == ("BepInEx", "plugins") and parts[2].casefold() != "notestpilot" and destination.endswith(".dll")
        config = len(parts) == 3 and parts[:2] == ("BepInEx", "config") and parts[2].casefold() != "bepinex.cfg" and destination.endswith(".cfg")
        if not (plugin or config) or destination.casefold() in destinations:
            raise ValueError("Only unique plugin DLLs and plugin configs may be added")
        source = (manifest.parent / item["source"]).resolve()
        expected = item.get("sha256")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected != actual:
            raise ValueError("Server input SHA256 does not match: " + destination)
        destinations.add(destination.casefold())
        inputs.append({"source": source, "destination": destination, "sha256": actual})
    return sorted(inputs, key=lambda item: item["destination"])


def describe_server_inputs(inputs):
    # Source paths stay private; the report needs file names and exact bytes.
    return [{"destination": item["destination"], "sha256": item["sha256"]} for item in inputs]


def apply_server_inputs(lab: Path, inputs, server_was_present: bool):
    marker = lab / ".notestpilot-server-inputs.json"
    description = describe_server_inputs(inputs)
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != description:
            raise ValueError("Server mods changed; choose a new disposable lab")
    elif server_was_present and inputs:
        raise ValueError("Adding server mods requires a new disposable lab")
    server = (lab / "server").resolve()
    declared = {item["destination"] for item in inputs}
    # A manually copied/unlisted mod must not contaminate a baseline run.
    plugin_root = server / "BepInEx/plugins"
    for path in plugin_root.rglob("*"):
        if path.is_file() and path.relative_to(plugin_root).parts[0] != "NOTestPilot":
            if path.relative_to(server).as_posix() not in declared:
                raise ValueError("Unlisted server plugin in disposable lab")
    for client in lab.glob("client*"):
        client_plugins = client / "BepInEx/plugins"
        for path in client_plugins.rglob("*"):
            if path.is_file() and path.relative_to(client_plugins).parts[0] != "NOTestPilot":
                raise ValueError("Server test mods must not be loaded on clients")
    for item in inputs:
        destination = server / item["destination"]
        if not destination.resolve().is_relative_to(server):
            raise ValueError("Server destination escapes disposable lab")
        if server_was_present:
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Disposable server input was modified")
        else:
            # Recheck just before copying: source could have changed since parsing.
            if hashlib.sha256(item["source"].read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Server input changed before copy")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item["source"], destination)
    marker.write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")
