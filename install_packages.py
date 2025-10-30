import sys
import subprocess

if __name__ == "__main__":
    target_dir = sys.argv[1]
    requirements_file = sys.argv[2]
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", requirements_file, "--target", target_dir])
