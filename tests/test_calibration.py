import os
import select
import signal
import subprocess
import sys


def test_ctrl_c_interrupts_real_prompt_and_disconnects_once(tmp_path):
    marker = tmp_path / "disconnects"
    script = """
from pathlib import Path
import sys
import lerobot_webrtc.cli as cli

marker = Path(sys.argv.pop())
class FakeArm:
    def __init__(self, *args):
        self.exit_mode = args[-1]
    def calibrate(self):
        print('CALIBRATION PROMPT', flush=True)
        input('Press enter to continue: ')
    def disconnect(self):
        with marker.open('a') as output:
            output.write(self.exit_mode + '\\n')
cli.LeRobotArm = FakeArm
cli.main()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            "calibrate",
            "--role",
            "follower",
            "--port",
            "fake",
            "--id",
            "fake",
            str(marker),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "calibration did not reach its input prompt"
        assert "CALIBRATION PROMPT" in process.stdout.readline()
        os.kill(process.pid, signal.SIGINT)
        process.wait(timeout=5)
        assert process.returncode == 0
        assert marker.read_text() == "torque-off\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
