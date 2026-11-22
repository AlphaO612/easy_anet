import hashlib
import io
import tarfile
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle


class BundleTests(unittest.TestCase):
    def test_integrity_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "images.tar"
            config = b'{"architecture":"amd64","os":"linux"}'
            with tarfile.open(archive, "w") as tar:
                for name, data in [("config.json",config),("manifest.json",b'[{"Config":"config.json","Layers":[]}]')]:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            original = archive.read_bytes()
            image = "sha256:" + hashlib.sha256(config).hexdigest()
            images = dict(SERVER_IMAGE=image, NETNS_IMAGE=image)
            manifest = dict(format=1, platform="linux/amd64", archive="images.tar",
                            sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), images=images)
            (root/"manifest.json").write_text(json.dumps(manifest))
            with patch.object(bundle,"import_images") as loader, patch.object(bundle,"identity",return_value=image):
                self.assertEqual(bundle.load_bundle(root,"standalone"),images)
                self.assertEqual(loader.call_args.args[2],{image})
                with self.assertRaises(ValueError):
                    bundle.load_bundle(root,"anet-easy")
                images["AUTH_IMAGE"] = image
                (root/"manifest.json").write_text(json.dumps(manifest))
                self.assertEqual(set(bundle.load_bundle(root, "standalone")), {"NETNS_IMAGE", "SERVER_IMAGE"})
                del images["AUTH_IMAGE"]
                archive.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError,"SHA-256"):
                    bundle.load_bundle(root,"standalone")
                self.assertEqual(loader.call_count,2)
            archive.write_bytes(original)
            images["SERVER_IMAGE"] = "sha256:" + "b"*64
            (root/"manifest.json").write_text(json.dumps(manifest))
            with patch.object(bundle,"import_images") as loader:
                with self.assertRaisesRegex(ValueError,"missing from the archive"):
                    bundle.load_bundle(root,"standalone")
                loader.assert_not_called()
            images["SERVER_IMAGE"] = image
            (root/"manifest.json").write_text(json.dumps(manifest))
            runtime = "sha256:" + "c" * 64
            with patch.object(bundle,"import_images"), patch.object(bundle,"identity",return_value=runtime):
                self.assertEqual(bundle.load_bundle(root,"standalone"),dict.fromkeys(images,runtime))

    def test_optional_dns_image_for_full_profile(self):
        from release import resolve_images, SERVICES
        image = "registry.example/image@sha256:" + "a" * 64
        manifest = dict(platforms=["linux/amd64"], images=dict.fromkeys(SERVICES["anet-easy"], image))
        self.assertNotIn("DNS_IMAGE", resolve_images(manifest, "anet-easy", "linux/amd64"))
        manifest["images"]["DNS_IMAGE"] = image
        self.assertEqual(resolve_images(manifest, "anet-easy", "linux/amd64")["DNS_IMAGE"], image)
        self.assertNotIn("DNS_IMAGE", resolve_images(manifest, "webui", "linux/amd64"))

    def test_oci_descriptor_resolves_to_config(self):
        config = "sha256:" + "a"*64
        manifest = json.dumps({"config":{"digest":config}}).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"manifests":[{"digest":digest,"platform":{"os":"linux","architecture":"amd64"}}]}).encode()
        idx = "sha256:" + hashlib.sha256(index).hexdigest()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw,mode="w") as archive:
            for name, data in ((digest,manifest),(idx,index)):
                entry=tarfile.TarInfo("blobs/sha256/"+name.split(":")[1])
                entry.size=len(data)
                archive.addfile(entry,io.BytesIO(data))
        raw.seek(0)
        with tarfile.open(fileobj=raw) as archive:
            self.assertEqual(bundle.saved_config(archive,{config:{}},idx),config)
