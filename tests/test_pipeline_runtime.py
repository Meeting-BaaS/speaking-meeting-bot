import os
import subprocess
import sys
import unittest


class PipelineRuntimeTest(unittest.TestCase):
    def test_worker_is_registered_before_runner_starts(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio\n"
                "from scripts import meetingbaas as m\n"
                "async def check():\n"
                "    pipeline = m.Pipeline([m.FrameProcessor()])\n"
                "    worker, runner = await m.build_pipeline_runner(pipeline)\n"
                "    assert runner.get_worker(worker.name) is worker\n"
                "    print('registered')\n"
                "asyncio.run(check())\n",
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip().splitlines()[-1], "registered")
