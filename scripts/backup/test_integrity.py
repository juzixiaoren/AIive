import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("integrity.py")
class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("audit.json", "database.dump", "metadata.txt", "database-version.txt", "config/.env", "config/docker-compose.yml", "config/backend/Dockerfile", "config/deploy/pg/Dockerfile"):
            p=self.root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b"fixture")
        with tarfile.open(self.root/"data.tar","w") as t:
            entry=tarfile.TarInfo("auth_secret"); entry.size=6; t.addfile(entry,io.BytesIO(b"secret"))
        self.run_check("create", True)
    def run_check(self, mode, success):
        result=subprocess.run([sys.executable,str(SCRIPT),mode,str(self.root)],capture_output=True)
        self.assertEqual(result.returncode==0,success,result.stderr.decode())
    def test_valid(self): self.run_check("verify",True)
    def test_corrupt_database(self):
        (self.root/"database.dump").write_bytes(b"bad"); self.run_check("verify",False)
    def test_missing_config(self):
        (self.root/"config/.env").unlink(); self.run_check("verify",False)
    def test_extra_file(self):
        (self.root/"extra").write_bytes(b"bad"); self.run_check("verify",False)
    def test_path_traversal(self):
        with tarfile.open(self.root/"data.tar","w") as t:
            t.addfile(tarfile.TarInfo("../../escape"))
        self.run_check("create",False)
    def test_absolute_symlink(self):
        with tarfile.open(self.root/"data.tar","w") as t:
            e=tarfile.TarInfo("escape"); e.type=tarfile.SYMTYPE; e.linkname="/etc/passwd"; t.addfile(e)
        self.run_check("create",False)
if __name__ == "__main__": unittest.main()
