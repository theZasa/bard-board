"""Creates the sounds the end-to-end test expects (pure tones, so the test can recognise them by frequency).

Usage: python tools/make_test_sounds.py <folder>
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def main(target: Path) -> None:
    (target / "Memes").mkdir(parents=True, exist_ok=True)
    # 440 Hz, 2 s
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ac", "1", str(target / "beep.mp3"))
    # 880 Hz, 1.5 s, umlaut in the name on purpose
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=1.5", "-ac", "1", str(target / "alter käse.wav"))
    # 0.5 s silence, then 660 Hz for 1 s: checks that leading silence is trimmed
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.5", "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
           "-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-ac", "1", "-c:a", "libvorbis",
           str(target / "Memes" / "tada.ogg"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
