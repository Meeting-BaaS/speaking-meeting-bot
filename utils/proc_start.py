"""Process start timestamp, taken before the expensive imports.

Import this first in a bot process. ``scripts/meetingbaas.py`` reports
spawn-to-ready latency against it, and everything imported after it (pipecat,
torch, silero, SDK clients) is the part worth measuring.
"""

import time

PROC_T0 = time.monotonic()
