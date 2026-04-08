"""
Launcher — runs one or two bot instances and streams their output.
"""
import os
import sys
import asyncio
import subprocess

TOKEN1 = os.getenv("DISCORD_BOT_TOKEN", "")
TOKEN2 = os.getenv("DISCORD_BOT_TOKEN_2", "")

async def stream(proc, label):
    """Stream subprocess output line by line with a prefix label."""
    async for line in proc.stdout:
        text = line.decode(errors="replace").rstrip()
        print(f"{label} {text}", flush=True)

async def main():
    procs = []
    tasks = []

    base = [sys.executable, "-u", "bot.py"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    if TOKEN2:
        print("🔀 Dual-bot mode — launching Lana Voice 1 (?) and Lana Voice 2 (.)", flush=True)
        p1 = await asyncio.create_subprocess_exec(
            *base, "--token-env", "DISCORD_BOT_TOKEN", "--prefix", "?", "--keepalive",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
        )
        p2 = await asyncio.create_subprocess_exec(
            *base, "--token-env", "DISCORD_BOT_TOKEN_2", "--prefix", ".",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
        )
        procs = [p1, p2]
        tasks = [
            asyncio.create_task(stream(p1, "[Voice1]")),
            asyncio.create_task(stream(p2, "[Voice2]")),
        ]
        print(f"✅ Lana Voice 1 (PID {p1.pid}) — prefix: ?", flush=True)
        print(f"✅ Lana Voice 2 (PID {p2.pid}) — prefix: .", flush=True)
    else:
        print("💡 Tip: Set DISCORD_BOT_TOKEN_2 to run a second clone!", flush=True)
        p1 = await asyncio.create_subprocess_exec(
            *base, "--keepalive",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
        )
        procs = [p1]
        tasks = [asyncio.create_task(stream(p1, "[Bot]"))]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(main())
