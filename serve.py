import os, subprocess, sys

port = os.environ.get("PORT", "8080")
subprocess.run([sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", port])
