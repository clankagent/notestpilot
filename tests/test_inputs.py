import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from notestpilot.inputs import describe_server_inputs, load_server_inputs
from notestpilot.runner import prepare_lab


class ServerInputTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "game"
        bridge = source / "BepInEx/plugins/NOTestPilot/NOTestPilot.Bridge.dll"
        bridge.parent.mkdir(parents=True)
        bridge.write_bytes(b"fixture bridge")
        game = source / "NuclearOptionServer_Data/Managed/Assembly-CSharp.dll"
        game.parent.mkdir(parents=True)
        game.write_bytes(b"fixture game assembly")
        mod = root / "OwnMod.dll"
        mod.write_bytes(b"fixture mod; not a game binary")
        manifest = root / "mods.json"
        manifest.write_text(json.dumps({"version": 1, "files": [{"source": mod.name,
            "destination": "BepInEx/plugins/OwnMod/OwnMod.dll",
            "sha256": hashlib.sha256(mod.read_bytes()).hexdigest()}]}), encoding="utf-8")
        return source, manifest, mod

    def test_explicit_mod_is_server_only_and_report_omits_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, mod = self.fixture(root)
            inputs = load_server_inputs(manifest)
            lab = prepare_lab(source, root / "lab", 2, inputs)
            destination = Path(inputs[0]["destination"])
            self.assertEqual(mod.read_bytes(), (lab / "server" / destination).read_bytes())
            for client in ("client1", "client2"):
                self.assertFalse((lab / client / destination).exists())
            self.assertNotIn(str(root), json.dumps(describe_server_inputs(inputs)))
            prepare_lab(source, lab, 2, inputs)  # Reuse the same exact variant.
            (lab / "server" / destination).write_bytes(b"changed mod")
            with self.assertRaisesRegex(ValueError, "modified"):
                prepare_lab(source, lab, 2, inputs)

    def test_baseline_cannot_silently_change_variant_or_accept_unlisted_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, mod = self.fixture(root)
            lab = prepare_lab(source, root / "lab")
            inputs = load_server_inputs(manifest)
            with self.assertRaisesRegex(ValueError, "mods changed"):
                prepare_lab(source, lab, server_inputs=inputs)
            rogue = lab / "server/BepInEx/plugins/rogue.dll"
            rogue.write_bytes(b"unlisted")
            with self.assertRaisesRegex(ValueError, "Unlisted"):
                prepare_lab(source, lab)
            rogue.unlink()
            (lab / "client/BepInEx/plugins/rogue.dll").write_bytes(b"unlisted")
            with self.assertRaisesRegex(ValueError, "clients"):
                prepare_lab(source, lab)

    def test_sha_mismatch_traversal_bridge_and_loader_replacement_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, mod = self.fixture(root)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["files"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                load_server_inputs(manifest)
            data["files"][0]["sha256"] = hashlib.sha256(mod.read_bytes()).hexdigest()
            for destination in ("../outside.dll", "BepInEx/plugins/../core/loader.dll",
                    "BepInEx/plugins/NOTestPilot/bridge.dll", "BepInEx/plugins/notestpilot/bridge.dll",
                    "BepInEx/config/BepInEx.cfg", "BepInEx/config/BepInEx.CFG",
                    "BepInEx//plugins/OwnMod.dll", "C:/private.dll", "BepInEx\\plugins\\mod.dll"):
                with self.subTest(destination=destination):
                    data["files"][0]["destination"] = destination
                    manifest.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_server_inputs(manifest)


if __name__ == "__main__":
    unittest.main()
