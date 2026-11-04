"""Run native packaging tools against inert fixtures; never installs a client."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import installer
import repack_deb


@unittest.skipUnless(os.name == 'posix' and shutil.which('dpkg-deb'), 'Linux packaging tools required')
class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='geodomdesk-package-test-')
        self.root = Path(self.temp.name)
        self.config = {'host':'www.qcjcpt.com', 'relay':'', 'api':'', 'websocket_port':21114,
                       'key':'AKJROlsG4WAI103ADqoVrBs4Nb7+6dpbHYVO+MVd81Y='}

    def tearDown(self):
        self.temp.cleanup()

    def stub_deb(self):
        package = self.root/'input'
        (package/'DEBIAN').mkdir(parents=True)
        (package/'DEBIAN/control').write_text('Package: rustdesk\nVersion: 1.5.2\nArchitecture: amd64\nMaintainer: Fixture <fixture@example.com>\nDescription: Inert packaging test\nConflicts: rustdesk-other\n')
        original = package/'usr/share/rustdesk/rustdesk'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'#!/bin/sh\nexit 99\n')
        original.chmod(0o755)
        (package/'DEBIAN/preinst').write_text('#!/bin/sh\n# Must be discarded; never executed.\nservice rustdesk stop\n')
        (package/'DEBIAN/preinst').chmod(0o755)
        payload = self.root/'input.deb'
        subprocess.run(['dpkg-deb','--root-owner-group','-b',str(package),str(payload)],check=True,capture_output=True)
        return payload

    def test_deb_isolated_paths_and_service_scripts(self):
        from publisher import check_deb
        payload = self.stub_deb()
        output = self.root/'geodomdesk.deb'
        repack_deb.repack(payload,output,'1.5.2')
        check_deb(output,'1.5.2')
        extracted = self.root/'unpacked'
        subprocess.run(['dpkg-deb','-R',str(output),str(extracted)],check=True)
        self.assertEqual(os.readlink(extracted/'usr/bin/geodomdesk'),'/usr/share/geodomdesk/geodomdesk')
        self.assertFalse((extracted/'usr/bin/rustdesk').exists())
        self.assertFalse((extracted/'DEBIAN/preinst').exists())
        fakebin = self.root/'fakebin'
        fakebin.mkdir()
        log = self.root/'systemctl.log'
        fake = fakebin/'systemctl'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"\n')
        fake.chmod(0o755)
        env = {**os.environ,'PATH':str(fakebin)+':'+os.environ['PATH'],'TEST_SYSTEMCTL_LOG':str(log)}
        subprocess.run(['sh',str(extracted/'DEBIAN/postinst'),'configure'],check=True,env=env)
        subprocess.run(['sh',str(extracted/'DEBIAN/prerm'),'remove'],check=True,env=env)
        self.assertEqual(log.read_text().splitlines(),['daemon-reload','enable --now geodomdesk.service','stop geodomdesk.service','disable geodomdesk.service'])

    def test_linux_wrapper_embedded_package_verification(self):
        payload = self.stub_deb()
        output = self.root/'geodomdesk-ubuntu.run'
        asset = {'digest':'sha256:'+installer.sha(payload)}
        installer.make_linux(payload,asset,'deb','x86_64',self.config,'1.5.2',output)
        result = subprocess.run(['sh',str(output),'--check'],capture_output=True,text=True,check=True)
        self.assertIn('Verified GeodomDesk',result.stdout)
        data=bytearray(output.read_bytes())
        data[-1] ^= 1
        output.write_bytes(data)
        self.assertNotEqual(subprocess.run(['sh',str(output),'--check'],capture_output=True).returncode,0)

    @unittest.skipUnless(shutil.which('makensis') and shutil.which('7z'), 'NSIS and 7z required')
    def test_windows_nsis_embeds_only_the_expected_payload(self):
        payload=self.root/'GeodomDesk.exe'
        payload.write_bytes(b'MZ'+b'inert fixture '*100)
        output=self.root/'geodomdesk-setup.exe'
        license_file=Path(__file__).parent/'LICENSE.client'
        asset={'digest':'sha256:'+installer.sha(payload)}
        installer.make_windows(payload,asset,self.config,'1.5.2',self.root,license_file,output)
        self.assertTrue(output.read_bytes().startswith(b'MZ'))
        unpacked=self.root/'verify-nsis'
        subprocess.run(['7z','x','-y','-o'+str(unpacked),str(output)],check=True,capture_output=True)
        embedded=list(unpacked.rglob('geodomdesk-client.exe'))
        self.assertEqual(len(embedded),1)
        self.assertEqual(installer.sha(embedded[0]),installer.sha(payload))


if __name__=='__main__':
    unittest.main()
