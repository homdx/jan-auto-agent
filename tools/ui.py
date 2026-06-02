import sys
import time

class Spinner:
    def __init__(self, message: str = "Processing"):
        self.message = message
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.idx = 0
        self.running = False

    def spin(self):
        sys.stdout.write(f"\r{self.frames[self.idx % len(self.frames)]} {self.message}...")
        sys.stdout.flush()
        self.idx += 1