"""The image `service deploy --target k8s` runs (Kamino item 55).

The manifests named marker-service:latest with imagePullPolicy IfNotPresent,
and the image reaches the cluster by hand. After a rebuild nothing in the pod
spec changed, so nothing rolled out and a node with the old :latest kept it.
Now each build is tagged with its image ID and deploy applies the manifests
pinned to that tag. No cluster and no docker: both are replaced, and what
would be handed to `kubectl apply` is captured.

Run: python3 -m unittest discover -s tests   (stdlib only)
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pdroid import service  # noqa: E402

ID = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TAG = "marker-service:0123456789ab"
MANIFESTS = ("api.yaml", "worker.yaml", "batch-job.yaml")


def docker_inspect(image_id=ID, rc=0):
    def run(cmd, **kw):
        assert cmd[:3] == ["docker", "image", "inspect"], cmd
        return subprocess.CompletedProcess(cmd, rc, stdout=image_id + "\n", stderr="")
    return run


class ImageTag(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(service.shutil, "which", lambda name: f"/usr/bin/{name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_tag_is_the_image_id(self):
        with mock.patch.object(service.subprocess, "run", docker_inspect()):
            self.assertEqual(service.image_tag(), TAG)

    def test_no_image_no_tag(self):
        with mock.patch.object(service.subprocess, "run", docker_inspect("", rc=1)):
            self.assertIsNone(service.image_tag())

    def test_no_docker_no_tag(self):
        with mock.patch.object(service.shutil, "which", lambda name: None):
            self.assertIsNone(service.image_tag())

    def test_something_that_is_not_an_id_is_not_a_tag(self):
        with mock.patch.object(service.subprocess, "run", docker_inspect("<no value>")):
            self.assertIsNone(service.image_tag())


class Render(unittest.TestCase):
    def path(self, name):
        return os.path.join(service.k8s_dir(), name)

    def test_every_manifest_that_runs_the_image_is_pinned(self):
        for name in MANIFESTS:
            text = service.render_manifest(self.path(name), TAG)
            self.assertIn(f"image: {TAG}", text, msg=name)
            self.assertNotIn("image: marker-service:latest", text, msg=name)

    def test_pinned_with_ifnotpresent_which_is_now_right(self):
        # An immutable tag never needs a second pull; IfNotPresent stays.
        for name in MANIFESTS:
            self.assertIn("imagePullPolicy: IfNotPresent", service.render_manifest(self.path(name), TAG))

    def test_without_a_tag_the_manifest_is_unchanged(self):
        for name in MANIFESTS:
            with open(self.path(name), encoding="utf-8") as fh:
                self.assertEqual(service.render_manifest(self.path(name), None), fh.read())


class Deploy(unittest.TestCase):
    """deploy --target k8s, with kubectl and docker replaced."""

    def setUp(self):
        self.applied, self.messages = [], []
        patches = [
            mock.patch.object(service, "need_kubectl", lambda: None),
            mock.patch.object(service, "redis_secret_k8s", lambda: None),
            mock.patch.object(service, "env_load", lambda: None),
            mock.patch.object(service, "render_pvcs", lambda: "kind: PersistentVolumeClaim\n"),
            mock.patch.object(service, "_quiet", lambda cmd: True),           # the image exists
            mock.patch.object(service.c, "run", lambda cmd: 0),
            mock.patch.object(service.subprocess, "run", self.kubectl_apply),
            mock.patch.object(service, "success", self.messages.append),
            mock.patch.object(service, "info", self.messages.append),
            mock.patch.object(service, "warn", lambda m: self.messages.append("WARN " + m)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def kubectl_apply(self, cmd, input=None, **kw):
        if cmd[:3] == ["kubectl", "apply", "-f"] and cmd[3] == "-":
            self.applied.append(input)
        return subprocess.CompletedProcess(cmd, 0)

    def deploy(self, tag):
        with mock.patch.object(service, "tag_build", lambda: tag):
            service.deploy("k8s", "", "")
        return [t for t in self.applied if "kind: Deployment" in t]

    def test_a_build_is_deployed_by_its_id(self):
        deployments = self.deploy(TAG)
        self.assertEqual(len(deployments), 2)                       # api and worker
        for text in deployments:
            self.assertIn(f"image: {TAG}", text)
            self.assertNotIn("image: marker-service:latest", text)
        self.assertTrue(any(f"kind load docker-image {TAG}" in m for m in self.messages))

    def test_a_rebuild_is_a_new_spec(self):
        first = self.deploy("marker-service:aaaaaaaaaaaa")
        second = self.deploy("marker-service:bbbbbbbbbbbb")
        self.assertNotEqual(first, second[-2:])

    def test_without_an_id_it_says_how_to_roll_by_hand(self):
        deployments = self.deploy(None)
        self.assertTrue(all("image: marker-service:latest" in t for t in deployments))
        self.assertTrue(any(m.startswith("WARN") and "rollout restart" in m for m in self.messages))


if __name__ == "__main__":
    unittest.main()
