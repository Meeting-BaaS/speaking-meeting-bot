"""Compatibility shim for the local protobuf module.

Pipecat 1.0.0 ships its own generated `frames_pb2` that is the canonical
schema for the runtime serializer. Re-export it here so any legacy imports
stay aligned with the installed Pipecat version and don't register a stale
descriptor into the global protobuf pool.
"""

from pipecat.frames.protobufs.frames_pb2 import *  # noqa: F401,F403
