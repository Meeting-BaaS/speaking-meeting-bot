import asyncio
import sys
import websockets
import protobufs.frames_pb2 as frames_pb2
from google.protobuf.message import EncodeError
from websockets.exceptions import ConnectionClosedError
from loguru import logger
from .runner import configure

# Setup Loguru logger
logger.remove()
logger.add(sys.stderr, level="INFO")


async def handle_pipecat_messages(pipecat_ws, client_ws):
  """Handle messages coming from Pipecat back to the client"""
  try:
    async for message in pipecat_ws:
      if isinstance(message, bytes):
        try:
          frame = frames_pb2.Frame()
          frame.ParseFromString(message)
          if frame.HasField("audio"):
            audio_data = frame.audio.audio
            await client_ws.send(bytes(audio_data))
            logger.debug("Forwarded audio response to client")
        except Exception as e:
          logger.error(f"Error processing Pipecat response: {str(e)}")
          logger.exception(e)
  except Exception as e:
    logger.error(f"Error in Pipecat message handler: {str(e)}")
    logger.exception(e)


async def forward_audio(websocket, websocket_url, sample_rate, channels):
  try:
    async with websockets.connect(websocket_url) as pipecat_ws:
      logger.debug("Connected to Pipecat WebSocket")

      pipecat_handler = asyncio.create_task(
        handle_pipecat_messages(pipecat_ws, websocket)
      )

      try:
        async for message in websocket:
          if isinstance(message, bytes):
            try:
              frame = frames_pb2.Frame()
              frame.audio.audio = message
              frame.audio.sample_rate = sample_rate
              frame.audio.num_channels = channels

              serialized_frame = frame.SerializeToString()
              await pipecat_ws.send(serialized_frame)
              logger.debug("Successfully forwarded audio frame to Pipecat")
            except Exception as e:
              logger.error(f"Error processing client frame: {str(e)}")
              logger.exception(e)
          else:
            logger.info(f"Received non-bytes message: {message}, ignoring")
      except Exception as e:
        logger.error(f"Error in client message handler: {str(e)}")
        logger.exception(e)
      finally:
        pipecat_handler.cancel()
        try:
          await pipecat_handler
        except asyncio.CancelledError:
          pass
  except ConnectionClosedError as e:
    logger.warning(f"Connection to Pipecat WebSocket closed: {e}")
  except Exception as e:
    logger.error(f"Unexpected error: {str(e)}")
    logger.exception(e)
  finally:
    try:
      await websocket.close()
    except:
      pass


async def main():
  host, port, websocket_url, sample_rate, channels, args = await configure()

  server = await websockets.serve(
    lambda ws: forward_audio(ws, websocket_url, sample_rate, channels), host, port
  )
  logger.info(f"WebSocket server started on ws://{host}:{port}")

  try:
    await asyncio.Future()  # Keep server running
  except KeyboardInterrupt:
    logger.info("Shutting down server...")
    server.close()
    await server.wait_closed()


def start():
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    logger.info("Server shutdown complete.")


if __name__ == "__main__":
  start()
