import os
import sys
import subprocess

project_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_dir)

print("Starting Equb Management System...")
print("Open your browser at: http://localhost:8000")
print("Press Ctrl+C to stop.\n")

subprocess.run([
    sys.executable, "-m", "uvicorn", "main:app",
    "--host", "0.0.0.0",
    "--port", "8000",
    "--reload",
    "--reload-dir", project_dir,
])
