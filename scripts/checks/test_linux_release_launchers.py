#!/usr/bin/env python3
"""Exercise release staging and installed launchers against packaging templates."""

import ast
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


def step_script(workflow, name):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    step = text.split(f"      - name: {name}\n", 1)[1].split("\n      - ", 1)[0]
    value = re.search(r"(?m)^        run: (.*)$", step)
    if value[1] == "|":
        return textwrap.dedent(step[value.end() + 1:])
    return value[1]


class LinuxReleaseLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pleazyng-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.packaging = self.root / "linux/packaging"
        shutil.copytree(ROOT / "linux/packaging", self.packaging)
        (self.root / "assets").mkdir()
        shutil.copy2(ROOT / "assets/pleazyng.png", self.root / "assets/pleazyng.png")
        shutil.copy2(ROOT / "LICENSE", self.root / "LICENSE")
        self.bundle = self.root / "build/linux/x64/release/bundle"
        (self.bundle / "lib").mkdir(parents=True)
        # A probe application records the wrapper's actual arguments and loader
        # environment; no Flutter or media runtime is substituted or qualified.
        executable = self.bundle / "pleazyng"
        executable.write_text(
            "#!/usr/bin/env python3\nimport json,os,sys\n"
            "print(json.dumps({'args':sys.argv[1:], 'libs':os.environ.get('LD_LIBRARY_PATH')}))\n"
        )
        executable.chmod(0o755)

    def run_script(self, script, **environment):
        return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                              cwd=self.root, env={**os.environ, **environment},
                              capture_output=True, text=True, check=True)

    def stage_release(self):
        self.run_script(step_script("build.yml", "Copy wrapper script into bundle"),
                        BUNDLE_DIR=str(self.bundle))

    def assert_launch(self, launcher, library_directory, **environment):
        self.assertTrue(os.access(launcher, os.X_OK), f"Launcher must be executable: {launcher}")
        result = subprocess.run([str(launcher), "a movie.mkv", "--title=a b"],
                                env={**os.environ, "LD_LIBRARY_PATH": "/existing", **environment},
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "args": ["a movie.mkv", "--title=a b"],
            "libs": f"{library_directory}:/existing",
        })

    def test_release_and_ci_stage_executable_portable_launcher(self):
        for workflow, step in (("build.yml", "Copy wrapper script into bundle"),
                               ("ci.yml", "Copy wrapper script into the bundle")):
            with self.subTest(workflow=workflow):
                self.run_script(step_script(workflow, step), BUNDLE_DIR=str(self.bundle))
                self.assert_launch(self.bundle / "pleazyng.sh", self.bundle / "lib")
                self.assertFalse((self.bundle / "plezy.sh").exists())

    def test_moved_bundle_relocates_im_cache_and_preserves_locale_records(self):
        self.stage_release()
        moved = self.root / 'portable "copy"'
        shutil.copytree(self.bundle, moved)
        modules = moved / 'lib/gtk-3.0/3.0.0/immodules'
        modules.mkdir(parents=True)
        module = modules / 'im-viqr.so'
        module.touch()
        cache = modules.parent / 'immodules.cache'
        metadata = '"viqr" "Vietnamese (VIQR)" "gtk30" "/usr/share/locale" "vi" '
        original = '"/builder/location/im-viqr.so" \n' + metadata + '\n'
        cache.write_text(original)
        (moved / 'pleazyng').write_text(
            '#!/usr/bin/env python3\nimport json,os,pathlib\n'
            'p=pathlib.Path(os.environ["GTK_IM_MODULE_FILE"])\n'
            'print(json.dumps({"path":str(p),"cache":p.read_text()}))\n')
        environment = {**os.environ, 'XDG_CACHE_HOME': str(self.root / 'cache')}
        result = subprocess.run([str(moved / 'pleazyng.sh')], env=environment,
                                check=True, capture_output=True, text=True)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['cache'], json.dumps(str(module)) + '\n' + metadata + '\n')
        self.assertEqual(cache.read_text(), original)
        self.assertEqual(len(list((self.root / 'cache/pleazyng/gtk-im').iterdir())), 1)
        # A missing module must not replace a usable cache or leave a temporary file.
        previous = Path(actual['path']).read_bytes()
        module.unlink()
        failed = subprocess.run([str(moved / 'pleazyng.sh')], env=environment,
                                capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(Path(actual['path']).read_bytes(), previous)
        self.assertEqual(len(list((self.root / 'cache/pleazyng/gtk-im').iterdir())), 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native GdkPixbuf requires Linux")
    def test_relocated_portable_and_read_only_appimage_decode_with_bundled_modules(self):
        # On Debian the native query lives outside PATH, next to the ABI directory.
        query = shutil.which("gdk-pixbuf-query-loaders")
        if not query:
            query = next(Path("/usr/lib").glob("*/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders"), None)
        if not query:
            self.skipTest("native gdk-pixbuf-query-loaders is required")
        system_modules = Path(query).parent / "2.10.0/loaders"
        if not system_modules.is_dir() and shutil.which("pkg-config"):
            result = subprocess.run(["pkg-config", "--variable=gdk_pixbuf_moduledir", "gdk-pixbuf-2.0"],
                                    capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                system_modules = Path(result.stdout.strip())
        modules = self.bundle / "lib/gdk-pixbuf-2.0/2.10.0/loaders"
        modules.mkdir(parents=True)
        for ext in ("svg", "gif"):
            native = next(system_modules.glob(f"*{ext}.so"), None)
            if native is None:
                self.skipTest(f"native {ext} loader is required")
            shutil.copy2(native, modules / native.name)
        subprocess.run([sys.executable, self.packaging / "rebuild-pixbuf-cache.py",
                        self.bundle, "--query-tool", query, "--runtime-prefix", "/opt/pleazyng"], check=True)
        self.stage_release()
        (self.bundle / "image.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"/>')
        (self.bundle / "image.gif").write_bytes(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="))
        (self.bundle / "pleazyng").write_text(
            "#!/usr/bin/env python3\nimport ctypes as c, os, pathlib\n"
            "if os.environ.get('VERIFY_READ_ONLY_MOUNT'):\n"
            "    assert os.statvfs(__file__).f_flag & os.ST_RDONLY\n"
            "lib = c.CDLL('libgdk_pixbuf-2.0.so.0')\n"
            "lib.gdk_pixbuf_new_from_file.argtypes = [c.c_char_p, c.c_void_p]\n"
            "lib.gdk_pixbuf_new_from_file.restype = c.c_void_p\n"
            "for name in ('image.svg', 'image.gif'):\n"
            "    assert lib.gdk_pixbuf_new_from_file(os.fsencode(pathlib.Path(__file__).with_name(name)), None), name\n"
            "print(os.environ['GDK_PIXBUF_MODULE_FILE'])\n")
        packaged_cache = modules.parent / "loaders.cache"
        original = packaged_cache.read_bytes()
        portable = self.root / 'portable "copy"'
        self.bundle.rename(portable)
        app = self.root / "mounted.AppDir"
        shutil.copytree(portable, app / "opt/pleazyng")
        shutil.copy2(self.packaging / "appimage/AppRun", app / "AppRun")
        (app / "AppRun").chmod(0o755)
        for path in [*app.rglob("*"), app]:
            path.chmod(path.stat().st_mode & ~0o222)

        environment = {**os.environ, "XDG_CACHE_HOME": str(self.root / "cache")}
        for entry, bundle in ((portable / "pleazyng.sh", portable), (app / "AppRun", app / "opt/pleazyng")):
            with self.subTest(entry=entry):
                command = [entry]
                env = dict(environment)
                if entry == app / "AppRun" and shutil.which("unshare") and shutil.which("mount"):
                    # The namespace dies with the probe. Environments that disallow
                    # unprivileged mounts still exercise the non-writable AppDir.
                    namespace = ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private"]
                    bind = 'mount --bind "$1" "$1" && mount -o remount,bind,ro "$1"'
                    if subprocess.run([*namespace, "sh", "-ec", bind, "sh", app], capture_output=True).returncode == 0:
                        command = [*namespace, "sh", "-ec", bind + ' && exec "$2"', "sh", app, entry]
                        env["VERIFY_READ_ONLY_MOUNT"] = "1"
                result = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
                relocated = Path(result.stdout.strip())
                self.assertTrue(relocated.is_relative_to(self.root / "cache"))
                self.assertEqual((bundle / packaged_cache.relative_to(self.bundle)).read_bytes(), original)
                for line in relocated.read_text().splitlines():
                    if line.startswith('"') and line.endswith('.so"'):
                        self.assertTrue(Path(ast.literal_eval(line)).is_relative_to(bundle))

    def test_empty_pixbuf_cache_is_rejected_before_startup(self):
        self.stage_release()
        modules = self.bundle / "lib/gdk-pixbuf-2.0/2.10.0/loaders"
        modules.mkdir(parents=True)
        (modules.parent / "loaders.cache").write_text("# no registered loaders\n")
        result = subprocess.run([self.bundle / "pleazyng.sh"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing module records", result.stderr)

    def test_appimage_entrypoint_executes_staged_bundle_launcher(self):
        self.stage_release()
        app = self.root / "Pleazyng.AppDir"
        shutil.copytree(self.bundle, app / "opt/pleazyng")
        shutil.copy2(self.packaging / "appimage/AppRun", app / "AppRun")
        (app / "AppRun").chmod(0o755)
        self.assert_launch(app / "AppRun", app / "opt/pleazyng/lib", APPDIR=str(app))

    def test_snap_staging_satisfies_actual_override_and_app_command(self):
        self.stage_release()
        self.run_script(step_script("build.yml", "Prepare Snap package inputs"),
                        BUNDLE_DIR=str(self.bundle), RELEASE_TAG="3.0.2")
        project = self.packaging / "snap"
        manifest = (project / "snapcraft.yaml").read_text()
        override = textwrap.dedent(manifest.split("    override-build: |\n", 1)[1]
                                   .split("    stage-packages:", 1)[0])
        installed = self.root / "snap-install"
        triplet = "x86_64-linux-gnu"
        libraries = ("libssh.so.4", "libssh-gcrypt.so.4")
        for soname in libraries:
            (project / "src/lib" / soname).write_bytes(b"old")
            staged = installed / "usr/lib" / triplet / soname
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(b"security update")
        self.run_script(override, CRAFT_PROJECT_DIR=str(project),
                        CRAFT_PART_SRC=str(project / "src"), CRAFT_PART_INSTALL=str(installed),
                        CRAFT_ARCH_TRIPLET_BUILD_FOR=triplet)
        for soname in libraries:
            runtime_lib = installed / "opt/pleazyng/lib" / soname
            self.assertTrue(runtime_lib.samefile(installed / "usr/lib" / triplet / soname))
        command = re.search(r"(?m)^    command: (.+)$", manifest)[1]
        self.assert_launch(installed / command, installed / "opt/pleazyng/lib")
        self.assertTrue((installed / "usr/share/applications/org.snapetech.pleazyng.desktop").is_file())

    @unittest.skipUnless(shutil.which("rpmbuild"), "rpmbuild is required for the source-RPM check")
    def test_copr_staging_supplies_every_declared_source(self):
        runner = self.root / "runner"
        assets = runner / "pleazyng-copr/assets"
        assets.mkdir(parents=True)
        for arch in ("x64", "arm64"):
            shutil.make_archive(str(assets / f"pleazyng-linux-{arch}"), "gztar", self.bundle)
        # This test checks SRPM input staging, not downloader integrity. Keep it
        # offline; exact helper-package verification has its own native checks.
        (self.packaging / "fetch-webkit-runtime.py").write_text(
            "import argparse\nfrom pathlib import Path\np=argparse.ArgumentParser()\n"
            "p.add_argument('--output',type=Path,required=True)\na=p.parse_args()\n"
            "for arch in ('x64','arm64'): (a.output/f'webkit-{arch}.deb').write_bytes(b'fixture')\n"
        )
        self.run_script(step_script("publish-copr.yml", "Build source RPM"),
                        RUNNER_TEMP=str(runner), RELEASE_TAG="v3.0.2", RELEASE_VERSION="3.0.2")
        top = runner / "pleazyng-rpmbuild"
        spec = (top / "SPECS/pleazyng.spec").read_text()
        for source in re.findall(r"(?m)^Source\d+:\s+(\S+)$", spec):
            self.assertTrue((top / "SOURCES" / source).is_file(), f"Missing RPM source: {source}")
        self.assertEqual(len(list((top / "SRPMS").glob("*.src.rpm"))), 1)

    @unittest.skipUnless(shutil.which("cc"), "cc is required for ELF dependency checks")
    def test_flatpak_runtime_check_resolves_app_and_converter_libraries(self):
        # Each probe needs a library available only inside its own closure.
        # Execute the production CHECK with only its /app mount relocated into
        # the fixture, so stale product paths or LD_LIBRARY_PATH cannot pass.
        library = self.root / "probe.c"
        library.write_text("int probe(void) { return 42; }\n")
        program = self.root / "main.c"
        program.write_text("int probe(void); int main(void) { return probe() != 42; }\n")
        subprocess.run(["cc", "-shared", "-fPIC", str(library), "-o",
                        str(self.bundle / "lib/libapp_probe.so")], check=True)
        subprocess.run(["cc", str(program), f"-L{self.bundle / 'lib'}", "-lapp_probe",
                        "-o", str(self.bundle / "pleazyng")], check=True)
        tools = self.bundle / "tools/dv"
        (tools / "lib/runtime").mkdir(parents=True)
        (tools / "libexec").mkdir()
        subprocess.run(["cc", "-c", str(library), "-o", str(tools / "lib/python.o")], check=True)
        subprocess.run(["cc", "-shared", "-fPIC", str(library), "-o",
                        str(tools / "lib/runtime/libconverter_probe.so")], check=True)
        subprocess.run(["cc", str(program), f"-L{tools / 'lib/runtime'}", "-lconverter_probe",
                        "-o", str(tools / "libexec/converter")], check=True)
        launcher = tools / "plezyng-dv-convert"
        launcher.write_text('#!/usr/bin/env bash\nroot="$(dirname "$0")"\n'
                            'export LD_LIBRARY_PATH="$root/lib/runtime"\n'
                            'exec "$root/libexec/converter" "$@"\n')
        launcher.chmod(0o755)
        self.stage_release()
        entry = self.root / "bin/pleazyng"
        entry.parent.mkdir()
        entry.symlink_to(self.bundle / "pleazyng.sh")
        module = ast.parse((self.packaging / "check-flatpak.py").read_text())
        check = next(ast.literal_eval(n.value) for n in module.body
                     if isinstance(n, ast.Assign) and any(
                         isinstance(t, ast.Name) and t.id == "CHECK" for t in n.targets))
        check = check.replace("/app/bin/pleazyng", str(entry))
        check = check.replace("/app/pleazyng", str(self.bundle))
        result = self.run_script(check)
        self.assertIn("Resolved all dependencies for 4 ELF objects", result.stdout)


if __name__ == "__main__":
    unittest.main()
