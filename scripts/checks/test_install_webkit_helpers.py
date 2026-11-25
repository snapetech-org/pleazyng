"""Verify paired-runtime integrity boundaries and complete helper installation."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('webkit_helpers', ROOT/'linux/packaging/install-webkit-helpers.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
fetch_spec = importlib.util.spec_from_file_location('webkit_fetch', ROOT/'linux/packaging/fetch-webkit-runtime.py')
fetcher = importlib.util.module_from_spec(fetch_spec)
fetch_spec.loader.exec_module(fetcher)


class WebkitFetchTests(unittest.TestCase):
    def test_ubuntu_snapshot_recovers_transport_failure_but_rejects_bad_bytes(self):
        primary = 'https://ports.ubuntu.com/fixture.deb'
        snapshot = 'https://snapshot.ubuntu.com/fixture.deb'
        data = b'verified fixture package'
        source = {'url': primary, 'snapshot_url': snapshot, 'sha256': hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / 'webkit-arm64.deb'
            error = HTTPError(primary, 403, 'Forbidden', {}, None)
            with patch.object(fetcher.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(data)]) as download:
                fetcher.fetch_helper(package, 'arm64', source)
                self.assertEqual([call.args[0] for call in download.call_args_list], [primary, snapshot])
            self.assertEqual(package.read_bytes(), data)
            package.unlink()
            with patch.object(fetcher.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong bytes')) as download:
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    fetcher.fetch_helper(package, 'arm64', source)
                download.assert_called_once_with(primary, timeout=60)
            self.assertEqual(list(Path(temporary).iterdir()), [])


@unittest.skipUnless(all(shutil.which(x) for x in ('ar', 'tar', 'zstd')), 'archive tools required')
class WebkitHelperTests(unittest.TestCase):
    def test_complete_pair_is_installed_and_mismatches_write_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root/'bundle'
            (bundle/'lib').mkdir(parents=True)
            library = b'fixture library identity'
            (bundle/'lib/libwebkit2gtk-4.1.so.0').write_bytes(library)
            tree = root/'tree'
            libdir = tree/'usr/lib/x86_64-linux-gnu'
            directory = libdir/'webkit2gtk-4.1'
            (directory/'injected-bundle').mkdir(parents=True)
            (libdir/'libwebkit2gtk-4.1.so.0.123').write_bytes(library)
            for name in ('WebKitWebProcess','WebKitNetworkProcess','WebKitGPUProcess',
                         'injected-bundle/libwebkit2gtkinjectedbundle.so'):
                p=directory/name
                p.write_bytes(name.encode())
                p.chmod(0o755)
            copyright = tree/'usr/share/doc/libwebkit2gtk-4.1-0/copyright'
            copyright.parent.mkdir(parents=True)
            copyright.write_text('Fixture license')
            subprocess.run(['tar','--zstd','-cf',str(root/'data.tar.zst'),'-C',str(tree),'.'],check=True)
            subprocess.run(['ar','rc',str(root/'runtime.deb'),'data.tar.zst'],cwd=root,check=True)
            metadata={'x64':{'version':'fixture','sha256':helpers.digest(root/'runtime.deb'),
                             'library_sha256':hashlib.sha256(library).hexdigest(),'triplet':'x86_64-linux-gnu'}}
            (root/'metadata.json').write_text(json.dumps(metadata))
            helpers.install(bundle,root/'runtime.deb',root/'stage',root/'metadata.json','x64')
            receipt=json.loads((root/'stage/usr/share/pleazyng/webkit-runtime.json').read_text())
            self.assertEqual(len(receipt['installed_files']),4)
            self.assertEqual((root/'stage/usr/share/licenses/pleazyng/webkit-copyright').read_text(),'Fixture license')
            self.assertTrue((root/'stage/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1/WebKitWebProcess').stat().st_mode & 0o111)
            (bundle/'lib/libwebkit2gtk-4.1.so.0').write_bytes(b'different build')
            with self.assertRaisesRegex(ValueError,'does not match'):
                helpers.install(bundle,root/'runtime.deb',root/'rejected',root/'metadata.json','x64')
            self.assertFalse((root/'rejected').exists())
            with self.assertRaisesRegex(ValueError,'staging root'):
                helpers.install(bundle,root/'runtime.deb',Path('/'),root/'metadata.json','x64')


if __name__ == '__main__':
    unittest.main()
