import tempfile, threading, http.server, socketserver, hashlib, time, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import modpacker

# Start a tiny HTTP server serving a few files for download tests

def test_http_download_hash_and_skip(tmp_path=None):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Create a temp http server
        serve_dir = td / "serve"
        serve_dir.mkdir()
        content = b"hello modpacker test content " * 100
        (serve_dir / "file.jar").write_bytes(content)
        sha512 = hashlib.sha512(content).hexdigest()
        sha1 = hashlib.sha1(content).hexdigest()

        # start server
        orig_dir = Path.cwd()
        import os
        os.chdir(serve_dir)
        handler = http.server.SimpleHTTPRequestHandler
        # find free port
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.2)
            url = f"http://127.0.0.1:{port}/file.jar"
            dest = td / "dest" / "file.jar"
            # first download
            modpacker.http_download(url, dest, expected_hashes={"sha512": sha512})
            assert dest.exists()
            assert dest.read_bytes() == content
            # second download should skip via hash check but still succeed (file exists and hash matches)
            mtime_before = dest.stat().st_mtime
            time.sleep(0.01)
            modpacker.http_download(url, dest, expected_hashes={"sha512": sha512})
            # file should still exist (may be skipped)
            assert dest.exists()
            # hash mismatch should raise
            try:
                modpacker.http_download(url, dest.with_name("bad.jar"), expected_hashes={"sha512": "0"*128})
                assert False, "should raise HashMismatchError"
            except modpacker.HashMismatchError:
                pass
            # also test DownloadManager with local url
            dest2 = td / "dest2" / "file2.jar"
            task = modpacker.DownloadTask(url=url, dest=dest2, hashes={"sha512": sha512}, size=len(content), path="mods/file2.jar")
            task.downloads = [url]
            mgr = modpacker.DownloadManager(concurrency=2)
            completed, total, failed = mgr.download_all([task])
            assert total == 1 and completed == 1 and not failed
            assert dest2.read_bytes() == content
            httpd.shutdown()
        os.chdir(orig_dir)

def test_concurrent_downloads(tmp_path=None):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        serve_dir = td / "serve"
        serve_dir.mkdir()
        for i in range(5):
            (serve_dir / f"f{i}.jar").write_bytes(f"content{i}".encode()*1000)

        import os
        os.chdir(serve_dir)
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.2)
            tasks = []
            for i in range(5):
                dest = td / "out" / f"f{i}.jar"
                content = f"content{i}".encode()*1000
                sha512 = hashlib.sha512(content).hexdigest()
                url = f"http://127.0.0.1:{port}/f{i}.jar"
                task = modpacker.DownloadTask(url=url, dest=dest, hashes={"sha512": sha512}, size=len(content), path=f"mods/f{i}.jar")
                task.downloads=[url]
                tasks.append(task)
            mgr = modpacker.DownloadManager(concurrency=3)
            c,t,f = mgr.download_all(tasks, progress_cb=lambda a,b,c: None)
            assert c==5 and not f
            httpd.shutdown()
        os.chdir(Path(td).parent)
