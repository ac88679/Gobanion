"""
Test if asyncio subprocess works on this system.
"""
import asyncio
import sys
import os

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

async def test():
    print(f"Python: {sys.executable}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('hello from subprocess')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    print(f"Proc started: {proc.pid}", flush=True)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    print(f"stdout: {stdout.decode()}", flush=True)
    print(f"stderr: {stderr.decode()}", flush=True)
    print(f"retcode: {proc.returncode}", flush=True)

asyncio.run(test())
